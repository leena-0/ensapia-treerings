# -*- coding: utf-8 -*-
"""
Slack Block Kit 빌더.

- 홈 탭(build_home_view/build_goal_dashboard_blocks): "오늘의 현황"을 항상 보여주는 정적
  대시보드. LLM을 호출하지 않고 goals/sub_goals 등을 그 자리에서 읽어 렌더링하므로
  report_cache 유무와 무관하게 항상 최신 상태를 보여준다. 하위 목표(건물)는 레몬베이스
  원본 하위 목표를 mock으로 대체한 것으로, 정성 목표 마일스톤(goal_milestones, HR 평가
  근거용)과는 완전히 별개 개념이다 -- 헷갈리지 말 것 (data_dictionary.md 17절/18절 참고).
- 개인 주간 리포트/팀 코칭 카드(build_personal_blocks/build_coaching_blocks): 캐시된 LLM
  결과를 블록으로 렌더링한다. 이 두 개는 홈 탭이 아니라 DM 메시지 전송용으로 쓰인다
  (send_weekly_dm.py 등에서 재사용).
"""

import json
import os
from datetime import date

from reports import cache_store
from reports.collaboration import load_wish_pending, member_collaboration_summary
from reports.data_access import DATA_DIR
from reports.generate_reports import QUARTER_END, QUARTER_START
from reports.progress import subgoal_stage

CITY_IMAGE_CONFIG_PATH = os.path.join(DATA_DIR, "home_city_image.json")
LEADER_ROLES = ("1차평가자", "2차평가자")

_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


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


def _load_city_image_file_id():
    if not os.path.exists(CITY_IMAGE_CONFIG_PATH):
        return None
    with open(CITY_IMAGE_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f).get("file_id")


# 크레인 상태 임계값(영업일, PDF 기획서 6-2절): 작업중 0~2 / 멈춤 3~9 / 장기중단 10+
CRANE_STOPPED_FROM = 3
CRANE_WITHDRAWN_FROM = 10


def _crane_state(business_days_since):
    """작업 기록 없음 경과 영업일수를 기준으로 크레인 상태 3단계를 판정한다.
    화면이 실제와 다르면 시스템 전체 신뢰도가 흔들린다는 원칙(6-2절/12절 리스크)에 따라,
    며칠째 손대지 않은 항목을 계속 '작업 중'으로 보여주지 않는다."""
    if business_days_since is None or business_days_since <= CRANE_STOPPED_FROM - 1:
        return "🏗", "작업 중"
    if business_days_since < CRANE_WITHDRAWN_FROM:
        return "⏸", f"멈춤 · {business_days_since}영업일째 작업 없음"
    return "🏚", f"장기 중단 · {business_days_since}영업일째 작업 없음 (크레인 철수)"


# 본인이 주간 확인 요청에서 선택한 상태 -> 크레인 표시 오버라이드 (PDF 기획서 6-2절 표 그대로).
# 자동 판정(freshness)보다 본인 선택이 우선한다 -- 단, 이 오버라이드보다 더 최근 작업 로그가
# 있으면(재개) 자동 판정이 다시 이긴다 (_subgoal_display 에서 날짜 비교로 처리).
CHECKIN_OVERRIDE_DISPLAY = {
    "진행중": ("🏗", "작업 중 (기록엔 안 남았지만 본인 확인)"),
    "대기": ("⏸", "대기 중 (타인·외부 요인, 본인 표시)"),
    "보류": ("🏚", "보류 (본인 의도적 중단)"),
    "막힘": ("⏸", "막힘 (문제 있음, 리더 확인 필요)"),
}


def _subgoal_display(sg, p, store=None):
    """
    계량기(비율/최대치 추정)가 아니라 계수기 원칙: 여기서 보여주는 stage/worked_days는 전부
    subgoal_stage()가 실제 로그를 세어 계산한 사실이고, "완료" 여부만 본인/리더가 명시적으로
    확정한 상태(sg['status'])다. 5단계(25영업일)를 다 채워도 본인이 신고하기 전까지는 건물
    높이가 5단계에서 멈춘 채 크레인만 계속 떠 있다(공사중 표시 유지) -- 자동으로 완공되지 않는다.
    """
    if sg["status"] == "확정완료":
        return "✅", "완공"
    if sg["status"] == "본인완료":
        return "🟡", "리더 확인 대기 중"
    if p["stage"] == 0:
        return "⬜", "_미착수_"

    checkin = store.latest_checkin(sg["sub_goal_id"]) if store else None
    if checkin and (p["last_date"] is None or checkin["reported_at"][:10] >= p["last_date"]):
        icon, state_text = CHECKIN_OVERRIDE_DISPLAY.get(checkin["status"]) or _crane_state(p["business_days_since"])
    else:
        icon, state_text = _crane_state(p["business_days_since"])

    if p["stage"] >= p["total_stages"]:
        return icon, f"{p['stage']}/{p['total_stages']}단계 (누적 {p['worked_days']}일) · {state_text} · 본인 완료 신고 대기"
    return icon, f"{p['stage']}/{p['total_stages']}단계 (누적 {p['worked_days']}일) · {state_text}"


