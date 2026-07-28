# -*- coding: utf-8 -*-
"""
코칭 카드 자체 테스트 (구현명세_코칭카드 §10 시나리오).

LLM은 스텁으로 주입해 **실제 API 없이** 전체 파이프라인을 검증한다.
실행: python -m coaching.selftest
"""
from __future__ import annotations

import io
import sys
from datetime import date, datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from coaching import nodes
from coaching.graph import build_graph
from coaching.schemas import CoachingCard, Evidence, WorkLog
from coaching.store import CardStore


def _wl(mid, uid, name, day, text):
    return WorkLog(message_id=mid, user_id=uid, user_name=name,
                   permalink=f"log://{mid}", timestamp=datetime(2026, 6, day), text=text)


def _stub_llm(prompt: str) -> dict:
    """LLM 대역: 프롬프트 종류를 문구로 식별해 정해진 JSON을 돌려준다."""
    if "카드 문장으로 작성" in prompt:
        return {"headline": "세 분이 같은 승인을 기다리고 있습니다",
                "body": "세 분의 작업이 동일한 리워드 구성안 승인에서 멈춰 있습니다. 승인을 확정하시면 세 분의 후속 작업이 재개됩니다.",
                "grounded": True}
    if "묶어라" in prompt:
        return {"groups": [{"canonical": "리워드 구성안 승인",
                            "message_ids": ["m1", "m2", "m3"], "confidence": "high"}]}
    if "기다리고 있는 상태" in prompt:
        return {"items": [
            {"message_id": "m1", "user_id": "u1", "waiting_for": "리워드 구성안 승인", "target_type": "approval", "explicit": True},
            {"message_id": "m2", "user_id": "u2", "waiting_for": "리워드 컨펌", "target_type": "approval", "explicit": True},
            {"message_id": "m3", "user_id": "u3", "waiting_for": "리워드 스펙 확정", "target_type": "decision", "explicit": True},
        ]}
    return {}


def test_scenario_A_bottleneck_card():
    """A — 의존 병목 카드 생성 (핵심). 지속<5일이라 긴급이 아니라 일반 카드."""
    logs = [
        _wl("m1", "u1", "정하늘", 1, "리워드 구성안 컨펌 회신 대기 중. 승인 나야 착수 가능."),
        _wl("m2", "u2", "김서연", 2, "러프 컨펌 아직 안 옴. 리워드 구성 확정 대기."),
        _wl("m3", "u3", "박준영", 2, "스펙 확정 전이라 착수 불가. 리워드 스펙 승인 대기."),
        _wl("m4", "u4", "한노이", 2, "결제 퍼널 지표 대시보드 정리 진행 중."),
    ]
    state = {"work_logs": logs, "weekly_status": [], "llm": _stub_llm}
    result = build_graph().invoke(state)
    cards, urgent = result.get("cards", []), result.get("urgent_alerts", [])
    assert len(cards) == 1, f"카드 1건 기대, 실제 {len(cards)} (urgent {len(urgent)})"
    c = cards[0]
    assert c.card_type == "dependency_bottleneck"
    assert c.affected_count == 3 and sorted(c.subjects) == ["u1", "u2", "u3"]
    assert c.confidence == "high"
    assert len(c.evidence) >= 2 and all(e.message_id in {"m1", "m2", "m3"} for e in c.evidence)
    assert not urgent, "지속 2일이라 긴급이 아니어야"
    print("PASS A: 의존 병목 카드 생성 (affected 3, high, 근거≥2, 긴급 아님)")


def test_scenario_E_verify_forgery():
    """E — 근거 위조 방어: 존재하지 않는 message_id / 원문에 없는 excerpt 폐기, <2면 카드 폐기."""
    logs = [_wl("m1", "u1", "A", 1, "리워드 승인 대기 중"),
            _wl("m2", "u2", "B", 2, "리워드 승인 대기 중")]
    cand = {"card_type": "dependency_bottleneck", "subjects": ["u1", "u2"],
            "evidence": [
                {"message_id": "m1", "user_name": "A", "permalink": "", "excerpt": "리워드 승인 대기", "timestamp": datetime(2026, 6, 1)},
                {"message_id": "m2", "user_name": "B", "permalink": "", "excerpt": "리워드 승인 대기", "timestamp": datetime(2026, 6, 2)},
                {"message_id": "m999", "user_name": "C", "permalink": "", "excerpt": "존재하지 않음", "timestamp": datetime(2026, 6, 2)},
                {"message_id": "m1", "user_name": "A", "permalink": "", "excerpt": "원문에 없는 발췌", "timestamp": datetime(2026, 6, 1)},
            ], "affected_count": 2, "duration_days": 2, "explicit_count": 2}
    out = nodes.verify_evidence({"work_logs": logs, "candidates": [cand]})["candidates"]
    assert len(out) == 1 and len(out[0]["evidence"]) == 2, "유효 근거 2건만 남아야"

    # 유효 근거가 1건뿐이면 카드 자체 폐기
    cand2 = dict(cand); cand2 = {**cand, "evidence": [cand["evidence"][0], cand["evidence"][2]]}
    out2 = nodes.verify_evidence({"work_logs": logs, "candidates": [cand2]})["candidates"]
    assert out2 == [], "유효 근거 2건 미만이면 카드 폐기"
    print("PASS E: 근거 위조/원문 불일치 폐기 + 2건 미만 카드 폐기")


