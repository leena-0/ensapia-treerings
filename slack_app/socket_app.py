#!/usr/bin/env python3
"""
Slack Socket Mode 이벤트 리스너 -- 이 프로젝트 최초의 "상시 실행" 프로세스.

이전까지는 홈탭/리포트/발송이 전부 필요할 때 수동으로 돌리는 1회성 스크립트였다. 하지만 홈탭이
"열 때마다 최신 상태로 자동 갱신"되려면(예: slack_user_map.json 매핑이 바뀌거나, 마일스톤/
하위목표 상태가 바뀐 직후) 그 순간 이벤트를 실시간으로 받아야 한다 -- 그래서 이 프로세스가 필요하다.

Socket Mode를 쓰는 이유: 공인 HTTPS 엔드포인트(도메인+인증서, 인터랙티비티 서버)가 없어도
웹소켓으로 이벤트를 받을 수 있어서, 로컬 개발이든 GCE VM이든 추가 인프라 없이 바로 돌릴 수 있다.

필요 환경변수:
  SLACK_BOT_TOKEN  (기존과 동일)
  SLACK_APP_TOKEN  (xapp-..., Socket Mode용 App-Level Token. Slack 앱 설정 > Socket Mode에서 발급,
                    connections:write 스코프 필요)

개인 주간 리포트 "확인 요청"(action_id=subgoal_checkin_select): 이번 주 언급 없던 하위목표에
대해 본인이 진행중/대기/보류/막힘을 select 메뉴로 고르면 여기서 받아 subgoal_weekly_checkin
테이블에 기록한다(reports/data_access.py record_subgoal_checkin). Socket Mode라 이것도 별도
HTTPS 엔드포인트 없이 그대로 동작한다 -- 코칭 카드/건물 확정 버튼이 "아직 서버가 없어 장식용"
이었던 것과 달리, 이 액션은 실제로 DB에 반영된다.

슬래시 명령어 `/평가근거 <이름 또는 member_id>`: 관리자가 매번 SQL을 직접 짜거나 스크립트를
돌리지 않아도, Slack에서 바로 그 사람의 평가 근거 패키지 PDF를 받아볼 수 있게 한다
(send_evidence_package.py의 _build_pdf()를 그대로 재사용 -- 캐시된 report_cache 내용을 PDF로
렌더링만 함, 이 명령어 자체는 LLM을 호출하지 않는다). Slack 앱 설정 > Slash Commands 에서
`/평가근거` 커맨드를 등록해야 한다 (Socket Mode 사용 중이라 Request URL은 아무 값이나 넣어도 됨 --
실제 전달은 웹소켓으로 받는다).

사용 예:
  python3 -m slack_app.socket_app
"""

import os
import re

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from env_loader import load_env
from reports.data_access import DataStore, record_subgoal_checkin
from slack_app import wish_match
from slack_app.home_view import build_home_view
from slack_app.member_map import display_name_for, load_member_map, member_to_slack_id
from slack_app.send_evidence_package import _build_pdf

QUARTER = "2026-Q2"
LEADER_ROLES = ("1차평가자", "2차평가자")

# 명칭 미확정(2026-07-28 기준, 김재우님 제안: /지원요청 또는 /협업요청) -- 확정되면 이 한 줄과
# Slack 앱 설정의 Slash Commands 등록만 바꾸면 된다. 내부 코드/데이터(wish_match.py, "소원"
# 관련 필드명)는 이번 리네이밍 범위에 넣지 않았다 -- 사용자에게 보이는 문구가 아니라 구현
# 세부사항이라 이름이 바뀌어도 그대로 둬도 무방하기 때문.
WISH_COMMAND = "/지원요청"

load_env()
app = App(token=os.environ["SLACK_BOT_TOKEN"])


def _resolve_member(store, query):
    """member_id("M07") 또는 이름(표시 이름 포함)으로 멤버 하나를 찾는다. 못 찾으면 None."""
    query = query.strip()
    upper = query.upper()
    if upper in store.members_by_id:
        return store.members_by_id[upper]
    for member in store.members:
        if query in (member["name"], display_name_for(member)):
            return member
    return None


