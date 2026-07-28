# -*- coding: utf-8 -*-
"""
코칭 카드 파이프라인 노드 (구현명세_코칭카드 §6, §7).

원칙(개요 §5-0, §7-1): 탐지·선별·초안은 AI/코드가, 확정(채택/기각)은 리더가.
  - 대기 대상 추출/정규화(detect_bottleneck)와 카드 본문(generate_card_text)만 LLM.
  - **근거 검증(verify_evidence)·점수·확신도·선별·긴급분리는 전부 코드** (LLM 미개입).
  - 근거 발췌(excerpt)는 LLM이 지어내지 않도록 **원문에서 코드가 잘라낸 부분 문자열**만 사용.

state(dict)를 노드들이 갱신하며 흐른다. LLM 호출은 state["llm"](prompt)->dict 로 주입받아
테스트 시 스텁으로 대체할 수 있다.
"""
from __future__ import annotations

import re
from datetime import datetime

from .schemas import CoachingCard, Evidence, UrgentAlert

# ---------------------------------------------------------------------------
# 마커
# ---------------------------------------------------------------------------
WAIT_MARKERS = ("대기", "승인", "컨펌", "회신", "착수 불가", "확정 전", "보류", "지연", "기다")
REQUEST_MARKERS = ("요청", "문의", "도움", "지원 요청", "협업 요청")
RESOLUTION_MARKERS = ("완료", "해결", "반영", "처리", "적용", "복구")
ACHIEVEMENT_MARKERS = ("완료", "해결", "달성", "출시", "릴리스", "배포", "성공")
URGENT_KEYWORDS = ("릴리스", "출시", "납기", "배포", "장애", "긴급")

_SENT_SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+")


def _excerpt(text: str, n: int = 60) -> str:
    """원문에서 마커가 든 문장을 골라 반환(반드시 원문 부분 문자열). 없으면 앞부분."""
    for sent in _SENT_SPLIT.split(text):
        s = sent.strip()
        if s and any(m in s for m in WAIT_MARKERS + REQUEST_MARKERS + RESOLUTION_MARKERS):
            return s[:n]
    return text[:n]


def _ev(wl, excerpt=None) -> dict:
    return {
        "message_id": wl.message_id,
        "user_name": wl.user_name,
        "permalink": wl.permalink,
        "excerpt": excerpt if excerpt is not None else _excerpt(wl.text),
        "timestamp": wl.timestamp,
    }


def _duration_days(evidence_or_ts) -> int:
    ts = [e["timestamp"] if isinstance(e, dict) else e for e in evidence_or_ts]
    if not ts:
        return 0
    return (max(ts) - min(ts)).days + 1


# ---------------------------------------------------------------------------
# LLM 프롬프트 (object 형태 JSON 강제 — litellm json_object 모드와 호환)
# ---------------------------------------------------------------------------
CARD_TEXT_PROMPT = """아래 탐지 결과를 관리 책임자에게 전달할 카드 문장으로 작성하라.

[공통 제약]
- 아래 근거에 있는 사실만 사용(추측 금지)
- headline 은 한 문장 40자 이내
- body 는 2~3문장, 구성원 비난 금지(구조적 문제로 서술)

[확신도={confidence} — body의 마지막 문장 형식을 반드시 지킬 것]
{tone}

탐지 유형: {card_type}
영향 인원: {affected_count}명 / 지속: {duration_days}일

근거:
{evidence}

JSON 객체만 출력: {{"headline": "...", "body": "...", "grounded": true}}
근거로 문장을 뒷받침할 수 없으면 grounded=false.
"""

# 프롬프트에 넣을 유형 라벨 — card_type 은 내부 스키마용 snake_case 식별자(dependency_bottleneck 등)라
# 그대로 프롬프트에 넣으면 일부 LLM(Solar Pro 등)이 이 식별자를 그대로 문장에 베껴 쓰는 걸 확인했다
# ("...rework_loop가 발생하고 있으며" 처럼 리더가 읽을 문장에 내부 코드명이 노출됨). 그래서 프롬프트에는
# 항상 이 한국어 라벨을 넣는다(스키마에 저장되는 card_type 자체는 영향받지 않음).
CARD_TYPE_LABEL_KO = {
    "dependency_bottleneck": "의존 병목",
    "unresolved_request": "미해결 요청",
    "rework_loop": "재작업 반복",
    "load_imbalance": "부하 편중",
    "unrecognized_work": "미인지 성과",
    "blocked_escalation": "장기 막힘",
}