def test_scenario_F_select_cap():
    """F — 개수 제한: 후보 12건이어도 상위 5건만."""
    cands = [{"card_type": "load_imbalance", "subjects": [f"u{i}"], "evidence": [], "priority_score": i}
             for i in range(12)]
    out = nodes.select_top_n({"candidates": cands}, n=5)["candidates"]
    assert len(out) == 5, f"5건이어야, 실제 {len(out)}"
    print("PASS F: 상위 5건만 선별")


def test_scenario_G_confidence_low_dropped():
    """G — 약한 신호(근거 1건, 영향 1명)는 low 판정으로 폐기."""
    weak = {"card_type": "unresolved_request", "subjects": ["u1"],
            "evidence": [{"message_id": "m1"}], "affected_count": 1, "explicit_count": 1}
    out = nodes.assign_confidence({"candidates": [weak]})["candidates"]
    assert out == [], "low 신호는 카드가 되지 않아야"
    print("PASS G: 확신도 low 폐기")


def test_scenario_H_split_urgent():
    """H — 4명이 8일째 대기 → 긴급으로 분리.

    split_urgent 는 이제 select_top_n 이전(원시 후보 dict 단계)에서 동작한다 — 진짜 긴급한
    후보가 개수 제한에 걸려 조용히 잘리는 걸 막기 위해서다.
    """
    ev = [{"message_id": f"m{i}", "user_name": f"U{i}", "permalink": "", "excerpt": "리워드 승인 대기",
           "timestamp": datetime(2026, 6, i + 1)} for i in range(4)]
    cand = {"card_type": "dependency_bottleneck", "confidence": "high", "subjects": ["u1", "u2", "u3", "u4"],
            "evidence": ev, "affected_count": 4, "duration_days": 8, "canonical": "리워드 승인"}
    res = nodes.split_urgent({"candidates": [cand], "work_logs": []})
    assert len(res["candidates"]) == 0 and len(res["urgent_alerts"]) == 1, "긴급으로 분리되어야"
    print("PASS H: 영향 4명·8일 → 긴급 분리")


def test_scenario_J_blocked_escalation():
    """J — 본인이 2주 이상 blocked로 표시하면(제약 1, §7-5) 즉시 긴급 알림. on_hold는 무시.

    verify_evidence/confidence/score/select 를 거치지 않고 detect_blocked_escalation 이
    직접 UrgentAlert 를 만든다(nodes.detect_blocked_escalation 문서 참고).
    """
    from coaching.schemas import TeamContext, WeeklyStatusSelection

    tc = TeamContext(team_id="사업부", members=[{"user_id": "u1", "user_name": "김도윤", "role": "팀원"}],
                     manager_id="mgr", period_start=date(2026, 6, 1), period_end=date(2026, 6, 14))
    weekly = [
        WeeklyStatusSelection(user_id="u1", subgoal_id="sg1", subgoal_title="온보딩 실험",
                              status="blocked", week_of=date(2026, 6, 1), note="앱개발팀 대응 대기"),
        WeeklyStatusSelection(user_id="u1", subgoal_id="sg1", subgoal_title="온보딩 실험",
                              status="blocked", week_of=date(2026, 6, 15)),
        # on_hold(보류)는 제약 1에 따라 긴급 후보가 되면 안 됨
        WeeklyStatusSelection(user_id="u2", subgoal_id="sg2", subgoal_title="캠페인",
                              status="on_hold", week_of=date(2026, 6, 1)),
    ]
    out = nodes.detect_blocked_escalation({"weekly_status": weekly, "team_context": tc, "work_logs": []})
    alerts = out["blocked_escalation_candidates"]
    assert len(alerts) == 1, f"blocked 1건만 긴급이어야, 실제 {len(alerts)}"
    assert alerts[0].subjects == ["u1"] and alerts[0].card_type == "blocked_escalation"

    # finalize_urgent 가 split_urgent 의 결과와 잘 합쳐지는지도 확인
    merged = nodes.finalize_urgent({"urgent_alerts": [], "blocked_escalation_candidates": alerts})
    assert len(merged["urgent_alerts"]) == 1
    print("PASS J: 2주 이상 blocked 자기표시 → 즉시 긴급, on_hold는 무시, finalize_urgent 병합 확인")


