"""
목표 진행 단계("구획") 계산 (data_dictionary.md 12절 공식의 실행 코드).

AI는 이 값을 추정하지 않는다 -- slack_logs 에 실제로 그 goal 로 연결된 로그가 며칠에
걸쳐 작성됐는지를 코드가 세어 결정론적으로 계산한다. 완료 확정(불가역)은 사람의 몫이므로
여기서는 "완료" 여부를 판단하지 않고 stage/total_stages 진행률만 반환한다.
"""

from datetime import date, timedelta

SUBGOAL_TOTAL_STAGES = 5
SUBGOAL_DAYS_PER_STAGE = 5  # 5단계 x 5영업일 = 25영업일짜리 하위목표(건물)


def _business_days_elapsed(last_date, today):
    """last_date 다음 날부터 today 까지(포함) 평일(월~금) 개수. 주말은 세지 않는다
    (예: 금요일 작업 후 월요일 아침 = 1영업일 경과)."""
    days = 0
    d = last_date
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


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


def subgoal_stage(store, sub_goal_id, today=None):
    """
    하위 목표(건물)의 진행 단계. goal_stage()와 완전히 같은 원칙: 계량기(최대치/분모를 아는 비율)가
    아니라 계수기다 -- "앞으로 며칠 더 걸릴지"는 아무도 모르는 추정이라 다루지 않고, "실제로 며칠
    작업했는가"라는 오늘 시점에 확정된 사실만 센다.

    모든 건물은 예외 없이 고정 5단계(SUBGOAL_TOTAL_STAGES) x 5영업일(SUBGOAL_DAYS_PER_STAGE) =
    25영업일 구조다(goal의 total_stages처럼 목표마다 다르게 설정하지 않음). 5단계를 다 채워도
    (worked_days >= 25) 자동으로 "완료"가 되지 않는다 -- 건물 높이는 5단계에서 멈추고, 크레인은
    계속 떠 있는 상태(공사중)로 남는다. "완료"는 본인 self-report(report_subgoal.py)의 몫이다.
    """
    logs = store.subgoal_logs(sub_goal_id)
    distinct_dates = sorted({log["date"] for log in logs})
    worked_days = len(distinct_dates)
    stage = min(SUBGOAL_TOTAL_STAGES, worked_days // SUBGOAL_DAYS_PER_STAGE)

    if not distinct_dates:
        return {
            "worked_days": 0, "stage": 0, "total_stages": SUBGOAL_TOTAL_STAGES,
            "last_date": None, "freshness": "작업 기록 없음", "business_days_since": None,
        }

    last_date_str = distinct_dates[-1]
    today = today or date.today()
    last_date = date.fromisoformat(last_date_str)
    days_since = (today - last_date).days
    business_days_since = _business_days_elapsed(last_date, today)

    freshness = "오늘 작업함" if days_since <= 0 else f"{days_since}일째 작업 없음"

    return {
        "worked_days": worked_days, "stage": stage, "total_stages": SUBGOAL_TOTAL_STAGES,
        "last_date": last_date_str, "freshness": freshness, "business_days_since": business_days_since,
    }