# ---------------------------------------------------------------------------
# 노드
# ---------------------------------------------------------------------------
def collect_period_logs(state: dict) -> dict:
    # work_logs / blocked_selections 는 러너가 초기 상태로 넣어준다. 여기서는 통과.
    blocked = [s for s in state.get("weekly_status", []) if getattr(s, "status", None) == "blocked"]
    return {"blocked_selections": blocked}


BLOCKED_ESCALATION_MIN_DAYS = 14


def detect_blocked_escalation(state: dict) -> dict:
    """[코드] 본인이 주간 상태에서 직접 blocked로 표시한 하위목표가 14일(2주) 이상
    지속되면 즉시 긴급 알림으로 만든다 (제약 1: 정체는 자동 판정하지 않고, 본인이
    blocked라고 표시한 것만 신호로 받는다. §7-5 세 번째 긴급 조건).

    verify_evidence·assign_confidence·score_and_rank·select_top_n 을 전부 건너뛰고
    바로 UrgentAlert 를 만든다(finalize_urgent 에서 split_urgent 결과와 합쳐짐) — 이 신호는
    사람이 직접 입력한 상태값이라(LLM 미개입) 위조 위험이 없어 검증이 필요 없고, "확신도"라는
    개념도 안 맞으며, 다른 카드와 점수 경쟁을 시키면(select_top_n) 그 주 다른 후보가 많다는
    이유로 진짜 SOS가 조용히 탈락할 수 있기 때문이다.

    단순화: 상태가 blocked -> on_hold -> blocked 처럼 중간에 풀렸다 다시 막힌 이력까지는
    구분하지 않는다. 이 (user, subgoal) 조합에서 가장 이른 blocked 선택 ~ 가장 최근 선택
    사이의 기간만 보고, 가장 최근 선택이 blocked 가 아니면(이미 풀렸으면) 후보에서 뺀다.
    """
    selections = state.get("weekly_status", [])
    if not selections:
        return {"blocked_escalation_candidates": []}

    by_key: dict[tuple, list] = {}
    for s in selections:
        by_key.setdefault((s.user_id, s.subgoal_id), []).append(s)

    tc = state.get("team_context")
    members = {m["user_id"]: m["user_name"] for m in tc.members} if tc else {}
    detected_at = max((w.timestamp for w in state.get("work_logs", [])), default=datetime(2026, 1, 1))

    alerts = []
    for (uid, _sgid), entries in by_key.items():
        entries = sorted(entries, key=lambda e: e.week_of)
        if entries[-1].status != "blocked":
            continue  # 가장 최근 선택이 blocked 가 아니면 이미 풀린 것
        blocked_entries = [e for e in entries if e.status == "blocked"]
        span_days = (entries[-1].week_of - blocked_entries[0].week_of).days
        if span_days < BLOCKED_ESCALATION_MIN_DAYS:
            continue
        latest = entries[-1]
        user_name = members.get(uid, uid)
        excerpt = latest.note or f"{latest.subgoal_title} — {span_days}일째 막힘으로 표시"
        alerts.append(UrgentAlert(
            alert_id=f"urgent_blocked_{len(alerts) + 1:03d}",
            card_type="blocked_escalation",
            reason="본인이 2주 이상 '막힘'으로 표시",
            headline=f"{user_name} 님이 '{latest.subgoal_title}' 작업을 {span_days}일째 막힘으로 표시했습니다.",
            subjects=[uid],
            evidence=[Evidence(
                message_id=f"weekly:{latest.subgoal_id}:{latest.week_of}", user_name=user_name, permalink="",
                excerpt=excerpt, timestamp=datetime.combine(latest.week_of, datetime.min.time()),
            )],
            detected_at=detected_at,
        ))
    return {"blocked_escalation_candidates": alerts}


