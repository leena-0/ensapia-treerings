"""
목표 진행 단계("구획") 계산 (data_dictionary.md 12절 공식의 실행 코드).

AI는 이 값을 추정하지 않는다 -- slack_logs 에 실제로 그 goal 로 연결된 로그가 며칠에
걸쳐 작성됐는지를 코드가 세어 결정론적으로 계산한다. 완료 확정(불가역)은 사람의 몫이므로
여기서는 "완료" 여부를 판단하지 않고 stage/total_stages 진행률만 반환한다.
"""

from datetime import date


def goal_stage(store, goal_id, today=None):
    """
    반환: {"worked_days": int, "stage": int, "total_stages": int,
           "last_date": str|None, "freshness": str}
    """
    goal = store.goals_by_id[goal_id]
    total_stages = int(goal["total_stages"])
    logs = [log for log in store.slack_logs if log["linked_goal_id"] == goal_id]
    distinct_dates = sorted({log["date"] for log in logs})

    worked_days = len(distinct_dates)
    stage = min(total_stages, worked_days // 5)

    if not distinct_dates:
        return {
            "worked_days": 0, "stage": 0, "total_stages": total_stages,
            "last_date": None, "freshness": "작업 기록 없음",
        }

    last_date_str = distinct_dates[-1]
    today = today or date.today()
    last_date = date.fromisoformat(last_date_str)
    days_since = (today - last_date).days

    if days_since <= 0:
        freshness = "오늘 작업함"
    else:
        freshness = f"{days_since}일째 작업 없음"

    return {
        "worked_days": worked_days, "stage": stage, "total_stages": total_stages,
        "last_date": last_date_str, "freshness": freshness,
    }