def _subgoal_lines(goal, store):
    lines = [f"*{goal['title']}*"]
    sub_goals = store.sub_goals(goal["goal_id"])
    if not sub_goals:
        lines.append("    _등록된 하위 목표 없음_")
        return "\n".join(lines)
    for sg in sub_goals:
        p = subgoal_stage(store, sg["sub_goal_id"])
        icon, text = _subgoal_display(sg, p, store)
        lines.append(f"    {icon} {sg['title']} — {text}")
    return "\n".join(lines)


def _pending_confirmations_for_leader(member, store):
    """이 리더가 실제로 확정할 수 있는 '본인완료' 하위 목표 목록.
    본인 소유 목표는 제외한다 -- 리더도 본인 하위 목표는 스스로 확정 못 하고 팀의 다른 리더가
    확정해야 하므로(confirm_subgoal.py 의 본인 확정 금지 규칙과 동일 기준), 여기서도 본인 것은
    "내가 처리할 건"에서 빼야 앞뒤가 맞는다."""
    pending = []
    for mid in store.members_by_team.get(member["team"], []):
        if mid == member["member_id"]:
            continue
        owner = store.members_by_id[mid]
        for goal in store.member_goals(mid):
            for sg in store.sub_goals(goal["goal_id"]):
                if sg["status"] == "본인완료":
                    pending.append((owner, goal, sg))
    return pending


def _notification_blocks(member, store):
    member_id = member["member_id"]
    blocks = []

    latest_weekly = cache_store.latest("personal_weekly", member_id)
    if latest_weekly is not None:
        last_seen = store.last_seen_personal_weekly(member_id)
        if last_seen is None or latest_weekly["generated_at"] > last_seen:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "📄 새 주간 리포트 도착"},
                "accessory": {"type": "button", "text": {"type": "plain_text", "text": "열기"},
                               "style": "primary", "action_id": "weekly_report_open"},
            })

    if member["role"] in LEADER_ROLES:
        pending = _pending_confirmations_for_leader(member, store)
        if pending:
            detail = "\n".join(f"    · {owner['name']} — {goal['title']}: {sg['title']}" for owner, goal, sg in pending)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🏗 확정 대기 {len(pending)}건\n{detail}"},
                "accessory": {"type": "button", "text": {"type": "plain_text", "text": "처리"},
                               "action_id": "subgoal_confirm_open"},
            })

    pending_all = load_wish_pending()
    my_pending = [r for r in pending_all if r.get("stuck_member_id") == member_id and r.get("status") == "pending"]
    received = [r for r in pending_all if r.get("helper_member_id") == member_id and r.get("status") == "confirmed"]
    blocks.append(_section(f"🌱 소원 — 내가 보낼 대기 {len(my_pending)}건 · 받은 요청 {len(received)}건"))

    return blocks


def _contribution_line(member, store):
    """
    도시 이미지 범례 기준: 역(🚉, 같은 팀 협업) / 항구(⚓, 다른 팀 협업) / 정원(🌱, 내가 도운 것 -
    단방향). reports.collaboration.member_collaboration_summary()가 wish_match.py(confirmed)
    기록에서 계산한 값을 그대로 쓴다 (평가 근거 패키지와 동일 로직 공유, 중복 방지).
    """
    summary = member_collaboration_summary(member["member_id"], store, QUARTER_START, QUARTER_END)
    return _context(f"🚉 역 {summary['same_team_count']}(같은 팀 협업) · "
                     f"⚓ 항구 {summary['other_team_count']}(다른 팀 협업) · "
                     f"🌱 정원 {len(summary['helped'])}(내가 도운 것)")