def detect_bottleneck(state: dict) -> dict:
    """[코드·결정론] 의존 병목 탐지 — reports.coaching_signals 코어 재사용.

    LLM 정규화의 recall 편차(당사자 누락)를 없애기 위해 탐지를 **코드가** 수행한다
    (프로젝트 원칙: 코드가 탐지·검증, LLM은 서술만). '막혔을 때만 등장하는 대상 토큰'으로
    대기 로그를 군집화해 2명 이상이 같은 대상을 기다리면 병목으로 판정한다.
    """
    from reports.coaching_signals import detect_bottlenecks

    logs = state.get("work_logs", [])
    if not logs:
        return {"bottleneck_candidates": []}

    records = [{"id": w.message_id, "user_id": w.user_id,
                "date": w.timestamp.date().isoformat(), "text": w.text} for w in logs]
    wl_by_id = {w.message_id: w for w in logs}

    cands = []
    for s in detect_bottlenecks(records):
        # excerpt 는 노드에서 원문 부분 문자열로 다시 뽑는다(코어 excerpt엔 '…' 잘림표시가 붙어
        # verify_evidence 의 '원문 포함' 검증을 통과하지 못하므로).
        evidence = [_ev(wl_by_id[e["id"]]) for e in s["evidence"] if e["id"] in wl_by_id]
        cands.append({
            "card_type": "dependency_bottleneck",
            "canonical": ", ".join(s["shared_terms"][:2]),
            "subjects": s["member_ids"],
            "evidence": evidence,
            "affected_count": s["impact_count"],
            "duration_days": s["duration_days"],
            "explicit_count": s["evidence_strength"],   # 대기 로그 수
        })
    return {"bottleneck_candidates": cands}


def _has(text, markers):
    return any(m in text for m in markers)


def detect_unresolved(state: dict) -> dict:
    """[코드] 도움/결정 요청 후 같은 사람의 해결 언급이 없으면 후보.

    '해결됐다'는 판정은 나중 로그에 해결 마커가 있을 뿐 아니라 **그 요청과 같은 대상을
    언급할 때만** 인정한다(내용 토큰이 2개 이상 겹쳐야 함). 마커만 보고 판정하면, 전혀 다른
    주제의 정기 진행 로그("...세그먼트 정의 완료" 등)가 우연히 "완료"를 언급했다는 이유로
    무관한 요청까지 해결된 것으로 오판해 실제 미해결 신호를 놓친다. 겹침 기준을 토큰 1개로
    두면 이번엔 반대로, 그 사람이 늘 쓰는 공통 업무 단어(예: "결제") 하나만 겹쳐도 완전히
    다른 건이 해결된 것으로 오판되는 걸 확인해 2개 이상으로 올렸다(둘 다 사업부 목데이터에서
    실측 확인).
    """
    from reports.coaching_signals import _content_tokens

    logs = state.get("work_logs", [])
    by_user = {}
    for w in logs:
        by_user.setdefault(w.user_id, []).append(w)
    cands = []
    for uid, ws in by_user.items():
        ws.sort(key=lambda w: w.timestamp)
        for i, w in enumerate(ws):
            if _has(w.text, REQUEST_MARKERS) and not _has(w.text, RESOLUTION_MARKERS):
                req_tokens = _content_tokens(w.text)
                resolved = any(
                    _has(later.text, RESOLUTION_MARKERS) and len(req_tokens & _content_tokens(later.text)) >= 2
                    for later in ws[i + 1:]
                )
                if not resolved:
                    cands.append({
                        "card_type": "unresolved_request", "canonical": "",
                        "subjects": [uid], "evidence": [_ev(w)],
                        "affected_count": 1, "duration_days": 1, "explicit_count": 1,
                    })
    return {"unresolved_candidates": cands}


_REWORK_UNAMBIGUOUS = ("롤백", "재작업", "되돌", "다시 작업", "재수정")


