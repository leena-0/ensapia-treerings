# -*- coding: utf-8 -*-
"""
협업 기록(노선·정원) 계산 -- wish_match.py 가 만드는 data/wish_pending.json 을 근거로,
그 분기에 확정(confirmed)된 협업만 집계한다.

홈탭(slack_app/home_view.py 의 기여 요약 줄)과 평가 근거 패키지(reports/generate_reports.py 의
run_evidence)가 동일한 로직을 필요로 해서 여기로 분리했다 (중복 방지). 원문/발췌는 담지 않고
관계(누구·같은팀 여부)·주제(helper_topic)·월 단위 시점만 담는다 -- wish_match.py 의 프라이버시
원칙(§5-3)과 동일 기준.
"""

import json
import os

from .data_access import DATA_DIR

WISH_PENDING_PATH = os.path.join(DATA_DIR, "wish_pending.json")


def load_wish_pending():
    if not os.path.exists(WISH_PENDING_PATH):
        return []
    with open(WISH_PENDING_PATH, encoding="utf-8") as f:
        return json.load(f)


def quarter_wishes(member_id, records, quarter_start, quarter_end):
    """그 분기(quarter_start~quarter_end, date 객체) 안에서 member_id 가 관여한 confirmed 소원만 추린다."""
    out = []
    for r in records:
        if r.get("status") != "confirmed":
            continue
        checked_date = r.get("checked_at", "")[:10]
        if not (quarter_start.isoformat() <= checked_date <= quarter_end.isoformat()):
            continue
        if r.get("stuck_member_id") == member_id or r.get("helper_member_id") == member_id:
            out.append(r)
    return out


def member_collaboration_summary(member_id, store, quarter_start, quarter_end):
    """
    반환: {"same_team_count", "other_team_count", "helped": [...], "received": [...]}
    - helped: 내가 도와준 것 (§6-5 "발송 기록" -- 상대가 나에게 씨앗을 보낸 관계)
    - received: 내가 도움받은 것
    각 항목은 {member_id, name, team, topic, month} 만 담고 원문/log_id는 담지 않는다.
    """
    member = store.members_by_id[member_id]
    pending_all = load_wish_pending()
    records = quarter_wishes(member_id, pending_all, quarter_start, quarter_end)

    helped, received = [], []
    same_team, other_team = set(), set()

    for r in records:
        other_id = r["helper_member_id"] if r["stuck_member_id"] == member_id else r["stuck_member_id"]
        other = store.members_by_id.get(other_id)
        if not other:
            continue
        (same_team if other["team"] == member["team"] else other_team).add(other_id)

        entry = {
            "member_id": other_id, "name": other["name"], "team": other["team"],
            "topic": r.get("helper_topic") or r.get("problem_summary") or "",
            "month": (r.get("checked_at") or "")[:7],
        }
        if r.get("helper_member_id") == member_id:
            helped.append(entry)
        else:
            received.append(entry)

    return {
        "same_team_count": len(same_team), "other_team_count": len(other_team),
        "helped": helped, "received": received,
    }
