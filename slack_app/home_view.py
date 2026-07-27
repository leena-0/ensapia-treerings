# -*- coding: utf-8 -*-
"""
Slack Block Kit 빌더.

- 홈 탭(build_home_view/build_goal_dashboard_blocks): 목표/KPI 현황을 "항상" 보여주는
  정적 대시보드. LLM을 호출하지 않고 goals/kpis/goal_kpi_link CSV를 그 자리에서 읽어
  렌더링하므로 report_cache 유무와 무관하게 항상 최신 상태를 보여준다.
- 개인 주간 리포트/팀 코칭 카드(build_personal_blocks/build_coaching_blocks): 캐시된 LLM
  결과를 블록으로 렌더링한다. 이 두 개는 홈 탭이 아니라 DM 메시지 전송용으로 쓰인다
  (send_weekly_dm.py 등에서 재사용).
"""

from reports.progress import goal_stage


def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _header(text):
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def _context(text):
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _divider():
    return {"type": "divider"}


def _bullets(items, empty_text="(없음)"):
    if not items:
        return empty_text
    return "\n".join(f"• {item}" for item in items)


def build_goal_dashboard_blocks(member, store, quarter, display_name=None):
    """
    홈 탭 본문: 이 멤버의 목표 현황을 항상 보여주는 정적 대시보드.
    KPI 원시 수치 대신 "구획 진행률"(누적 업무일지 작성일 / 5) + 최근 작업 여부를 보여준다
    (data_dictionary.md 12절). AI는 이 값을 추정하지 않는다 -- reports/progress.py 가
    slack_logs 를 세어 결정론적으로 계산한다.
    """
    display_name = display_name or member["name"]
    goals = store.member_goals(member["member_id"])

    blocks = [
        _header(f"📍 {display_name}님의 목표"),
        _context(f"{member['team']} · {member['role']} · {quarter}"),
        _divider(),
    ]

    if not goals:
        blocks.append(_section("_이번 분기에 등록된 목표가 없습니다._"))
        return blocks

    lines = []
    for goal in goals:
        p = goal_stage(store, goal["goal_id"])
        lines.append(f"• *{goal['title']}* — 구획 {p['stage']}/{p['total_stages']} · {p['freshness']}")
    blocks.append(_section("\n".join(lines)))

    return blocks


def build_home_view(member, store, quarter, display_name=None):
    blocks = build_goal_dashboard_blocks(member, store, quarter, display_name=display_name)
    blocks.append(_divider())
    blocks.append(_context("구획: 업무일지가 실제로 작성된 날짜 누적 5일마다 1단계 (연속이 아니라 누적). "
                            "주간 리포트/팀 코칭 카드는 메시지로 따로 전송됩니다."))
    return {"type": "home", "blocks": blocks}


def _log_ref(log_id, permalinks):
    """log_id 를 permalink 가 있으면 클릭 가능한 링크로, 없으면(목데이터 등) 맨 텍스트로."""
    url = (permalinks or {}).get(log_id)
    return f"<{url}|{log_id}>" if url else log_id


def _bullets_with_citations(items, permalinks=None, empty_text="(없음)"):
    if not items:
        return empty_text
    lines = []
    for item in items:
        log_ids = item.get("log_ids") or []
        cite = f" _({', '.join(_log_ref(lid, permalinks) for lid in log_ids)})_" if log_ids else ""
        lines.append(f"• {item.get('text', '')}{cite}")
    return "\n".join(lines)


