#!/usr/bin/env python3
"""
평가 근거 패키지를 PDF로 변환해 Slack DM으로 발송한다 (알림 메시지 + PDF 첨부를 한 번에).

여기서는 LLM을 호출하지 않는다 -- report_cache 에 이미 있는 evidence_package 캐시를 읽어
PDF로 렌더링하고 업로드할 뿐이다. 생성 자체는
`python3 -m reports.generate_reports --report evidence --members M03` 로 먼저 해둬야 한다.

--team 모드: 팀 리더(1차/2차평가자)가 "전체 조직을 한눈에" 보고 싶다는 요구사항에 대한 답.
Block Kit 텍스트로 전체 팀원의 인용까지 다 요약하면 메시지당 블록 50개 제한에 걸리고 내용도
너무 빽빽해지므로, 대신 PDF는 원본 그대로 두고 `files_upload_v2` 의 다중 파일 업로드
(file_uploads 파라미터)로 팀원 전원의 PDF를 **메시지 하나**에 몰아서 리더에게 보낸다 --
Block Kit 텍스트 용량 제한과 무관하게 파일 첨부라 전체 내용이 그대로 보존된다.

사용 예:
  python3 -m slack_app.send_evidence_package --member M03
  python3 -m slack_app.send_evidence_package --all
  python3 -m slack_app.send_evidence_package --team R&D
"""

import argparse
import os
import sys
import tempfile

from env_loader import load_env
from reports import cache_store
from reports.data_access import DataStore
from reports.evidence_pdf import build_evidence_pdf
from reports.generate_reports import QUARTER
from slack_app.member_map import load_member_map, display_name_for

LEADER_ROLES = ("1차평가자", "2차평가자")


def _build_pdf(store, member_id):
    """member_id 의 평가 근거 패키지 PDF를 만들어 (display_name, pdf_path) 를 반환.
    캐시가 없거나 파싱 실패면 경고를 출력하고 None을 반환한다."""
    member = store.members_by_id.get(member_id)
    if member is None:
        print(f"  [skip] 알 수 없는 member_id: {member_id}")
        return None

    path = cache_store.cache_path("evidence_package", member_id, QUARTER)
    record = cache_store.load(path)
    if record is None:
        print(f"  [skip] {member_id}: 생성된 평가 근거 패키지가 없습니다 "
              f"(먼저 reports.generate_reports --report evidence --members {member_id} 실행 필요)")
        return None

    content = record["content"]
    if content.get("_parse_error"):
        print(f"  [skip] {member_id}: LLM 응답 파싱 실패 캐시라 PDF로 만들 수 없습니다.")
        return None

    display_name = display_name_for(member)
    member_for_pdf = {**member, "name": display_name}  # PDF에도 표시 이름 오버라이드 반영 (mock 데이터 자체는 그대로 둠)
    pdf_path = os.path.join(tempfile.gettempdir(), f"evidence_{member_id}_{QUARTER}.pdf")
    build_evidence_pdf(member_for_pdf, content, pdf_path)
    return display_name, pdf_path


def send_for_member(client, store, slack_user_id, member_id):
    built = _build_pdf(store, member_id)
    if built is None:
        return
    display_name, pdf_path = built

    # files_upload_v2 는 user_id 가 아니라 실제 conversation(channel) id 를 요구한다 --
    # DM 은 conversations.open 으로 그 유저와의 DM 채널 id(D...)를 먼저 받아와야 한다.
    dm = client.conversations_open(users=[slack_user_id])
    dm_channel_id = dm["channel"]["id"]

    comment = (f"📄 *{display_name}님의 평가 근거 패키지({QUARTER})가 생성되었습니다.*\n"
               f"목표별 진척도·임팩트와 실제 업무일지 인용 근거가 담긴 PDF를 첨부합니다.")
    client.files_upload_v2(
        channel=dm_channel_id,
        file=pdf_path,
        filename=f"{display_name}_{QUARTER}_평가근거패키지.pdf",
        title=f"{display_name}님 평가 근거 패키지 ({QUARTER})",
        initial_comment=comment,
    )
    print(f"  [sent] {display_name}({member_id}) 평가 근거 패키지 PDF -> slack user {slack_user_id}")


def send_team_bundle(client, store, member_map, team):
    """팀 전체 PDF를 한 메시지에 몰아서 그 팀의 1차/2차평가자에게 각각 발송."""
    member_ids = store.members_by_team.get(team, [])
    if not member_ids:
        print(f"  [skip] {team}: 소속 멤버가 없습니다.")
        return

    built = []
    for member_id in member_ids:
        result = _build_pdf(store, member_id)
        if result:
            built.append((member_id, *result))
    if not built:
        print(f"  [skip] {team}: 생성된 평가 근거 패키지가 하나도 없습니다.")
        return

    leader_ids = [mid for mid in member_ids if store.members_by_id[mid]["role"] in LEADER_ROLES]
    if not leader_ids:
        print(f"  [skip] {team}: 1차/2차평가자가 없습니다.")
        return

    file_uploads = [
        {"file": pdf_path, "filename": f"{name}_{QUARTER}_평가근거패키지.pdf", "title": f"{name}_{QUARTER}_평가근거패키지"}
        for _mid, name, pdf_path in built
    ]
    member_list = ", ".join(name for _mid, name, _path in built)
    comment = (f"📄 *{team} 팀 전체 평가 근거 패키지 모음 ({QUARTER})*\n"
               f"포함된 팀원({len(built)}명): {member_list}\n"
               f"각 PDF는 팀원 본인이 받는 것과 동일한 내용입니다(목표별 진척도·임팩트·업무일지 인용).")

    for leader_id in leader_ids:
        leader = store.members_by_id[leader_id]
        slack_user_id = next((sid for sid, mid in member_map.items() if mid == leader_id), None)
        if not slack_user_id:
            print(f"  [skip] 리더 {leader['name']}({leader_id})의 Slack 매핑이 없습니다.")
            continue
        dm = client.conversations_open(users=[slack_user_id])
        client.files_upload_v2(channel=dm["channel"]["id"], file_uploads=file_uploads, initial_comment=comment)
        print(f"  [sent] {team} 팀 PDF {len(built)}건 -> {leader['name']}({leader_id}, {leader['role']})")


def main():
    from slack_sdk import WebClient

    parser = argparse.ArgumentParser(description="평가 근거 패키지를 PDF로 만들어 DM 발송")
    parser.add_argument("--member", help="member_id 하나만 발송 (예: M03)")
    parser.add_argument("--all", action="store_true", help="매핑된 전원 발송")
    parser.add_argument("--team", help="그 팀 전체 PDF를 한 메시지로 묶어 1차/2차평가자에게 발송 (예: R&D)")
    args = parser.parse_args()

    if not args.member and not args.all and not args.team:
        sys.exit("--member M03 / --all / --team R&D 중 하나를 지정하세요")

    load_env()
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    store = DataStore()
    member_map = load_member_map()

    if args.team:
        send_team_bundle(client, store, member_map, args.team)
    elif args.all:
        if not member_map:
            sys.exit("data/slack_user_map.json 이 비어 있습니다.")
        for slack_user_id, member_id in member_map.items():
            send_for_member(client, store, slack_user_id, member_id)
    else:
        slack_user_id = next((sid for sid, mid in member_map.items() if mid == args.member), None)
        if not slack_user_id:
            sys.exit(f"{args.member} 에 매핑된 Slack 사용자가 data/slack_user_map.json 에 없습니다.")
        send_for_member(client, store, slack_user_id, args.member)


if __name__ == "__main__":
    main()