@app.event("app_home_opened")
def handle_app_home_opened(event, client, logger):
    if event.get("tab") != "home":
        return  # 메시지 탭 열림 등은 무시, 홈탭 열림만 처리

    slack_user_id = event["user"]
    member_map = load_member_map()
    member_id = member_map.get(slack_user_id)
    if not member_id:
        logger.info(f"[skip] 매핑 안 된 Slack 사용자: {slack_user_id}")
        return

    store = DataStore()  # 매 이벤트마다 새로 읽어서 항상 최신 데이터로 렌더링 (캐시 안 씀)
    member = store.members_by_id.get(member_id)
    if member is None:
        logger.warning(f"[skip] 알 수 없는 member_id: {member_id}")
        return

    display_name = display_name_for(member)
    view = build_home_view(member, store, QUARTER, display_name=display_name)
    client.views_publish(user_id=slack_user_id, view=view)
    logger.info(f"[published] {display_name}({member_id}) 홈탭 자동 갱신 (app_home_opened)")


@app.action("subgoal_checkin_select")
def handle_subgoal_checkin(ack, action, body, respond, logger):
    ack()  # select 클릭은 3초 내 ack 필요 (버튼과 동일)

    block_id = action.get("block_id", "")
    parts = block_id.split("|")
    if len(parts) != 4 or parts[0] != "checkin":
        logger.warning(f"[skip] 알 수 없는 block_id: {block_id!r}")
        return
    _, sub_goal_id, week_start, week_end = parts
    status = action["selected_option"]["value"]

    slack_user_id = body["user"]["id"]
    member_map = load_member_map()
    member_id = member_map.get(slack_user_id)
    if not member_id:
        respond(text="본인 Slack 계정이 조직 멤버와 매핑되어 있지 않아 상태를 기록할 수 없습니다.",
                response_type="ephemeral", replace_original=False)
        return

    store = DataStore()
    # 본인 소유 하위목표인지 검증 -- 다른 사람의 건물에 대신 체크인하는 것을 막는다.
    sub_goal = next(
        (sg for g in store.member_goals(member_id) for sg in store.sub_goals(g["goal_id"])
         if sg["sub_goal_id"] == sub_goal_id),
        None,
    )
    if sub_goal is None:
        respond(text="본인 소유의 하위 목표가 아니라 상태를 기록할 수 없습니다.",
                response_type="ephemeral", replace_original=False)
        logger.warning(f"[denied] {member_id} -> {sub_goal_id} 체크인 시도 (소유자 아님)")
        return

    record_subgoal_checkin(
        sub_goal_id=sub_goal_id, week_start=week_start, week_end=week_end,
        member_id=member_id, status=status,
    )
    # replace_original=False 필수 -- 안 쓰면 Slack 기본값(response_url이 원본 메시지를 대체)이 적용돼
    # 개인 주간 리포트 전체(다른 확인 요청 select들 포함)가 이 확인 문구 한 줄로 사라져버린다.
    respond(text=f"✅ *{sub_goal['title']}* 상태를 *{status}*(으)로 기록했습니다."
                 + (" 리더 코칭 카드로 전달됩니다." if status == "막힘" else ""),
            response_type="ephemeral", replace_original=False)
    logger.info(f"[checkin] {member_id} -> {sub_goal_id} ({week_start}~{week_end}) = {status}")


