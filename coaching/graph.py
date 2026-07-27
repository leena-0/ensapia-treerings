# -*- coding: utf-8 -*-
"""
LangGraph 파이프라인 조립 + 실행 (구현명세_코칭카드 §6).

흐름:
  collect → [detect_bottleneck | unresolved | rework | load | unrecognized] (병렬)
          → merge → verify(코드) → score → confidence → select → generate(LLM) → split_urgent
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

# 기본 활성 탐지기: 5종 모두 (v10.2 확정 — 탐지는 전부 코드/결정론이고, verify_evidence의
# 근거 2건 미만 폐기 + assign_confidence의 low 폐기 + 슬롯 분리 선별로 오탐을 걸러낸다).
DEFAULT_DETECTORS = tuple(DETECTOR_NODES.keys())


def build_graph(top_n: int = 5, enabled_detectors=DEFAULT_DETECTORS):
    g = StateGraph(CoachState)
    g.add_node("collect", nodes.collect_period_logs)
    enabled = [DETECTOR_NODES[c] for c in enabled_detectors if c in DETECTOR_NODES]
    for node_name, fn in enabled:
        g.add_node(node_name, fn)
    g.add_node("merge", nodes.merge_candidates)
    g.add_node("verify", nodes.verify_evidence)
    g.add_node("confidence", nodes.assign_confidence)
    g.add_node("score", nodes.score_and_rank)
    g.add_node("select", partial(nodes.select_top_n, n=top_n))
    g.add_node("generate", nodes.generate_card_text)
    g.add_node("split_urgent", nodes.split_urgent)

    g.add_edge(START, "collect")
    if enabled:
        for node_name, _ in enabled:
            g.add_edge("collect", node_name)   # 병렬 fan-out
            g.add_edge(node_name, "merge")     # merge 는 활성 탐지기 모두 끝난 뒤(fan-in)
    else:
        g.add_edge("collect", "merge")
    g.add_edge("merge", "verify")
    # confidence 가 score 보다 먼저다 — v10.2 점수식이 확신도 항을 쓰기 때문(nodes.score_and_rank).
    g.add_edge("verify", "confidence")
    g.add_edge("confidence", "score")
    g.add_edge("score", "select")
    g.add_edge("select", "generate")
    g.add_edge("generate", "split_urgent")
    g.add_edge("split_urgent", END)
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
    enabled_detectors 로 탐지기를 선택(기본: 5종 전부).
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
        "weekly_status": weekly_status if weekly_status is not None else load_weekly_status(team),
        "llm": narrator,
        "adoption_stats": adoption_stats or {},
    }
    result = build_graph(top_n=top_n, enabled_detectors=enabled_detectors).invoke(state)
    return {
        "team_context": state["team_context"],
        "cards": result.get("cards", []),
        "urgent_alerts": result.get("urgent_alerts", []),
    }
