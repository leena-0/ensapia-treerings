"""
KPI/목표 우선순위 계산 (data_dictionary.md 7절 공식의 실행 코드).

절대 난수로 우선순위를 정하지 않는다:
    normalized_gap  = KPI 하나의 달성 갭을 0~1 로 정규화한 값
    kpi_priority    = strategy_weights.priority_score(운영자 입력) x normalized_gap
    goal_priority   = sum( goal_kpi_link.weight_i x kpi_priority_i )  -- goal이 링크한 모든 KPI에 대해

이 값들은 LLM이 생성하지 않고 여기서 결정론적으로 계산한 뒤,
리포트 생성 시 "이미 계산된 숫자"로 프롬프트에 전달한다 (LLM은 숫자를 서술만 함).
"""


def normalized_gap(kpi_row, direction):
    target = float(kpi_row["target"])
    current = float(kpi_row["current_value"])
    if target == 0:
        return 0.0
    if direction == "+":
        gap = (target - current) / target
    elif direction == "-":
        gap = (current - target) / target
    else:
        raise ValueError(f"알 수 없는 direction: {direction!r} (kpi={kpi_row['name']})")
    return max(0.0, gap)


def kpi_priority(kpi_row, direction, quarter, store):
    key = (quarter, kpi_row["name"])
    if key not in store.strategy_by_key:
        raise ValueError(
            f"strategy_weights 에 {key} 가 없습니다. "
            f"모든 정량 KPI는 해당 분기의 전략 우선순위 파라미터가 있어야 합니다."
        )
    score = store.strategy_by_key[key]
    gap = normalized_gap(kpi_row, direction)
    return score * gap, gap, score


def goal_priority(goal_id, quarter, store):
    """
    반환: (goal_priority_score, detail_rows)
    detail_rows: [{kpi_name, weight, direction, priority_score, normalized_gap, kpi_priority}, ...]
    링크된 KPI가 없는(정성) goal 은 (None, []) 를 반환한다 -- 0점 처리가 아니라 "순위 계산 대상 아님".
    """
    links = store.goal_links(goal_id)
    if not links:
        return None, []

    total = 0.0
    details = []
    for link, kpi in links:
        weight = float(link["weight"])
        direction = link["direction"]
        kp, gap, score = kpi_priority(kpi, direction, quarter, store)
        total += weight * kp
        details.append({
            "kpi_id": kpi["kpi_id"],
            "kpi_name": kpi["name"],
            "weight": weight,
            "direction": direction,
            "current_value": kpi["current_value"],
            "target": kpi["target"],
            "unit": kpi["unit"],
            "priority_score": score,
            "normalized_gap": round(gap, 4),
            "kpi_priority": round(kp, 4),
        })
    return round(total, 4), details


def rank_team_goals(team, quarter, store):
    """
    팀 코칭 카드용: 해당 팀 멤버들의 모든 goal 을 goal_priority 내림차순 정렬.
    정성 목표(goal_priority=None)는 별도 리스트로 분리해 "정기 체크인 필요" 섹션에 사용.
    """
    ranked = []
    qualitative = []
    for member_id in store.members_by_team[team]:
        for goal in store.member_goals(member_id):
            score, details = goal_priority(goal["goal_id"], quarter, store)
            if score is None:
                qualitative.append({
                    "goal_id": goal["goal_id"],
                    "member_id": member_id,
                    "title": goal["title"],
                })
            else:
                ranked.append({
                    "goal_id": goal["goal_id"],
                    "member_id": member_id,
                    "title": goal["title"],
                    "goal_priority_score": score,
                    "kpi_details": details,
                })
    ranked.sort(key=lambda r: r["goal_priority_score"], reverse=True)
    return ranked, qualitative