def build_personal_blocks(member, cache_record, display_name=None, permalinks=None):
    display_name = display_name or member["name"]
    if cache_record is None:
        return [_section(f"*{display_name}님의 개인 주간 리포트*"), _context("아직 생성된 리포트가 없습니다.")]

    content = cache_record["content"]
    if content.get("_parse_error"):
        return [_section("*개인 주간 리포트*"), _context("⚠️ LLM 응답 파싱 실패 - 재생성이 필요합니다.")]

    blocks = [
        _header(f"📋 {display_name}님의 주간 리포트"),
        _context(f"{content.get('week_start', '?')} ~ {content.get('week_end', '?')}"
                  f"  |  model: {cache_record.get('model', '?')}"
                  + ("  |  ⚠️ 로컬 LLM 폴백 사용" if cache_record.get("used_fallback") else "")),
        _divider(),
        _section("*이번 주 핵심 성과*\n" + _bullets_with_citations(content.get("highlights", []), permalinks)),
    ]

    for gp in content.get("goal_progress", []):
        p = gp.get("progress") or {}
        progress_line = (f"구획 {p.get('stage', '?')}/{p.get('total_stages', '?')} "
                          f"(누적 {p.get('worked_days', '?')}일 작업) · {p.get('freshness', '')}") if p else ""
        citations = gp.get("citations", [])
        cite_text = " · ".join(
            f"{_log_ref(c.get('log_id'), permalinks)}({c.get('date')})" for c in citations
        ) or "인용 없음"
        blocks.append(_section(
            f"*🎯 {gp.get('title', '')}* (`{gp.get('goal_id', '')}`)\n"
            f"{gp.get('kpi_causal_summary', '')}\n"
            f"_{progress_line}_\n"
            f"근거: {cite_text}"
        ))

    nwp = content.get("next_week_priorities", [])
    nwp_text = "\n".join(f"• *{r['title']}* (`{r['goal_id']}`) — {r['reminder']}" for r in nwp) \
        or "(이번 주 모든 목표가 언급됨)"

    blocks += [
        _divider(),
        _section("*🚧 이슈/도움 요청*\n" + _bullets_with_citations(
            content.get("issues", []), permalinks, "(이번 주 이슈 없음)")),
        _context("다음 주 우선순위는 시스템이 계산합니다 (이번 주 언급 없었던 목표 리마인드) --------"),
        _section("*➡️ 다음 주 우선순위 (리마인드)*\n" + nwp_text),
        _section("*🗣️ 1on1 아젠다*\n" + _bullets_with_citations(
            content.get("one_on_one_agenda", []), permalinks, "(이번 주 안건 없음)")),
    ]
    return blocks


_CATEGORY_BADGE = {"개별 이슈": "🟢 개별 이슈", "구조 이슈": "🟠 구조 이슈"}


def _card_block(card):
    badge = _CATEGORY_BADGE.get(card.get("category"), card.get("category", ""))
    members = ", ".join(card.get("member_ids", []))
    evidence = card.get("evidence", [])
    ev_text = " · ".join(f"{e.get('log_id')}({e.get('date')})" for e in evidence) or "근거 없음"

    return [
        _context(f"{badge}  |  대상: {members or '-'}"),
        _section(card.get("summary", "")),
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "채택"}, "style": "primary",
                 "action_id": "coaching_card_adopt", "value": card.get("card_id", "")},
                {"type": "button", "text": {"type": "plain_text", "text": "필요없음"},
                 "action_id": "coaching_card_dismiss", "value": card.get("card_id", "")},
            ],
        },
        _context(f"근거: {ev_text}"),
        _divider(),
    ]


def build_coaching_blocks(team, cache_record):
    if cache_record is None:
        return []

    content = cache_record["content"]
    if content.get("_parse_error"):
        return [_divider(), _section(f"*{team} 팀 코칭 카드*"), _context("⚠️ LLM 응답 파싱 실패")]

    blocks = [
        _divider(),
        _header(f"🧭 {team} 팀 코칭 카드"),
        _context(f"기간: {content.get('period', '?')}  |  model: {cache_record.get('model', '?')}"
                  + ("  |  ⚠️ 로컬 LLM 폴백 사용" if cache_record.get("used_fallback") else "")),
        _context("AI가 팀원들의 최근 업무일지를 읽고 제안했어요. 채택하거나 나중으로 미뤄보세요. "
                  "(⚠️ 버튼 클릭은 아직 서버에 연결되지 않아 반응하지 않습니다 -- 인터랙티비티 엔드포인트 배포 후 연동 예정)"),
        _divider(),
    ]

    cards = content.get("cards", [])
    if not cards:
        blocks.append(_section("_이번 주 제안할 카드가 없습니다._"))
    for card in cards:
        blocks += _card_block(card)

    qual = content.get("qualitative_goals_checkin", [])
    if qual:
        blocks.append(_section("*정성 목표 - 정기 체크인*\n" + _bullets(
            [f"{q.get('goal_id')} ({q.get('member_id')}) {q.get('title')} - {q.get('note')}" for q in qual]
        )))
    return blocks


