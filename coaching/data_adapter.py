# -*- coding: utf-8 -*-
"""
데이터 어댑터: 기존 자체 DB(현재는 CSV DataStore)를 코칭 명세 스키마로 변환.

명세는 PostgreSQL을 가정하지만, 현재 repo는 CSV(`reports.data_access.DataStore`)를
자체 DB로 사용하므로 이를 그대로 데이터 소스로 삼는다. 저장소가 바뀌어도 이 어댑터만
교체하면 파이프라인/스키마는 그대로다.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, time

from reports.data_access import DATA_DIR, DataStore

from .schemas import TeamContext, WeeklyStatusSelection, WorkLog


def _permalink_for(log: dict) -> str:
    ch, ts = log.get("channel_id", "").strip(), log.get("ts", "").strip()
    if ch and ts:
        return f"https://slack.com/archives/{ch}/p{ts.replace('.', '')}"
    return f"log://{log['log_id']}"       # 목데이터: 실제 permalink 근거 없음


def load_work_logs(store: DataStore, team: str, period_start: date, period_end: date) -> list[WorkLog]:
    """팀 구성원 전원의 기간 내 업무일지를 WorkLog 리스트로."""
    out: list[WorkLog] = []
    for member_id in store.members_by_team.get(team, []):
        name = store.members_by_id[member_id]["name"]
        for l in store.logs_by_member.get(member_id, []):
            d = date.fromisoformat(l["date"])
            if period_start <= d <= period_end:
                out.append(WorkLog(
                    message_id=l["log_id"],
                    user_id=member_id,
                    user_name=name,
                    permalink=_permalink_for(l),
                    timestamp=datetime.combine(d, time.min),
                    text=l["text"],
                ))
    out.sort(key=lambda w: w.timestamp)
    return out


def build_team_context(store: DataStore, team: str, period_start: date, period_end: date) -> TeamContext:
    members = [
        {"user_id": m, "user_name": store.members_by_id[m]["name"], "role": store.members_by_id[m]["role"]}
        for m in store.members_by_team.get(team, [])
    ]
    # 카드 수신자 = 관리 책임자(1차평가자). 없으면 첫 멤버로 폴백.
    manager = next((m["user_id"] for m in members if m["role"] == "1차평가자"),
                   members[0]["user_id"] if members else "")
    return TeamContext(team_id=team, members=members, manager_id=manager,
                       period_start=period_start, period_end=period_end)


def load_weekly_status(team: str, path: str | None = None) -> list[WeeklyStatusSelection]:
    """주간 상태 선택 목 소스(옵션). data/weekly_status_selections.json 이 있으면 로드."""
    path = path or os.path.join(DATA_DIR, "weekly_status_selections.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    return [WeeklyStatusSelection(**r) for r in rows if r.get("team") in (None, team)]
