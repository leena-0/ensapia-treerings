#!/usr/bin/env python3
"""
data/*.csv (6개 목데이터 테이블, 실제 ingest.py로 수집된 일부 실제 로그 포함) -> data/treerings.db 일회성 이전.

seed/generate_mock_data.py는 건드리지 않는다 -- 그 스크립트의 계약은 "SEED=42로 재실행하면
100% 동일 출력"이므로, 기존 CSV를 읽어 병합하는 로직을 넣으면 이 순수성이 깨진다. 지금 디스크의
CSV는 이미 목데이터+실제 ingest 로그가 합쳐진 상태이므로, 이 스크립트는 그 CSV를 있는 그대로
SQLite로 복사하기만 한다.

추가로 정성 목표(G09~G19) 11개에 대해 마일스톤 3개(가설수립/실행/결과측정)를 **파생 생성**한다.
난수를 새로 굴리지 않고, seed/generate_mock_data.py 의 stage_for_week()/GOAL_NARRATIVES 를 그대로
재사용해 "그 goal의 slack_logs가 이미 어느 단계까지 진행됐는지"로부터 상태를 역산한다.

사용 예:
  python3 -m scripts.migrate_csv_to_sqlite
  python3 -m scripts.migrate_csv_to_sqlite --force   # 기존 data/treerings.db 를 지우고 재생성
"""

import argparse
import csv
import os
import shutil
import sys
from datetime import date

from reports.data_access import DATA_DIR, DB_PATH, get_connection
from seed.generate_mock_data import QUARTER_START, stage_for_week

LEGACY_CSV_DIR = os.path.join(DATA_DIR, "legacy_csv")

CSV_TABLES = ["members", "goals", "kpis", "goal_kpi_link", "strategy_weights", "slack_logs"]

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE members (
    member_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    team TEXT NOT NULL,
    role TEXT NOT NULL
);

CREATE TABLE goals (
    goal_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES members(member_id),
    quarter TEXT NOT NULL,
    title TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('정량','정성')),
    total_stages INTEGER NOT NULL
);
CREATE INDEX idx_goals_member ON goals(member_id);

CREATE TABLE kpis (
    kpi_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    target TEXT NOT NULL,
    current_value TEXT NOT NULL,
    team TEXT NOT NULL
);

CREATE TABLE goal_kpi_link (
    goal_id TEXT NOT NULL REFERENCES goals(goal_id),
    kpi_id TEXT NOT NULL REFERENCES kpis(kpi_id),
    weight REAL NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('+','-')),
    PRIMARY KEY (goal_id, kpi_id)
);

CREATE TABLE strategy_weights (
    quarter TEXT NOT NULL,
    kpi_name TEXT NOT NULL,
    priority_score INTEGER NOT NULL,
    PRIMARY KEY (quarter, kpi_name)
);

CREATE TABLE slack_logs (
    log_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES members(member_id),
    date TEXT NOT NULL,
    text TEXT NOT NULL,
    linked_goal_id TEXT REFERENCES goals(goal_id),
    channel_id TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_slack_logs_member_date ON slack_logs(member_id, date);
CREATE INDEX idx_slack_logs_goal ON slack_logs(linked_goal_id);

CREATE TABLE goal_milestones (
    milestone_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(goal_id),
    title TEXT NOT NULL,
    order_index INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('미착수','진행중','완료')) DEFAULT '미착수',
    self_reported_at TEXT,
    reported_by TEXT REFERENCES members(member_id),
    UNIQUE (goal_id, order_index)
);

CREATE TABLE milestone_evidence (
    milestone_id TEXT NOT NULL REFERENCES goal_milestones(milestone_id),
    log_id TEXT NOT NULL REFERENCES slack_logs(log_id),
    PRIMARY KEY (milestone_id, log_id)
);
CREATE INDEX idx_milestone_evidence_log ON milestone_evidence(log_id);