# ---------------------------------------------------------------------------
# 신규 코칭 카드 파이프라인 (coaching/, 구현명세_코칭카드) 전용 블록.
# 위 build_coaching_blocks 는 구 reports.generate_reports 캐시 스키마(category/summary)용이고,
# 아래는 coaching.schemas.CoachingCard/UrgentAlert(headline/body/confidence/evidence) 전용이다.
# ---------------------------------------------------------------------------
_CONFIDENCE_BADGE = {"high": "🟢 확신도 높음", "medium": "🟡 확신도 중간"}
_CARD_TYPE_LABEL = {
    "dependency_bottleneck": "의존 병목",
    "unresolved_request": "미해결 요청",
    "rework_loop": "재작업 반복",
    "load_imbalance": "부하 편중",
    "unrecognized_work": "미인지 성과",
}
# 확신도별 카드 색상 (Slack Block Kit 자체는 카드마다 배경/테두리색을 못 주므로, 레거시
# attachments 의 color 바를 빌려 카드마다 왼쪽에 색상 띠를 붙인다 — Slack에서 유일하게
# 메시지 안 요소별로 색을 다르게 줄 수 있는 방법).
_CONFIDENCE_COLOR = {"high": "#2ecc71", "medium": "#f39c12"}
_URGENT_COLOR = "#e74c3c"


def _ev_ref(e):
    """실제 Slack permalink(https)면 클릭 가능한 링크로, 목데이터(log://...)면 이름만 표시."""
    return f"<{e.permalink}|{e.user_name}>" if e.permalink.startswith("http") else e.user_name


def _coaching_card_v2_block(card):
    badge = _CONFIDENCE_BADGE.get(card.confidence, card.confidence)
    label = _CARD_TYPE_LABEL.get(card.card_type, card.card_type)
    ev_text = " · ".join(f"{_ev_ref(e)}({e.excerpt})" for e in card.evidence) or "근거 없음"

    return [
        _context(f"{badge}  |  {label}  |  영향 {card.affected_count}명 · {card.duration_days}일째"),
        _section(f"*{card.headline}*\n{card.body}"),
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "채택"}, "style": "primary",
                 "action_id": "coaching_card_adopt", "value": card.card_id},
                {"type": "button", "text": {"type": "plain_text", "text": "기각"},
                 "action_id": "coaching_card_dismiss", "value": card.card_id},
            ],
        },
        _context(f"근거: {ev_text}"),
    ]


def build_coaching_cards_blocks(team, period_start, period_end, cards, narration="template"):
    """격주 배치 코칭 카드 DM 본문 (coaching.graph.run_coaching 의 cards 결과).

    반환: {"blocks": 상단 헤더/설명, "attachments": 카드마다 확신도색 바를 두른 블록 목록}.
    Slack Block Kit 메시지는 항상 세로로만 쌓이므로(가로 배치 불가), 카드를 attachments 로
    하나씩 분리해 색상 띠로 서로 다른 카드임을 시각적으로 구분한다.
    호출부에서 chat_postMessage(blocks=결과["blocks"], attachments=결과["attachments"]) 로 넘긴다.
    """
    header_blocks = [
        _header(f"🧭 {team} 팀 코칭 카드"),
        _context(f"기간: {period_start} ~ {period_end}  |  서술: "
                  f"{'실제 LLM(Gemini)' if narration == 'llm' else '코드 템플릿(오프라인)'}"),
        _context("AI가 팀원들의 최근 업무일지를 분석해 제안했어요. 카드 왼쪽 색으로 확신도를 구분합니다"
                  "(🟢 초록=높음, 🟠 주황=중간). 채택하거나 기각해보세요. "
                  "(⚠️ 버튼 클릭은 아직 서버에 연결되지 않아 반응하지 않습니다 -- 인터랙티비티 엔드포인트 배포 후 연동 예정)"),
    ]
    if not cards:
        return {"blocks": header_blocks + [_section("_이번 주기에 제안할 카드가 없습니다._")], "attachments": []}

    attachments = [
        {"color": _CONFIDENCE_COLOR.get(card.confidence, "#95a5a6"), "blocks": _coaching_card_v2_block(card)}
        for card in cards
    ]
    return {"blocks": header_blocks, "attachments": attachments}


def build_urgent_alert_blocks(team, alert):
    """긴급 알림 DM 본문 — 격주 배치를 기다리지 않고 개별 메시지로 즉시 발송한다(명세 §7-5).
    반환 형태는 build_coaching_cards_blocks 와 동일(blocks/attachments 분리)."""
    ev_text = "\n".join(f"• {_ev_ref(e)}: {e.excerpt}" for e in alert.evidence) or "근거 없음"
    card_blocks = [
        _context(f"사유: {alert.reason}  |  탐지 시각: {alert.detected_at:%Y-%m-%d %H:%M}"),
        _section(f"*{alert.headline}*"),
        _section(f"근거:\n{ev_text}"),
        _context(f"대상: {', '.join(alert.subjects)}"),
    ]
    return {
        "blocks": [_header(f"🚨 {team} 팀 긴급 알림")],
        "attachments": [{"color": _URGENT_COLOR, "blocks": card_blocks}],
    }
