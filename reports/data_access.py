"""CSV 목 데이터를 메모리 구조로 로드하는 유틸리티. 외부 의존성 없음(표준 csv 모듈만 사용)."""

import csv
import os
from collections import defaultdict

REPORTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(REPORTS_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def _read_csv(name):
    path = os.path.join(DATA_DIR, name)
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class DataStore:
    """5개 목 데이터 테이블 + strategy_weights 를 로드하고, 조회용 인덱스를 만든다."""

    def __init__(self):
        self.members = _read_csv("members.csv")
        self.goals = _read_csv("goals.csv")
        self.kpis = _read_csv("kpis.csv")
        self.goal_kpi_links = _read_csv("goal_kpi_link.csv")
        self.strategy_weights = _read_csv("strategy_weights.csv")
        self.slack_logs = _read_csv("slack_logs.csv")

        self.members_by_id = {m["member_id"]: m for m in self.members}
        self.goals_by_id = {g["goal_id"]: g for g in self.goals}
        self.kpis_by_id = {k["kpi_id"]: k for k in self.kpis}

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

    def member_goals(self, member_id):
        return [self.goals_by_id[gid] for gid in self.goals_by_member.get(member_id, [])]

    def goal_links(self, goal_id):
        """goal 하나에 연결된 (link, kpi) 쌍 목록."""
        return [(link, self.kpis_by_id[link["kpi_id"]]) for link in self.links_by_goal.get(goal_id, [])]

    def logs_in_range(self, member_id, date_from, date_to):
        return [
            log for log in self.logs_by_member.get(member_id, [])
            if date_from <= log["date"] <= date_to
        ]
