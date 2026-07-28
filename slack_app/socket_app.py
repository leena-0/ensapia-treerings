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

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from env_loader import load_env
from reports.data_access import DataStore, record_subgoal_checkin
from slack_app.home_view import build_home_view
from slack_app.member_map import display_name_for, load_member_map
from slack_app.send_evidence_package import _build_pdf

QUARTER = "2026-Q2"
LEADER_ROLES = ("1차평가자", "2차평가자")

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


def main():
    if not os.environ.get("SLACK_APP_TOKEN"):
        raise SystemExit(
            "SLACK_APP_TOKEN 환경변수가 없습니다. Slack 앱 설정 > Socket Mode에서 App-Level "
            "Token(xapp-...)을 발급해 .env에 SLACK_APP_TOKEN=xapp-... 로 추가하세요."
        )
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Socket Mode 리스너 시작 -- app_home_opened 이벤트 대기 중 (Ctrl+C로 종료)")
    handler.start()


if __name__ == "__main__":
    main()
