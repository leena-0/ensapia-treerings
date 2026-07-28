"""SQLite(data/treerings.db)를 메모리 구조로 로드하는 유틸리티. 외부 의존성 없음(표준 sqlite3 모듈만 사용).

기존에는 CSV 파일을 직접 읽었으나, 여러 리더가 조직 전체 평가 자료를 조회할 수 있어야 한다는
요구사항 때문에 SQLite로 이전했다 (scripts/migrate_csv_to_sqlite.py 로 최초 1회 이전, 원본 CSV는
data/legacy_csv/ 에 스냅샷으로 보존됨). DataStore가 노출하는 in-memory 자료구조(list[dict]/인덱스)의
모양은 CSV 시절과 동일하게 유지해 다른 모듈들이 수정 없이 그대로 동작하도록 했다.
"""

import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

REPORTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(REPORTS_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "treerings.db")


def get_connection():
    """DB_PATH에 대한 sqlite3 연결. FK 제약은 연결마다 켜야 하므로(SQLite 특성) 이 헬퍼로 통일한다."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _read_table(conn, name):
    """CSV 시절과 동일하게 모든 값을 문자열로 맞춰(list[dict]) 반환 -- 다른 모듈이 int()/float()로
    캐스팅하는 기존 호출부를 그대로 유지하기 위함."""
    return [{k: ("" if v is None else str(v)) for k, v in dict(row).items()} for row in conn.execute(f"SELECT * FROM {name}")]


class DataStore:
    """6개 테이블 + goal_milestones/milestone_evidence 를 SQLite에서 로드하고, 조회용 인덱스를 만든다.
    읽기 전용 -- 쓰기는 ingest.py/report_milestone.py 가 get_connection()으로 직접 담당한다."""

    def __init__(self):
        conn = get_connection()
        try:
            self.members = _read_table(conn, "members")
            self.goals = _read_table(conn, "goals")
            self.kpis = _read_table(conn, "kpis")
            self.goal_kpi_links = _read_table(conn, "goal_kpi_link")
            self.strategy_weights = _read_table(conn, "strategy_weights")
            self.slack_logs = _read_table(conn, "slack_logs")
            self.goal_milestones_all = _read_table(conn, "goal_milestones")
            self.milestone_evidence_rows = _read_table(conn, "milestone_evidence")
            self.sub_goals_all = _read_table(conn, "sub_goals")
            self.subgoal_evidence_rows = _read_table(conn, "subgoal_evidence")
            self.home_view_state_rows = _read_table(conn, "home_view_state")
            self.subgoal_checkin_rows = _read_table(conn, "subgoal_weekly_checkin")
        finally:
            conn.close()

        self.members_by_id = {m["member_id"]: m for m in self.members}
        self.goals_by_id = {g["goal_id"]: g for g in self.goals}
        self.kpis_by_id = {k["kpi_id"]: k for k in self.kpis}
        self.logs_by_id = {log["log_id"]: log for log in self.slack_logs}
        self.sub_goals_by_id = {sg["sub_goal_id"]: sg for sg in self.sub_goals_all}

        self.goals_by_member = defaultdict(list)
        for g in self.goals:
            self.goals_by_member[g["member_id"]].append(g["goal_id"])

        self.links_by_goal = defaultdict(list)
        for link in self.goal_kpi_links:
            self.links_by_goal[link["goal_id"]].append(link)

        self.strategy_by_key = {
            (row["quarter"], row["kpi_name"]): float(row["priority_score"])
            for row in self.strategy_weights
        }

        self.logs_by_member = defaultdict(list)
        for log in self.slack_logs:
            self.logs_by_member[log["member_id"]].append(log)
        for member_id in self.logs_by_member:
            self.logs_by_member[member_id].sort(key=lambda r: r["date"])

        self.members_by_team = defaultdict(list)
        for m in self.members:
            self.members_by_team[m["team"]].append(m["member_id"])

        self.milestones_by_goal = defaultdict(list)
        for m in self.goal_milestones_all:
            self.milestones_by_goal[m["goal_id"]].append(m)
        for goal_id in self.milestones_by_goal:
            self.milestones_by_goal[goal_id].sort(key=lambda m: int(m["order_index"]))

        self.evidence_log_ids_by_milestone = defaultdict(list)
        for row in self.milestone_evidence_rows:
            self.evidence_log_ids_by_milestone[row["milestone_id"]].append(row["log_id"])

        self.sub_goals_by_goal = defaultdict(list)
        for sg in self.sub_goals_all:
            self.sub_goals_by_goal[sg["goal_id"]].append(sg)
        for goal_id in self.sub_goals_by_goal:
            self.sub_goals_by_goal[goal_id].sort(key=lambda sg: int(sg["order_index"]))

        self.home_view_state_by_member = {row["member_id"]: row for row in self.home_view_state_rows}

        self.evidence_log_ids_by_subgoal = defaultdict(list)
        for row in self.subgoal_evidence_rows:
            self.evidence_log_ids_by_subgoal[row["sub_goal_id"]].append(row["log_id"])

        self.checkins_by_subgoal = defaultdict(list)
        for row in self.subgoal_checkin_rows:
            self.checkins_by_subgoal[row["sub_goal_id"]].append(row)
        for sub_goal_id in self.checkins_by_subgoal:
            self.checkins_by_subgoal[sub_goal_id].sort(key=lambda r: r["week_start"])

    def member_goals(self, member_id):
        return [self.goals_by_id[gid] for gid in self.goals_by_member.get(member_id, [])]

    def goal_links(self, goal_id):
        """goal 하나에 연결된 (link, kpi) 쌍 목록."""
        return [(link, self.kpis_by_id[link["kpi_id"]]) for link in self.links_by_goal.get(goal_id, [])]

    def goal_milestones(self, goal_id):
        """goal 하나에 연결된 (milestone, evidence_logs) 쌍 목록. goal_links()와 동일한 패턴.
        정량 목표 등 마일스톤이 없는 goal_id 는 빈 리스트를 반환한다 (정성 목표만 마일스톤을 가짐)."""
        return [
            (m, [self.logs_by_id[lid] for lid in self.evidence_log_ids_by_milestone.get(m["milestone_id"], [])
                 if lid in self.logs_by_id])
            for m in self.milestones_by_goal.get(goal_id, [])
        ]

    def sub_goals(self, goal_id):
        """goal 하나의 하위 목표(건물) 목록 (order_index 순). 레몬베이스 원본 하위 목표를
        mock으로 대체한 것 -- goal_milestones(정성 목표 HR 평가용)와는 별개 개념."""
        return self.sub_goals_by_goal.get(goal_id, [])

    def subgoal_logs(self, sub_goal_id):
        """그 하위 목표(건물)에 실제로 연결된 slack_logs 목록 (subgoal_stage() 계산용)."""
        return [self.logs_by_id[lid] for lid in self.evidence_log_ids_by_subgoal.get(sub_goal_id, [])
                if lid in self.logs_by_id]

    def last_seen_personal_weekly(self, member_id):
        row = self.home_view_state_by_member.get(member_id)
        return row["last_seen_personal_weekly_generated_at"] if row else None

    def logs_in_range(self, member_id, date_from, date_to):
        return [
            log for log in self.logs_by_member.get(member_id, [])
            if date_from <= log["date"] <= date_to
        ]

    def latest_checkin(self, sub_goal_id, week_start=None):
        """그 하위목표(건물)의 확인 요청 체크인 이력 중, week_start를 지정하면 그 주차 것만,
        지정하지 않으면 가장 최근(week_start 기준) 것을 반환한다. 없으면 None."""
        rows = self.checkins_by_subgoal.get(sub_goal_id, [])
        if week_start is not None:
            return next((r for r in rows if r["week_start"] == week_start), None)
        return rows[-1] if rows else None


def record_subgoal_checkin(sub_goal_id, week_start, week_end, member_id, status):
    """확인 요청(§5-1)에서 본인이 고른 상태를 저장한다. 같은 주(week_start)에 다시 고르면
    upsert -- 가역적 정정을 허용한다 (완료 신고/확정과 달리 이 상태는 되돌릴 수 있는 판단)."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO subgoal_weekly_checkin "
                "(sub_goal_id, week_start, week_end, member_id, status, reported_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sub_goal_id, week_start) DO UPDATE SET "
                "status = excluded.status, reported_at = excluded.reported_at, "
                "member_id = excluded.member_id, week_end = excluded.week_end",
                (sub_goal_id, week_start, week_end, member_id, status,
                 datetime.now(timezone.utc).isoformat()),
            )
    finally:
        conn.close()


def mark_report_seen(member_id, generated_at):
    """홈탭 조회 시 '이 시점까지의 개인 주간 리포트는 봤다'고 기록 (알림 '새 리포트 도착' 판정용)."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO home_view_state (member_id, last_seen_personal_weekly_generated_at) "
                "VALUES (?, ?) ON CONFLICT(member_id) DO UPDATE SET "
                "last_seen_personal_weekly_generated_at = excluded.last_seen_personal_weekly_generated_at",
                (member_id, generated_at),
            )
    finally:
        conn.close()