-- 리포트 3종(개인 주간/코칭 카드/평가 근거 패키지) 캐시. 원래 data/report_cache/*.json 파일이었으나,
-- 관리자가 조직 전체 리포트 내용을 SQL로 조회할 수 있어야 한다는 요구로 SQLite로 이전했다
-- (scripts/migrate_report_cache_to_sqlite.py). content 는 JSON 문자열 그대로 저장하고
-- json_extract()로 필드별 조회도 가능하다.
CREATE TABLE report_cache (
    report_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    period TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    used_fallback INTEGER NOT NULL DEFAULT 0,
    generated_at TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (report_type, scope_id, period)
);

-- 하위 목표(건물) -- 레몬베이스(외부 OKR 툴) 원본 하위 목표를 mock으로 대체. 정성 목표
-- 마일스톤(goal_milestones)과는 완전히 별개 개념(홈탭 "진행 중인 목표" 표시용). 계량기(비율/최대치
-- 추정)가 아니라 계수기 원칙 -- status는 본인/리더의 명시적 확정 행위만 저장하고("본인완료"/
-- "확정완료", NULL이면 미착수/공사중), 진행 단계는 subgoal_evidence로 연결된 실제 로그 일수를 세어
-- reports/progress.py의 subgoal_stage()가 계산한다(모든 건물 고정 5단계 x 5영업일=25영업일).
-- scripts/seed_demo_subgoals.py 로 19개 목표 전부에 채움.
CREATE TABLE sub_goals (
    sub_goal_id       TEXT PRIMARY KEY,
    goal_id           TEXT NOT NULL REFERENCES goals(goal_id),
    title             TEXT NOT NULL,
    order_index       INTEGER NOT NULL,
    status            TEXT CHECK (status IN ('본인완료','확정완료')),
    self_reported_at  TEXT,
    confirmed_at      TEXT,
    confirmed_by      TEXT REFERENCES members(member_id),
    UNIQUE (goal_id, order_index)
);

CREATE TABLE subgoal_evidence (
    sub_goal_id TEXT NOT NULL REFERENCES sub_goals(sub_goal_id),
    log_id      TEXT NOT NULL REFERENCES slack_logs(log_id),
    PRIMARY KEY (sub_goal_id, log_id)
);
CREATE INDEX idx_subgoal_evidence_log ON subgoal_evidence(log_id);

CREATE TABLE home_view_state (
    member_id TEXT PRIMARY KEY REFERENCES members(member_id),
    last_seen_personal_weekly_generated_at TEXT
);

-- 개인 주간 리포트 "확인 요청"(scripts/add_subgoal_weekly_checkin_table.py 참고): 이번 주 언급
-- 없던 하위 목표에 대해 본인이 진행중/대기/보류/막힘 중 하나를 직접 선택한 이력. sub_goals.status
-- (완료 판정)와는 별개 축 -- 크레인 표시 오버라이드 + 코칭 카드 게이트(막힘만 전달)에 쓰인다.
CREATE TABLE subgoal_weekly_checkin (
    sub_goal_id TEXT NOT NULL REFERENCES sub_goals(sub_goal_id),
    week_start  TEXT NOT NULL,
    week_end    TEXT NOT NULL,
    member_id   TEXT NOT NULL REFERENCES members(member_id),
    status      TEXT NOT NULL CHECK (status IN ('진행중','대기','보류','막힘')),
    reported_at TEXT NOT NULL,
    PRIMARY KEY (sub_goal_id, week_start)
);
CREATE INDEX idx_checkin_member ON subgoal_weekly_checkin(member_id);
"""

# stage_for_week()이 반환하는 내부 키(stage1/stage2/stage3)를 마일스톤 제목으로 매핑.
STAGE_TITLES = {"stage1": "가설수립", "stage2": "실행", "stage3": "결과측정"}
STAGE_ORDER = {"stage1": 1, "stage2": 2, "stage3": 3}


def _read_csv(name):
    path = os.path.join(DATA_DIR, f"{name}.csv")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _derive_milestones(goal, logs_for_goal):
    """정성 목표 하나에 대한 (goal_milestones rows, milestone_evidence rows) 파생.
    난수 없음 -- 그 goal의 slack_logs를 stage_for_week()로 재분류해 역산."""
    goal_id = goal["goal_id"]
    buckets = {1: [], 2: [], 3: []}
    for log in logs_for_goal:
        week_idx = (date.fromisoformat(log["date"]) - QUARTER_START).days // 7
        stage_key = stage_for_week(week_idx)
        buckets[STAGE_ORDER[stage_key]].append(log)

    frontier = max((n for n in (1, 2, 3) if buckets[n]), default=0)

    milestone_rows = []
    evidence_rows = []
    for order_index, stage_key in ((1, "stage1"), (2, "stage2"), (3, "stage3")):
        milestone_id = f"{goal_id}-MS{order_index}"
        bucket_logs = buckets[order_index]

        if 0 < order_index < frontier:
            status = "완료"
            self_reported_at = max(l["date"] for l in bucket_logs)
            reported_by = goal["member_id"]
        elif order_index == frontier:
            status = "진행중"
            self_reported_at = None
            reported_by = None
        else:
            status = "미착수"
            self_reported_at = None
            reported_by = None

        milestone_rows.append((
            milestone_id, goal_id, STAGE_TITLES[stage_key], order_index,
            status, self_reported_at, reported_by,
        ))
        if order_index <= frontier:
            for log in bucket_logs:
                evidence_rows.append((milestone_id, log["log_id"]))

    if frontier == 0:
        print(f"  [경고] {goal_id}: 어떤 단계에도 로그가 없어 마일스톤 3개 전부 '미착수'로 생성됨 (수동 확인 권장)")

    return milestone_rows, evidence_rows


def migrate(force):
    if os.path.exists(DB_PATH):
        if not force:
            sys.exit(f"{DB_PATH} 이미 존재합니다. 다시 만들려면 --force 를 사용하세요.")
        os.remove(DB_PATH)

    csv_rows = {name: _read_csv(name) for name in CSV_TABLES}

    conn = get_connection()
    try:
        conn.executescript(SCHEMA)

        conn.executemany(
            "INSERT INTO members (member_id, name, team, role) VALUES (?, ?, ?, ?)",
            [(r["member_id"], r["name"], r["team"], r["role"]) for r in csv_rows["members"]],
        )
        conn.executemany(
            "INSERT INTO goals (goal_id, member_id, quarter, title, type, total_stages) VALUES (?, ?, ?, ?, ?, ?)",
            [(r["goal_id"], r["member_id"], r["quarter"], r["title"], r["type"], int(r["total_stages"]))
             for r in csv_rows["goals"]],
        )
        conn.executemany(
            "INSERT INTO kpis (kpi_id, name, unit, target, current_value, team) VALUES (?, ?, ?, ?, ?, ?)",
            [(r["kpi_id"], r["name"], r["unit"], r["target"], r["current_value"], r["team"])
             for r in csv_rows["kpis"]],
        )
        conn.executemany(
            "INSERT INTO goal_kpi_link (goal_id, kpi_id, weight, direction) VALUES (?, ?, ?, ?)",
            [(r["goal_id"], r["kpi_id"], float(r["weight"]), r["direction"]) for r in csv_rows["goal_kpi_link"]],
        )
        conn.executemany(
            "INSERT INTO strategy_weights (quarter, kpi_name, priority_score) VALUES (?, ?, ?)",
            [(r["quarter"], r["kpi_name"], int(r["priority_score"])) for r in csv_rows["strategy_weights"]],
        )
        conn.executemany(
            "INSERT INTO slack_logs (log_id, member_id, date, text, linked_goal_id, channel_id, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(r["log_id"], r["member_id"], r["date"], r["text"], r["linked_goal_id"] or None,
              r["channel_id"], r["ts"]) for r in csv_rows["slack_logs"]],
        )

        logs_by_goal = {}
        for r in csv_rows["slack_logs"]:
            logs_by_goal.setdefault(r["linked_goal_id"], []).append(r)

        print("정성 목표 마일스톤 파생 생성:")
        for goal in csv_rows["goals"]:
            if goal["type"] != "정성":
                continue
            milestone_rows, evidence_rows = _derive_milestones(goal, logs_by_goal.get(goal["goal_id"], []))
            conn.executemany(
                "INSERT INTO goal_milestones (milestone_id, goal_id, title, order_index, status, "
                "self_reported_at, reported_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                milestone_rows,
            )
            conn.executemany(
                "INSERT INTO milestone_evidence (milestone_id, log_id) VALUES (?, ?)",
                evidence_rows,
            )
            statuses = ", ".join(f"{m[2]}={m[4]}" for m in milestone_rows)
            print(f"  {goal['goal_id']}: {statuses}")

        conn.commit()
    finally:
        conn.close()

    for name in CSV_TABLES:
        count = get_connection().execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        print(f"{name}: {count} rows")
    milestone_count = get_connection().execute("SELECT COUNT(*) FROM goal_milestones").fetchone()[0]
    print(f"goal_milestones: {milestone_count} rows")

    os.makedirs(LEGACY_CSV_DIR, exist_ok=True)
    for name in CSV_TABLES:
        src = os.path.join(DATA_DIR, f"{name}.csv")
        shutil.move(src, os.path.join(LEGACY_CSV_DIR, f"{name}.csv"))
    print(f"원본 CSV -> {LEGACY_CSV_DIR}/ 로 이동 완료 (스냅샷 보존, 런타임 코드는 더 이상 참조하지 않음)")


def main():
    parser = argparse.ArgumentParser(description="CSV -> SQLite 일회성 이전 + 정성 목표 마일스톤 파생 생성")
    parser.add_argument("--force", action="store_true", help="기존 data/treerings.db 를 지우고 재생성")
    args = parser.parse_args()
    migrate(force=args.force)


if __name__ == "__main__":
    main()
