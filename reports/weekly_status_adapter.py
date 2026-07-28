# -*- coding: utf-8 -*-
"""
subgoal_weekly_checkin(확인 요청 -- 진행중/대기/보류/막힘, slack_app/socket_app.py 의
subgoal_checkin_select 액션이 기록) 실데이터를, coaching/ 파이프라인이 기대하는 스키마
(user_id/subgoal_id/subgoal_title/status/week_of/note)로 변환한다.

reports/ 는 coaching/ 을 몰라야 하므로(coaching -> reports 단방향 의존 유지) 여기서는
plain dict만 반환하고, pydantic 모델(WeeklyStatusSelection) 조립은 이걸 가져다 쓰는
coaching/data_adapter.py 가 담당한다.
"""

STATUS_KO_TO_EN = {"진행중": "in_progress", "대기": "waiting", "보류": "on_hold", "막힘": "blocked"}


def subgoal_checkins_for_team(store, team):
    """그 팀 멤버 소유 하위목표의 체크인 이력 전부를 coaching 스키마 dict로 변환해 반환.

    같은 하위목표를 여러 주에 걸쳐 체크인했으면 전부 반환한다(주차별 이력 전체) --
    coaching/nodes.py 의 detect_blocked_escalation 이 "며칠째 막힘인지"를 판단하려면
    가장 최근 값 하나가 아니라 이력 전체(가장 이른 blocked 시점)가 필요하다.
    """
    out = []
    for row in store.subgoal_checkin_rows:
        sg = store.sub_goals_by_id.get(row["sub_goal_id"])
        if sg is None:
            continue
        goal = store.goals_by_id.get(sg["goal_id"])
        if goal is None:
            continue
        member = store.members_by_id.get(goal["member_id"])
        if member is None or member["team"] != team:
            continue
        out.append({
            "team": team,
            "user_id": row["member_id"],
            "subgoal_id": row["sub_goal_id"],
            "subgoal_title": sg["title"],
            "status": STATUS_KO_TO_EN.get(row["status"], row["status"]),
            "week_of": row["week_start"],
            "note": None,
        })
    return out