def _is_rework_mention(text: str) -> bool:
    """재작업 마커 중 "수정"은 그 자체로 반복을 의미하지 않는다 -- "~을 수정해 해결함"처럼
    1회성 완료 서술에도 흔히 쓰인다(실측 버그: 동일한 완료 문장이 여러 주에 걸쳐 그대로 반복
    저장된 목데이터가 "3회 재작업"으로 오탐됨). 재작업/롤백/되돌/다시 작업/재수정처럼 단어
    자체가 반복을 뜻하는 마커는 그대로 인정하고, "수정"만 있는 경우엔 그 문장에 완료/해결
    마커(RESOLUTION_MARKERS)가 함께 있으면 -- 즉 "고쳐서 끝냈다"는 뜻이면 -- 재작업 신호로
    보지 않는다."""
    if _has(text, _REWORK_UNAMBIGUOUS):
        return True
    return "수정" in text and not _has(text, RESOLUTION_MARKERS)


def detect_rework(state: dict) -> dict:
    """[코드] 재작업 성격의 언급이 한 사람에게 서로 다른 사건으로 3건 이상 누적되면 후보.

    실측 버그: 목데이터가 goal의 같은 단계(stage) 문장을 여러 주에 걸쳐 문자 그대로 반복
    저장하는 특성이 있어(seed/generate_mock_data.py 내러티브 재사용), "한 번 있었던 일"이
    "3회 반복 수정"으로 잘못 집계됐다. 그래서 정규화한 문장이 완전히 같으면 하나의 사건으로
    묶고, 서로 구별되는 사건이 3건 이상일 때만 후보로 삼는다.
    """
    logs = state.get("work_logs", [])
    by_user = {}
    for w in logs:
        if _is_rework_mention(w.text):
            by_user.setdefault(w.user_id, []).append(w)
    cands = []
    for uid, ws in by_user.items():
        seen_text = set()
        distinct = []
        for w in sorted(ws, key=lambda w: w.timestamp):
            norm = " ".join(w.text.split())
            if norm in seen_text:
                continue
            seen_text.add(norm)
            distinct.append(w)
        if len(distinct) >= 3:
            ev = [_ev(w) for w in distinct[:3]]
            cands.append({
                "card_type": "rework_loop", "canonical": "",
                "subjects": [uid], "evidence": ev,
                "affected_count": 1, "duration_days": _duration_days(ev), "explicit_count": len(distinct),
            })
    return {"rework_candidates": cands}


def detect_load(state: dict) -> dict:
    """[코드] 한 사람의 로그 수가 팀 평균 대비 과다하면 후보."""
    logs = state.get("work_logs", [])
    by_user = {}
    for w in logs:
        by_user.setdefault(w.user_id, []).append(w)
    if len(by_user) < 2:
        return {"load_candidates": []}
    counts = {u: len(ws) for u, ws in by_user.items()}
    mean = sum(counts.values()) / len(counts)
    cands = []
    for uid, ws in by_user.items():
        if counts[uid] >= mean * 1.6 and counts[uid] >= mean + 3:
            ev = [_ev(w) for w in sorted(ws, key=lambda w: w.timestamp)[:3]]
            cands.append({
                "card_type": "load_imbalance", "canonical": "",
                "subjects": [uid], "evidence": ev,
                "affected_count": 1, "duration_days": _duration_days(ev), "explicit_count": len(ev),
            })
    return {"load_candidates": cands}


RISK_MARKERS = ("우려", "지연", "밀릴", "실패", "불가", "안 됨", "못하", "미완료", "리스크")


def _achievement_sentence(text: str) -> str | None:
    """성과 마커가 있으면서 위험/부정 표현이 섞이지 않은 문장만 성과로 인정한다.
    ('출시 일정이 밀릴 우려' 처럼 마커 단어만 겹치는 리스크 문장을 성과로 오인하는 것을 막는다).
    없으면 None — text[:n] 같은 폴백을 두지 않는다. 폴백을 두면 마커가 실제로 없는 문장도
    성과로 취급하게 되어 탐지 게이트(_achievement_sentence(w.text) 존재 여부)가 무력화된다."""
    for sent in _SENT_SPLIT.split(text):
        s = sent.strip()
        if s and _has(s, ACHIEVEMENT_MARKERS) and not _has(s, RISK_MARKERS):
            return s
    return None