@app.command("/평가근거")
def handle_evidence_command(ack, respond, command, client, logger):
    ack()  # Slack이 3초 내 ack를 요구하므로 먼저 응답하고 이후 처리를 계속한다

    query = command.get("text", "").strip()
    if not query:
        respond("사용법: `/평가근거 <이름 또는 member_id>` (예: `/평가근거 오승민` 또는 `/평가근거 M07`)")
        return

    store = DataStore()
    member_map = load_member_map()
    requester_id = member_map.get(command["user_id"])
    requester = store.members_by_id.get(requester_id) if requester_id else None
    if requester is None:
        respond("본인 Slack 계정이 조직 멤버와 매핑되어 있지 않아 이 명령어를 쓸 수 없습니다.")
        return

    target = _resolve_member(store, query)
    if target is None:
        respond(f"'{query}'에 해당하는 멤버를 찾을 수 없습니다. 이름 또는 member_id(M01~M15)로 입력해주세요.")
        return

    is_self = requester["member_id"] == target["member_id"]
    is_same_team_leader = requester["role"] in LEADER_ROLES and requester["team"] == target["team"]
    if not (is_self or is_same_team_leader):
        respond(f"권한이 없습니다 -- 본인 것이거나, {target['team']} 팀의 1차/2차평가자만 조회할 수 있습니다.")
        logger.info(f"[denied] {requester['name']}({requester['member_id']}) -> "
                     f"{target['name']}({target['member_id']}) 평가 근거 패키지 조회 거부")
        return

    built = _build_pdf(store, target["member_id"])
    if built is None:
        respond(f"{target['name']}님의 평가 근거 패키지가 아직 생성되지 않았습니다 "
                 f"(먼저 `reports.generate_reports --report evidence --members {target['member_id']}` 실행 필요).")
        return

    display_name, pdf_path = built
    client.files_upload_v2(
        channel=command["channel_id"],
        file=pdf_path,
        filename=f"{display_name}_평가근거패키지.pdf",
        title=f"{display_name}님 평가 근거 패키지",
        initial_comment=f"📄 {display_name}님의 평가 근거 패키지입니다.",
    )
    logger.info(f"[sent] {requester['name']} 조회 -> /평가근거 {query!r} -> {display_name}({target['member_id']})")


def _wish_button_blocks(wish_id, verb_yes, label_yes, verb_no=None, label_no=None, style_yes="primary"):
    """소원(지원요청) 흐름 전용 버튼 블록. action_id는 'wish_button_<verb>' 형태로 매번 다르게
    준다 -- Slack은 같은 메시지 안의 인터랙티브 요소들이 서로 다른 action_id를 갖도록 강제하며,
    위반 시 chat.postMessage 자체가 invalid_blocks 로 거부된다(실제로 겪은 버그). verb는
    handle_wish_button 에서 'wish_button_' 접두어를 뗀 나머지로 분기하므로 dispatch 로직은
    그대로 하나의 핸들러로 유지된다."""
    yes_button = {"type": "button", "action_id": f"wish_button_{verb_yes}",
                  "text": {"type": "plain_text", "text": label_yes}, "value": f"{verb_yes}:{wish_id}"}
    if style_yes:
        yes_button["style"] = style_yes
    elements = [yes_button]
    if verb_no:
        elements.append({"type": "button", "action_id": f"wish_button_{verb_no}",
                          "text": {"type": "plain_text", "text": label_no}, "value": f"{verb_no}:{wish_id}"})
    return [{"type": "actions", "block_id": f"wish|{wish_id}", "elements": elements}]