def build_goal_dashboard_blocks(member, store, quarter, display_name=None):
    """
    홈 탭 본문: "오늘의 현황" 대시보드. 5개 섹션 순서: 헤더+날짜 / 진행 중인 목표(하위목표 건물
    체크리스트) / 알림 / 기여 요약 / 내 도시(이미지). goal 타입(정량/정성) 구분 없이 모든 목표를
    "상위목표 -> 하위목표(건물)" 형태로 통일해서 보여준다(구 버전의 "구획 X/Y"/마일스톤
    체크리스트 표시는 여기서 더 이상 쓰지 않음 -- goal_milestones 데이터/기능 자체는 그대로
    살아있고 리포트/평가 근거 패키지에서는 계속 쓰인다).
    """
    display_name = display_name or member["name"]
    goals = store.member_goals(member["member_id"])
    today = date.today()

    blocks = [
        _header("오늘의 현황"),
        # Block Kit에 완전한 좌/우 정렬은 없어서 section의 fields(2열 그리드)로 근사한다.
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*{display_name}님*"},
            {"type": "mrkdwn", "text": f"{today.month}월 {today.day}일 ({_WEEKDAY_KR[today.weekday()]})"},
        ]},
        _divider(),
        _header("🎯 진행 중인 목표"),
    ]

    if not goals:
        blocks.append(_section("_이번 분기에 등록된 목표가 없습니다._"))
    for goal in goals:
        blocks.append(_section(_subgoal_lines(goal, store)))

    blocks.append(_divider())
    blocks.append(_header("📢 알림"))
    notif_blocks = _notification_blocks(member, store)
    if notif_blocks:
        blocks.extend(notif_blocks)
    else:
        blocks.append(_section("_새 알림이 없습니다._"))

    blocks.append(_contribution_line(member, store))
    blocks.append(_divider())
    blocks.append(_header("🏙️ 내 도시"))

    file_id = _load_city_image_file_id()
    if file_id:
        blocks.append({"type": "image", "slack_file": {"id": file_id}, "alt_text": f"{display_name}님의 도시"})
    else:
        blocks.append(_context("_도시 이미지가 아직 업로드되지 않았습니다._"))

    return blocks


def build_home_view(member, store, quarter, display_name=None):
    blocks = build_goal_dashboard_blocks(member, store, quarter, display_name=display_name)
    blocks.append(_divider())
    blocks.append(_context("하위 목표(건물): 본인이 완료 신고하면 리더 확인 대기 상태가 되고, "
                            "리더가 확정해야 최종 완공으로 반영됩니다. "
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


_CHECKIN_STATUS_OPTIONS = ("진행중", "대기", "보류", "막힘")


def _checkin_select_block(item, week_start, week_end):
    """확인 요청(§5-1 "이 시스템의 관문"): 이번 주 언급 없던 하위 목표에 대해 본인이 직접
    진행중/대기/보류/막힘 중 하나를 고른다. block_id 에 sub_goal_id/주차를 실어서
    socket_app.py 의 액션 핸들러가 어떤 건물의 어느 주차 체크인인지 복원할 수 있게 한다."""
    sub_goal_id = item["sub_goal_id"]
    block_id = f"checkin|{sub_goal_id}|{week_start}|{week_end}"
    options = [
        {"text": {"type": "plain_text", "text": label}, "value": label}
        for label in _CHECKIN_STATUS_OPTIONS
    ]
    element = {
        "type": "static_select",
        "action_id": "subgoal_checkin_select",
        "placeholder": {"type": "plain_text", "text": "상태 선택"},
        "options": options,
    }
    current = item.get("current_status")
    if current in _CHECKIN_STATUS_OPTIONS:
        element["initial_option"] = {"text": {"type": "plain_text", "text": current}, "value": current}

    text = f"*{item['title']}* (`{sub_goal_id}`) — {item['reminder']}"
    if current:
        text += f"\n_현재 선택: {current} (다시 고르면 정정됩니다)_"
    return {"type": "section", "block_id": block_id, "text": {"type": "mrkdwn", "text": text}, "accessory": element}


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

        milestones = gp.get("milestones") or []
        milestone_text = ""
        if milestones:
            icon = {"완료": "✅", "진행중": "🚧", "미착수": "⬜"}
            m_lines = [
                f"    {icon.get(m['status'], '•')} {m['status']} — {m['title']}"
                + (f"  ·  본인 보고 {m['self_reported_at'][:10]}" if m.get("self_reported_at") else "")
                for m in milestones
            ]
            milestone_text = "\n*마일스톤 (본인 보고, 검증 아님)*\n" + "\n".join(m_lines) + "\n"

        blocks.append(_section(
            f"*🎯 {gp.get('title', '')}* (`{gp.get('goal_id', '')}`)\n"
            f"{gp.get('kpi_causal_summary', '')}\n"
            f"_{progress_line}_\n"
            f"근거: {cite_text}\n"
            f"{milestone_text}"
        ))

    nwp = content.get("next_week_priorities", [])
    week_start = content.get("week_start", "?")
    week_end = content.get("week_end", "?")

    blocks += [
        _divider(),
        _section("*🚧 이슈/도움 요청*\n" + _bullets_with_citations(
            content.get("issues", []), permalinks, "(이번 주 이슈 없음)")),
        _divider(),
        _header("✅ 확인 요청"),
        _context("이번 주 언급 없었던 하위 목표입니다. 실제 상태를 골라주세요 -- "
                  "\"막힘\"으로 표시한 것만 리더 코칭 카드로 전달됩니다."),
    ]
    if not nwp:
        blocks.append(_section("_이번 주 모든 하위 목표가 언급됐습니다._"))
    for item in nwp:
        blocks.append(_checkin_select_block(item, week_start, week_end))
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