def detect_unrecognized(state: dict) -> dict:
    """[코드] 성과 언급이 있는데 노출이 적은 경우(best-effort).

    주의: '아무도 언급 안 함'은 코드로 신뢰성 있게 판정하기 어렵다(오탐 위험). 반복되는
    동일 성과 문장을 성과 여러 건으로 오인하지 않도록 **서로 다른 성과 문장 2건 이상**일 때만
    후보로 둔다. 로그 전체 텍스트가 아니라 성과 문장 자체로 중복을 판정한다 — 날짜별로 다른
    문구가 섞인 로그라도 반복되는 성과 문장(주간 정기 보고 등)은 그대로 중복으로 잡아야 하기
    때문이다. 근거 excerpt도 이 성과 문장 자체를 쓴다(_ev() 기본값은 대기/요청 마커 기준이라
    성과 탐지 근거로 쓰면 카드 내용과 무관한 문장이 인용될 수 있다).
    """
    logs = state.get("work_logs", [])
    by_user = {}
    for w in logs:
        if _achievement_sentence(w.text):
            by_user.setdefault(w.user_id, []).append(w)
    cands = []
    for uid, ws in by_user.items():
        # 동일 성과 문장 중복 제거 — 같은 성과가 반복 보고된 것을 성과 여러 건으로 세지 않는다
        uniq = {}
        for w in sorted(ws, key=lambda w: w.timestamp):
            uniq.setdefault(_achievement_sentence(w.text), w)
        distinct = list(uniq.values())
        if len(distinct) >= 2:
            ev = [_ev(w, excerpt=_achievement_sentence(w.text)[:60]) for w in distinct[:3]]
            cands.append({
                "card_type": "unrecognized_work", "canonical": "",
                "subjects": [uid], "evidence": ev,
                "affected_count": 1, "duration_days": _duration_days(ev), "explicit_count": len(ev),
            })
    return {"unrecognized_candidates": cands}


def merge_candidates(state: dict) -> dict:
    """탐지 후보를 한 목록으로 모으고, 같은 (유형·대상) 후보는 병합.

    병합되는 두 후보는 원래 서로 다른 개별 탐지(예: 같은 사람의 서로 다른 미해결 요청 2건)이므로
    explicit_count(신호 총량)는 합산하고, duration_days는 합쳐진 근거 전체 구간으로 다시 계산한다
    (병합 전 첫 후보 값을 그대로 두면 병합 후에도 과소 집계되어 확신도·점수가 실제보다 낮게 나온다).
    """
    merged = {}
    for key in ("bottleneck_candidates", "unresolved_candidates", "rework_candidates",
                "load_candidates", "unrecognized_candidates"):
        for c in state.get(key, []) or []:
            k = (c["card_type"], tuple(sorted(c["subjects"])))
            if k in merged:
                prev = merged[k]
                seen = {e["message_id"] for e in prev["evidence"]}
                prev["evidence"].extend(e for e in c["evidence"] if e["message_id"] not in seen)
                prev["explicit_count"] = prev.get("explicit_count", 0) + c.get("explicit_count", 0)
                prev["duration_days"] = _duration_days(prev["evidence"])
            else:
                merged[k] = dict(c)
    return {"candidates": list(merged.values())}


def verify_evidence(state: dict) -> dict:
    """[코드, LLM 금지] message_id 실재 + excerpt가 원문에 포함되는지 대조. 근거 2건 미만이면 폐기 (§7-3)."""
    text_by_id = {w.message_id: w.text for w in state.get("work_logs", [])}
    out = []
    for c in state.get("candidates", []):
        valid = [e for e in c["evidence"]
                 if e["message_id"] in text_by_id and e["excerpt"] in text_by_id[e["message_id"]]]
        if len(valid) >= 2:
            c = dict(c)
            c["evidence"] = valid
            out.append(c)
    return {"candidates": out}


def assign_confidence(state: dict) -> dict:
    """high/medium 판정, low는 폐기 (제약 2, §7-4)."""
    out = []
    for c in state.get("candidates", []):
        explicit = c.get("explicit_count", len(c["evidence"]))
        if explicit >= 3 and c["affected_count"] >= 2:
            c = dict(c); c["confidence"] = "high"
        elif len(c["evidence"]) >= 2:
            c = dict(c); c["confidence"] = "medium"
        else:
            continue  # low → 카드 생성 안 함
        out.append(c)
    return {"candidates": out}