@app.command(WISH_COMMAND)
def handle_wish_command(ack, command, client, logger):
    ack()

    # respond()(response_url 기반 임시 응답)가 이 환경에서 배달이 안 되는 문제가 있어(원인
    # 미확정), /평가근거·wish_match.py의 배치 스캔이 이미 안정적으로 검증한 방식 그대로 --
    # client.conversations_open + chat_postMessage 로 DM을 직접 보낸다.
    dm_channel_id = client.conversations_open(users=[command["user_id"]])["channel"]["id"]

    def send(text=None, blocks=None):
        client.chat_postMessage(channel=dm_channel_id, text=text or " ", blocks=blocks)

    issue_text = command.get("text", "").strip()
    if not issue_text:
        send(f"사용법: `{WISH_COMMAND} <막힌 상황을 적어주세요>` "
             f"(예: `{WISH_COMMAND} 구형 iOS 기기에서 크래시가 나는데 원인을 못 찾겠어요`)")
        return

    store = DataStore()
    member_map = load_member_map()
    stuck_id = member_map.get(command["user_id"])
    stuck_member = store.members_by_id.get(stuck_id) if stuck_id else None
    if stuck_member is None:
        send("본인 Slack 계정이 조직 멤버와 매핑되어 있지 않아 이 명령어를 쓸 수 없습니다.")
        return

    try:
        result = wish_match.match_for_issue_text(store, stuck_member, issue_text)
    except Exception:
        # LLM 호출은 외부 API 경계라 쿼터 초과/설정 오류/네트워크 장애 등으로 언제든 실패할 수 있다.
        logger.exception(f"[wish] {WISH_COMMAND} LLM 매칭 실패 (member={stuck_member['member_id']!r})")
        send("매칭 처리 중 오류가 발생했어요. 잠시 후 다시 시도해주세요. (반복되면 관리자에게 LLM 설정을 확인해달라고 알려주세요.)")
        return
    if not result.get("is_stuck"):
        send("음, 막힌 상황으로 보기 어려워요. 어떤 부분에서 막혔는지 조금 더 구체적으로 적어주세요.")
        return
    if not result.get("match_found"):
        send("비슷한 문제를 이미 해결한 동료를 찾지 못했어요. 나중에 다시 시도해보세요.")
        return

    records = wish_match.load_pending()
    record = wish_match.create_manual_request(records, stuck_member["member_id"], issue_text, result)
    wish_match.save_pending(records)

    helper = store.members_by_id[record["helper_member_id"]]
    helper_display = display_name_for(helper)
    send(
        text=f"🌱 {helper_display}님({helper['team']})이 {record['helper_topic']} 관련 경험이 있어요.",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"🌱 *지원 요청 매칭*\n{helper_display}님({helper['team']})이 "
                        f"*{record['helper_topic']}* 관련 작업 기록이 있어요. 요청을 보낼까요?"}},
            *_wish_button_blocks(record["id"], "confirm", "요청 보내기", "cancel", "취소"),
        ],
    )
    logger.info(f"[wish] {stuck_member['member_id']} {WISH_COMMAND} 매칭 -> {record['id']} "
                f"(helper={record['helper_member_id']}) DM 전송 완료")


def _wish_update(client, channel_id, message_ts, text, blocks=None):
    """response_url 기반 respond(replace_original=True) 대신 -- 원본 메시지를 client로 직접
    갱신한다(같은 채널+ts를 알고 있으면 누가 만든 메시지든 갱신 가능, response_url 없이도 동작)."""
    client.chat_update(channel=channel_id, ts=message_ts, text=text, blocks=blocks or [])


def _wish_deny(client, channel_id, text):
    """권한 없음/이미 처리됨 등 원본 메시지는 그대로 두고 새 알림만 보낼 때."""
    client.chat_postMessage(channel=channel_id, text=text)


def _wish_confirm(record, records, clicker_id, store, client, channel_id, message_ts, logger):
    if clicker_id != record["stuck_member_id"] or record["status"] != "awaiting_requester_confirm":
        _wish_deny(client, channel_id, "처리할 수 없는 요청이에요 (권한이 없거나 이미 처리됨).")
        return
    helper_id = record["helper_member_id"]
    helper_slack_id = member_to_slack_id(helper_id)
    if not helper_slack_id:
        wish_match.transition(records, record["id"], "confirmed_no_recipient")
        _wish_update(client, channel_id, message_ts, "상대방의 Slack 계정이 연결되어 있지 않아 전송할 수 없어요.")
        return

    stuck_display = display_name_for(store.members_by_id[record["stuck_member_id"]])
    dm = client.conversations_open(users=[helper_slack_id])["channel"]["id"]
    resp = client.chat_postMessage(
        channel=dm,
        text=f"{stuck_display}님이 도움을 요청했어요.",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"🙋 *지원 요청이 도착했어요*\n{stuck_display}님이 비슷한 문제로 도움을 요청했어요.\n"
                        f"문제: {record.get('issue_text') or record.get('problem_summary', '')}\n"
                        f"(참고: 회원님의 과거 기록 - {record.get('reason', '')})"}},
            *_wish_button_blocks(record["id"], "accept", "도와줄게요", "decline", "이번엔 어려워요"),
        ],
    )
    wish_match.transition(records, record["id"], "awaiting_helper_response",
                           helper_channel_id=dm, helper_message_ts=resp["ts"])
    _wish_update(client, channel_id, message_ts, "요청을 보냈어요! 상대방의 답장을 기다리는 중입니다.")
    logger.info(f"[wish] {record['id']} confirm -> helper {helper_id} DM 전송")