def test_scenario_I_permission():
    """I — 권한 격리: 대상자 본인은 자기 카드 조회 불가, 매니저만 조회."""
    store = CardStore()
    card = CoachingCard(card_id="c1", card_type="dependency_bottleneck", subjects=["u1", "u2"],
                        headline="h", body="b", affected_count=2, duration_days=2)
    store.save("사업부", "2026-06-01_2026-06-14", manager_id="mgr", cards=[card], urgent=[])
    # 매니저는 조회 가능
    assert len(store.cards_for_manager("mgr")) == 1
    # 대상자 u1 이 요청자면 자기 카드는 제외
    assert store.cards_for_manager("mgr", requester_id="u1") == []
    print("PASS I: 매니저만 조회 / 대상자 본인 차단")


def test_api_smoke():
    """API 스모크(httpx 있으면). 실제 LLM 없이 runner 주입."""
    try:
        from fastapi.testclient import TestClient
    except Exception as e:  # httpx 미설치 등
        print(f"SKIP API smoke ({e.__class__.__name__})")
        return
    from coaching.api import create_app
    from coaching.schemas import TeamContext

    def fake_runner(team, ps, pe):
        tc = TeamContext(team_id=team, members=[{"user_id": "mgr", "user_name": "리더", "role": "1차평가자"}],
                         manager_id="mgr", period_start=ps, period_end=pe)
        card = CoachingCard(card_id="card_001", card_type="dependency_bottleneck", subjects=["u1", "u2"],
                            headline="세 분 대기", body="...", affected_count=2, duration_days=2, confidence="high")
        return {"team_context": tc, "cards": [card], "urgent_alerts": []}

    # 인증/인가 주입: 신원=X-User-Id, 생성 권한은 mgr 만
    client = TestClient(create_app(runner=fake_runner, authorize_generate=lambda u, t: u == "mgr"))
    mgr = {"X-User-Id": "mgr"}

    # 생성: 권한 있는 매니저
    r = client.post("/coaching-cards/generate", headers=mgr,
                    json={"team_id": "사업부", "period_start": "2026-06-01", "period_end": "2026-06-14"})
    assert r.status_code == 200 and r.json()["card_count"] == 1, r.text
    period = r.json()["period"]

    # 매니저 조회 OK
    r2 = client.get(f"/coaching-cards?manager_id=mgr&period={period}", headers=mgr)
    assert r2.status_code == 200 and len(r2.json()["cards"]) == 1

    # fail-closed: 헤더(신원) 없이 조회 → 401
    r_noauth = client.get("/coaching-cards?manager_id=mgr")
    assert r_noauth.status_code == 401, r_noauth.text

    # 생성도 신원 없으면 401
    r_gen_noauth = client.post("/coaching-cards/generate",
                               json={"team_id": "사업부", "period_start": "2026-06-01", "period_end": "2026-06-14"})
    assert r_gen_noauth.status_code == 401, r_gen_noauth.text

    # 권한 없는 사용자의 생성 → 403
    r_gen_forbidden = client.post("/coaching-cards/generate", headers={"X-User-Id": "u1"},
                                  json={"team_id": "사업부", "period_start": "2026-06-01", "period_end": "2026-06-14"})
    assert r_gen_forbidden.status_code == 403, r_gen_forbidden.text

    # 대상자 본인(u1)이 매니저 목록 조회 시도 → 403
    r3 = client.get("/coaching-cards?manager_id=mgr", headers={"X-User-Id": "u1"})
    assert r3.status_code == 403, r3.text
    print("PASS API smoke: 생성/조회 + fail-closed(401) + 인가(403)")


if __name__ == "__main__":
    test_scenario_A_bottleneck_card()
    test_scenario_E_verify_forgery()
    test_scenario_F_select_cap()
    test_scenario_G_confidence_low_dropped()
    test_scenario_H_split_urgent()
    test_scenario_J_blocked_escalation()
    test_scenario_I_permission()
    test_api_smoke()
    print("\n✅ 전체 통과")