# ---------------------------------------------------------------------------
# 우선순위 점수 (프로젝트_개요 v10.2 확정, §5)
#   점수 = 유형기본값 × 확신도 × 영향범위 × 지속계수 × 채택률보정
# assign_confidence 가 score_and_rank 보다 먼저 실행되어야 한다(확신도 항이 필요하므로).
# ---------------------------------------------------------------------------
TYPE_BASE = {
    "dependency_bottleneck": 1.0,
    "unresolved_request": 0.9,
    "rework_loop": 0.8,
    "load_imbalance": 0.8,
    "unrecognized_work": 0.8,
}
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.6}
PROBLEM_TYPES = {"dependency_bottleneck", "unresolved_request", "rework_loop", "load_imbalance"}
RECOGNITION_TYPES = {"unrecognized_work"}


def _impact_factor(affected_count: int) -> float:
    if affected_count >= 3:
        return 1.0
    if affected_count == 2:
        return 0.8
    return 0.5


def _duration_factor_v2(duration_days: int) -> float:
    # 하한 0.5(사안 소멸 방지), 상한 1.0, 10일 포화(카드 주기 14일 안에서 포화해야 의미 있음)
    return 0.5 + 0.5 * min(1, duration_days / 10)


def _adoption_factor(card_type: str, manager_id: str, adoption_stats: dict) -> float:
    """유형×리더 채택률의 라플라스 평활 보정. 이력이 없으면 0.5(중립)로 시작해 표본이 적을 때
    과민 반응하지 않는다. 하한 0.3 — 채택률이 낮은 유형도 완전히 배제되지 않고 재평가 기회를 준다."""
    stats = (adoption_stats or {}).get((card_type, manager_id), {})
    adopt, dismiss = stats.get("adopt", 0), stats.get("dismiss", 0)
    return max(0.3, (adopt + 1) / (adopt + dismiss + 2))


def score_and_rank(state: dict) -> dict:
    """priority_score = 유형기본값 × 확신도 × 영향범위 × 지속계수 × 채택률보정 (v10.2 확정)."""
    tc = state.get("team_context")
    manager_id = tc.manager_id if tc else ""
    adoption_stats = state.get("adoption_stats") or {}
    scored = []
    for c in state.get("candidates", []):
        c = dict(c)
        c["priority_score"] = round(
            TYPE_BASE.get(c["card_type"], 0.8)
            * CONFIDENCE_WEIGHT.get(c["confidence"], 0.6)
            * _impact_factor(c["affected_count"])
            * _duration_factor_v2(c["duration_days"])
            * _adoption_factor(c["card_type"], manager_id, adoption_stats),
            4,
        )
        scored.append(c)
    scored.sort(key=lambda x: x["priority_score"], reverse=True)
    return {"candidates": scored}


def select_top_n(state: dict, n: int = 5) -> dict:
    """선별 — 슬롯 분리(v10.2 확정): 문제 카드 최대 4건 + 인정 카드 최대 1건.
    인정 카드는 본질적으로 1인·최저 영향범위 계수라 단일 순위로 세우면 문제 카드에 밀려
    영원히 선별되지 않으므로 슬롯을 분리한다. 인정 후보가 없는 주기엔 문제 카드로 빈 슬롯을 채운다."""
    scored = state.get("candidates", [])
    problems = [c for c in scored if c["card_type"] in PROBLEM_TYPES]
    praises = [c for c in scored if c["card_type"] in RECOGNITION_TYPES]
    selected = problems[:4] + praises[:1]
    if len(selected) < n:
        selected += problems[4:4 + (n - len(selected))]
    return {"candidates": selected[:n]}