def _wish_cancel(record, records, clicker_id, store, client, channel_id, message_ts, logger):
    if clicker_id != record["stuck_member_id"] or record["status"] != "awaiting_requester_confirm":
        _wish_deny(client, channel_id, "처리할 수 없는 요청이에요.")
        return
    wish_match.transition(records, record["id"], "cancelled")
    _wish_update(client, channel_id, message_ts, "요청을 취소했어요.")


def _wish_accept(record, records, clicker_id, store, client, channel_id, message_ts, logger):
    if clicker_id != record["helper_member_id"] or record["status"] != "awaiting_helper_response":
        _wish_deny(client, channel_id, "처리할 수 없는 요청이에요.")
        return
    wish_match.transition(records, record["id"], "helping")
    _wish_update(
        client, channel_id, message_ts, "수락했어요.",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": "✅ 수락했어요. 도움을 다 드리면 아래를 눌러주세요."}},
            *_wish_button_blocks(record["id"], "done", "도움을 마쳤어요"),
        ],
    )
    stuck_slack_id = member_to_slack_id(record["stuck_member_id"])
    if stuck_slack_id:
        helper_display = display_name_for(store.members_by_id[record["helper_member_id"]])
        dm = client.conversations_open(users=[stuck_slack_id])["channel"]["id"]
        client.chat_postMessage(channel=dm, text=f"🙌 {helper_display}님이 도와주기로 했어요!")


def _wish_decline(record, records, clicker_id, store, client, channel_id, message_ts, logger):
    if clicker_id != record["helper_member_id"] or record["status"] != "awaiting_helper_response":
        _wish_deny(client, channel_id, "처리할 수 없는 요청이에요.")
        return
    wish_match.transition(records, record["id"], "declined")
    _wish_update(client, channel_id, message_ts, "거절했어요. 요청자에게 알릴게요.")
    stuck_slack_id = member_to_slack_id(record["stuck_member_id"])
    if stuck_slack_id:
        dm = client.conversations_open(users=[stuck_slack_id])["channel"]["id"]
        client.chat_postMessage(channel=dm, text="이번엔 어렵다는 답변을 받았어요. 다른 방법을 찾아볼까요?")


def _wish_done(record, records, clicker_id, store, client, channel_id, message_ts, logger):
    """helper가 실제 도움을 마쳤다고 표시하는 시점 -- 여기서부터 요청자가 씨앗을 보낼 수 있게
    된다. '도와주기로 합의함'(accept)과 '실제로 도와줌'(done)을 구분해서, 씨앗이 합의가 아니라
    완료에 대한 보상이 되도록 한다."""
    if clicker_id != record["helper_member_id"] or record["status"] != "helping":
        _wish_deny(client, channel_id, "처리할 수 없는 요청이에요.")
        return
    wish_match.transition(records, record["id"], "awaiting_requester_seed")
    _wish_update(client, channel_id, message_ts, "완료 처리했어요. 곧 감사 인사가 올 거예요!")
    stuck_slack_id = member_to_slack_id(record["stuck_member_id"])
    if stuck_slack_id:
        helper_display = display_name_for(store.members_by_id[record["helper_member_id"]])
        dm = client.conversations_open(users=[stuck_slack_id])["channel"]["id"]
        client.chat_postMessage(
            channel=dm,
            text=f"{helper_display}님이 도움을 마쳤다고 알려왔어요.",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"🌱 {helper_display}님이 도움을 마쳤다고 알려왔어요. 씨앗을 보내드릴까요?"}},
                *_wish_button_blocks(record["id"], "send_seed", "씨앗 보내기"),
            ],
        )


