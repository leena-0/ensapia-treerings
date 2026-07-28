# -*- coding: utf-8 -*-
"""
LangGraph 파이프라인 조립 + 실행 (구현명세_코칭카드 §6).

흐름:
  collect → [detect_bottleneck | unresolved | rework | load | unrecognized] (병렬)
          → merge → verify(코드) → confidence → score → split_urgent → select → generate(LLM)
          → finalize_urgent → END
  collect → detect_blocked_escalation (병렬, 위 흐름과 독립) ─────────────────→ finalize_urgent

detect_blocked_escalation 은 merge/verify/confidence/score/select/generate 를 전부 건너뛰고
바로 finalize_urgent 로 간다 — 이유는 nodes.detect_blocked_escalation 문서 참고.
split_urgent 가 select(개수 제한)보다 앞에 있는 이유는 nodes.split_urgent 문서 참고.
"""
from __future__ import annotations

from datetime import date
from functools import partial
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from . import nodes
from .data_adapter import build_team_context, load_weekly_status, load_work_logs


class CoachState(TypedDict, total=False):
    team_context: Any
    work_logs: list
    weekly_status: list
    blocked_selections: list
    llm: Callable
    bottleneck_candidates: list
    unresolved_candidates: list
    rework_candidates: list
    load_candidates: list
    unrecognized_candidates: list
    blocked_escalation_candidates: list
    candidates: list
    adoption_stats: dict
    cards: list
    urgent_alerts: list


# 탐지기 레지스트리 (card_type -> (노드명, 함수))
DETECTOR_NODES = {
    "dependency_bottleneck": ("detect_bottleneck", nodes.detect_bottleneck),
    "unresolved_request": ("detect_unresolved", nodes.detect_unresolved),
    "rework_loop": ("detect_rework", nodes.detect_rework),
    "load_imbalance": ("detect_load", nodes.detect_load),
    "unrecognized_work": ("detect_unrecognized", nodes.detect_unrecognized),
}

# 기본 활성 탐지기: 3종 (2026-07-28 완성본 §5-2 확정). "부하 편중"/"미인지 성과"는 최종
# 스펙에서 명시적으로 제외됐다 — 둘 다 "무엇이 과부하/눈에 띄는 성과인지"를 AI가 판단해야
# 하는 평가 행위라 §4-1 원칙(AI는 분류·대조·계수·추출만, 잘함/못함 판정은 하지 않는다)과
# 충돌하기 때문이다. 미인지 성과에 대한 동료 인정은 씨앗(§6-5)이 대신 담당한다.
# detect_load/detect_unrecognized 노드 자체는 코드에 남겨둔다 -- DETECTOR_NODES 레지스트리에
# 그대로 있고 enabled_detectors 로 명시적으로 켜면 여전히 동작하니, 완전 삭제가 아니라
# "기본값에서 제외"로 처리했다.
DEFAULT_DETECTORS = ("dependency_bottleneck", "unresolved_request", "rework_loop")


def build_graph(top_n: int = 5, enabled_detectors=DEFAULT_DETECTORS):
    g = StateGraph(CoachState)
    g.add_node("collect", nodes.collect_period_logs)
    enabled = [DETECTOR_NODES[c] for c in enabled_detectors if c in DETECTOR_NODES]
    for node_name, fn in enabled:
        g.add_node(node_name, fn)
    g.add_node("detect_blocked_escalation", nodes.detect_blocked_escalation)
    g.add_node("merge", nodes.merge_candidates)
    g.add_node("verify", nodes.verify_evidence)
    g.add_node("confidence", nodes.assign_confidence)
    g.add_node("score", nodes.score_and_rank)
    g.add_node("split_urgent", nodes.split_urgent)
    g.add_node("select", partial(nodes.select_top_n, n=top_n))
    g.add_node("generate", nodes.generate_card_text)
    g.add_node("finalize_urgent", nodes.finalize_urgent)

    g.add_edge(START, "collect")
    if enabled:
        for node_name, _ in enabled:
            g.add_edge("collect", node_name)   # 병렬 fan-out
            g.add_edge(node_name, "merge")     # merge 는 활성 탐지기 모두 끝난 뒤(fan-in)
    else:
        g.add_edge("collect", "merge")
    # blocked_escalation 은 카드 후보 경로와 완전히 독립된 병렬 분기 — merge/verify/confidence/
    # score/select/generate 를 전부 건너뛰고 finalize_urgent 에서 다시 만난다.
    g.add_edge("collect", "detect_blocked_escalation")
    g.add_edge("detect_blocked_escalation", "finalize_urgent")

    g.add_edge("merge", "verify")
    # confidence 가 score 보다 먼저다 — v10.2 점수식이 확신도 항을 쓰기 때문(nodes.score_and_rank).
    g.add_edge("verify", "confidence")
    g.add_edge("confidence", "score")
    # split_urgent 가 select(개수 제한) 보다 앞이다 — 이유는 nodes.split_urgent 문서 참고.
    g.add_edge("score", "split_urgent")
    g.add_edge("split_urgent", "select")
    g.add_edge("select", "generate")
    g.add_edge("generate", "finalize_urgent")
    g.add_edge("finalize_urgent", END)
    return g.compile()


def _default_llm(prompt: str) -> dict:
    """실제 LLM 호출 래퍼 (litellm/Gemini). 반환은 파싱된 dict."""
    from reports.generate_reports import parse_json_safely
    from reports.llm_client import generate_json
    text, _, _ = generate_json(prompt)
    parsed = parse_json_safely(text)
    return parsed if isinstance(parsed, dict) else {}


def run_coaching(team: str, period_start: date, period_end: date, *,
                 store=None, llm: Callable | None = None, use_llm: bool = True,
                 weekly_status=None, top_n: int = 5, enabled_detectors=DEFAULT_DETECTORS,
                 adoption_stats: dict | None = None) -> dict:
    """팀·기간에 대해 코칭 카드 + 긴급 알림을 생성한다.

    탐지·선별·검증은 항상 코드가 결정론적으로 수행한다. 카드 '서술'만:
      - llm 을 주입하면 그것으로(테스트 스텁 가능),
      - 미지정 & use_llm=True 면 실제 Gemini,
      - use_llm=False 면 LLM 없이 코드 템플릿으로 서술(오프라인).
    enabled_detectors 로 탐지기를 선택(기본: 3종 -- 부하 편중/미인지 성과는 완성본 §5-2에서
    제외됨, DEFAULT_DETECTORS 주석 참고). 필요하면 명시적으로 켤 수 있다.
    adoption_stats: {(card_type, manager_id): {"adopt": n, "dismiss": n}} — 점수식의 채택률보정 항.
      미지정이면 이력 없음으로 간주해 모든 유형에 중립값(0.5)을 적용한다.
    """
    if store is None:
        from reports.data_access import DataStore
        store = DataStore()

    narrator = llm if llm is not None else (_default_llm if use_llm else None)
    state: CoachState = {
        "team_context": build_team_context(store, team, period_start, period_end),
        "work_logs": load_work_logs(store, team, period_start, period_end),
        "weekly_status": weekly_status if weekly_status is not None else load_weekly_status(team, store=store),
        "llm": narrator,
        "adoption_stats": adoption_stats or {},
    }
    result = build_graph(top_n=top_n, enabled_detectors=enabled_detectors).invoke(state)
    return {
        "team_context": state["team_context"],
        "cards": result.get("cards", []),
        "urgent_alerts": result.get("urgent_alerts", []),
    }