def _tone(conf: str) -> str:
    """확신도별 문형 지시 — 관찰된 문제(특히 high에서 문제 설명만 하고 끝내는 경우가 많음)를
    줄이려고, "이렇게 써라"가 아니라 body 마지막 문장이 반드시 만족해야 할 형식을 못 박는다."""
    if conf == "high":
        return (
            "body의 마지막 문장은 리더가 취할 구체적 행동과 그 결과를 명시한 조언 문장이어야 "
            "한다. 형식: \"(행동)하시면 (결과)됩니다.\" — 예: \"승인을 확정하시면 후속 작업이 "
            "재개됩니다.\" (이 예시 문구를 그대로 베끼지 말고 형식만 따라 이번 근거에 맞는 "
            "구체적 행동/결과로 채울 것). 문제 상황 설명만 하고 행동 지시 없이 문장을 끝내면 "
            "안 된다 — 마지막 문장에 반드시 리더가 지금 할 일이 나와야 한다."
        )
    return (
        "body의 마지막 문장은 반드시 물음표 없는 질문 형태(\"...인지 확인이 필요해 보입니다\" "
        "또는 \"...확인이 필요해 보입니다\")로 끝나야 한다. 단정적으로 결론짓거나 사실을 "
        "나열만 하고 끝내면 안 된다."
    )


def _names(c: dict) -> str:
    return ", ".join(dict.fromkeys(e["user_name"] for e in c["evidence"]))


def _template_headline(c: dict) -> str:
    t, names, obj = c["card_type"], _names(c), c.get("canonical") or "같은 대상"
    if t == "dependency_bottleneck":
        return f"{c['affected_count']}명이 '{obj}'을(를) {c['duration_days']}일째 기다리는 병목입니다."
    if t == "unresolved_request":
        return f"{names} 님의 요청이 {c['duration_days']}일째 후속 없이 남아 있습니다."
    if t == "rework_loop":
        return f"{names} 님의 작업이 반복적으로 수정되고 있습니다."
    if t == "load_imbalance":
        return f"{names} 님의 업무량이 팀 평균보다 눈에 띄게 많습니다."
    if t == "unrecognized_work":
        return f"{names} 님의 성과가 따로 언급되지 않았습니다."
    return f"{names} 님 관련 확인이 필요한 사안이 있습니다."


def _template_body(c: dict) -> str:
    t, names, obj, high = c["card_type"], _names(c), c.get("canonical") or "같은 대상", c.get("confidence") == "high"
    if t == "dependency_bottleneck":
        if high:
            return f"{names} 님의 작업이 '{obj}' 대기로 멈춰 있습니다. 해당 승인을 확정하시면 후속 작업이 재개됩니다."
        return f"{names} 님이 '{obj}' 관련해 대기 중인지, 리더가 풀어줄 수 있는 병목인지 확인이 필요해 보입니다."
    if t == "unresolved_request":
        if high:
            return f"{names} 님이 도움/결정을 요청한 뒤 별다른 후속 진행이 보이지 않습니다. 요청 내용을 확인해 응답을 주시면 지연이 해소됩니다."
        return f"{names} 님의 요청이 아직 처리 중인지, 놓친 부분은 없는지 확인이 필요해 보입니다."
    if t == "rework_loop":
        if high:
            return f"{names} 님의 작업이 반복적으로 수정·재작업되고 있습니다. 개인 역량 문제라기보다 초기 스펙/요구사항이 불명확할 가능성이 있어 확인이 필요합니다."
        return f"{names} 님의 반복 수정이 스펙 불명확 때문인지 확인이 필요해 보입니다."
    if t == "load_imbalance":
        if high:
            return f"{names} 님의 최근 업무량이 팀 평균 대비 눈에 띄게 많습니다. 업무 배분을 재검토하시면 도움이 될 것 같습니다."
        return f"{names} 님의 업무 배분이 팀 평균과 비교해 편중되어 있는지 확인이 필요해 보입니다."
    if t == "unrecognized_work":
        return f"{names} 님이 눈에 띄는 성과를 냈는데 별도로 언급되지 않았습니다. 1on1이나 채널에서 짚어주시면 좋을 것 같습니다."
    return f"{names} 님 관련해 확인이 필요해 보입니다."