def _wish_send_seed(record, records, clicker_id, store, client, channel_id, message_ts, logger):
    if clicker_id != record["stuck_member_id"] or record["status"] != "awaiting_requester_seed":
        _wish_deny(client, channel_id, "처리할 수 없는 요청이에요.")
        return
    wish_match.transition(records, record["id"], "awaiting_helper_plant_choice")
    _wish_update(client, channel_id, message_ts, "씨앗을 보냈어요!")
    helper_slack_id = member_to_slack_id(record["helper_member_id"])
    if helper_slack_id:
        stuck_display = display_name_for(store.members_by_id[record["stuck_member_id"]])
        dm = client.conversations_open(users=[helper_slack_id])["channel"]["id"]
        client.chat_postMessage(
            channel=dm,
            text=f"{stuck_display}님이 씨앗을 보냈어요!",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"🌱 {stuck_display}님이 씨앗을 보냈어요! 정원(화초)과 텃밭(작물) 중 골라주세요."}},
                *_wish_button_blocks(record["id"], "plant_flower", "🌸 화초", "plant_crop", "🌾 작물", style_yes=None),
            ],
        )


def _wish_plant_choice(record, records, clicker_id, store, client, channel_id, message_ts, logger, plant_type):
    if clicker_id != record["helper_member_id"] or record["status"] != "awaiting_helper_plant_choice":
        _wish_deny(client, channel_id, "처리할 수 없는 요청이에요.")
        return
    wish_match.transition(records, record["id"], "completed", plant_type=plant_type)
    label = "화초" if plant_type == "flower" else "작물"
    _wish_update(client, channel_id, message_ts, f"🌱 {label}(으)로 심었어요! 정원에서 확인할 수 있어요.")


_WISH_BUTTON_HANDLERS = {
    "confirm": _wish_confirm,
    "cancel": _wish_cancel,
    "accept": _wish_accept,
    "decline": _wish_decline,
    "done": _wish_done,
    "send_seed": _wish_send_seed,
    "plant_flower": lambda *a: _wish_plant_choice(*a, plant_type="flower"),
    "plant_crop": lambda *a: _wish_plant_choice(*a, plant_type="crop"),
}


@app.action(re.compile(r"^wish_button_"))
def handle_wish_button(ack, action, body, client, logger):
    ack()
    verb, _, wish_id = action["value"].partition(":")

    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]

    store = DataStore()
    member_map = load_member_map()
    clicker_id = member_map.get(body["user"]["id"])

    records = wish_match.load_pending()
    record = wish_match.find_record(records, wish_id)
    if record is None:
        _wish_deny(client, channel_id, "이미 처리되었거나 존재하지 않는 요청입니다.")
        return

    handler = _WISH_BUTTON_HANDLERS.get(verb)
    if handler is None:
        logger.warning(f"[skip] 알 수 없는 wish 버튼 verb: {verb!r}")
        return
    handler(record, records, clicker_id, store, client, channel_id, message_ts, logger)
    wish_match.save_pending(records)


def main():
    if not os.environ.get("SLACK_APP_TOKEN"):
        raise SystemExit(
            "SLACK_APP_TOKEN 환경변수가 없습니다. Slack 앱 설정 > Socket Mode에서 App-Level "
            "Token(xapp-...)을 발급해 .env에 SLACK_APP_TOKEN=xapp-... 로 추가하세요."
        )
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Socket Mode 리스너 시작 [코드버전: action_id-fix-v2] -- app_home_opened 이벤트 대기 중 (Ctrl+C로 종료)")
    handler.start()


if __name__ == "__main__":
    main()