def generate_card_text(state: dict) -> dict:
    """확신도별 문형으로 headline/body 생성 (§8-3).

    state['llm'] 이 있으면 LLM으로 서술(grounded=false면 폐기), 없으면 코드 템플릿으로 서술한다
    (오프라인/무API 실행 지원). 어느 경우든 근거·인원·점수·확신도는 코드가 이미 확정한 값이다.
    """
    llm = state.get("llm")
    cards = []
    for i, c in enumerate(state.get("candidates", []), 1):
        if llm is not None:
            ev_block = "\n".join(f"- {e['user_name']}: {e['excerpt']}" for e in c["evidence"])
            res = llm(CARD_TEXT_PROMPT.format(
                confidence=c["confidence"], tone=_tone(c["confidence"]),
                card_type=CARD_TYPE_LABEL_KO.get(c["card_type"], c["card_type"]),
                affected_count=c["affected_count"], duration_days=c["duration_days"], evidence=ev_block)) or {}
            if res.get("grounded") is False:
                continue
            headline = (res.get("headline") or "").strip() or _template_headline(c)
            body = (res.get("body") or "").strip() or _template_body(c)
        else:
            headline, body = _template_headline(c), _template_body(c)
        cards.append(CoachingCard(
            card_id=f"card_{i:03d}", card_type=c["card_type"],
            priority_score=c.get("priority_score", 0.0), confidence=c["confidence"],
            headline=headline, body=body, subjects=c["subjects"],
            evidence=[Evidence(**e) for e in c["evidence"]],
            affected_count=c["affected_count"], duration_days=c["duration_days"],
        ))
    return {"cards": cards}


def split_urgent(state: dict) -> dict:
    """긴급 조건 재확인 + 분리(§7-5): 영향 3명↑ & 지속 5일↑ 이거나 릴리스/납기 키워드 포함
    → 즉시 DM 경로.

    **select_top_n(개수 제한) 이전에** 실행한다 — 원래는 generate(LLM 서술) 뒤에 있었는데,
    그러면 진짜 긴급한 후보도 그 주 다른 후보에 밀려 select_top_n에서 개수 제한에 걸려
    조용히 잘려나갈 수 있었다(긴급은 격주 카드 개수 제한과 무관하게 나가야 하는데도).
    그래서 아직 LLM 서술 전인 원시 후보(dict)를 다루고, 긴급으로 분리된 건은 LLM을
    기다리지 않고 코드 템플릿으로 바로 서술한다 — 긴급 알림은 즉시 나가야 하므로 LLM
    호출/실패에 발이 묶이면 안 된다.

    릴리스 키워드 체크는 '문제' 후보에만 적용한다 — 미인지 성과(인정) 후보는 "배포 완료"처럼
    같은 단어를 좋은 소식으로 쓸 수 있고, 그런 경우까지 즉시 긴급 알림으로 보내면 안 된다.
    """
    detected_at = max((w.timestamp for w in state.get("work_logs", [])), default=datetime(2026, 1, 1))
    candidates, urgent = [], []
    for c in state.get("candidates", []):
        release_kw = (c["card_type"] in PROBLEM_TYPES
                      and any(any(k in e["excerpt"] for k in URGENT_KEYWORDS) for e in c["evidence"]))
        is_urgent = (c["affected_count"] >= 3 and c["duration_days"] >= 5) or release_kw
        if is_urgent:
            urgent.append(UrgentAlert(
                alert_id=f"urgent_{len(urgent)+1:03d}", card_type=c["card_type"],
                reason=("영향 3명 이상·5일 이상 지속" if c["affected_count"] >= 3 and c["duration_days"] >= 5
                        else "릴리스/납기 관련 병목"),
                headline=_template_headline(c),
                subjects=c["subjects"], evidence=[Evidence(**e) for e in c["evidence"]],
                detected_at=detected_at))
        else:
            candidates.append(c)
    return {"candidates": candidates, "urgent_alerts": urgent}


def finalize_urgent(state: dict) -> dict:
    """detect_blocked_escalation 이 만든 즉시-알림(이미 완성된 UrgentAlert)을 split_urgent 가
    만든 urgent_alerts 에 합친다. 카드에서 파생된 긴급 경로와 상태(blocked) 기반 긴급 경로,
    두 병렬 흐름이 여기서 다시 만난다."""
    urgent = list(state.get("urgent_alerts", [])) + list(state.get("blocked_escalation_candidates", []))
    return {"urgent_alerts": urgent}
