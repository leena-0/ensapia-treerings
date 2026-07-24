#!/usr/bin/env python3
"""
평가 근거 패키지를 PDF로 변환해 Slack DM으로 발송한다 (알림 메시지 + PDF 첨부를 한 번에).

여기서는 LLM을 호출하지 않는다 -- report_cache 에 이미 있는 evidence_package 캐시를 읽어
PDF로 렌더링하고 업로드할 뿐이다. 생성 자체는
`python3 -m reports.generate_reports --report evidence --members M03` 로 먼저 해둬야 한다.

사용 예:
  python3 -m slack_app.send_evidence_package --member M03
  python3 -m slack_app.send_evidence_package --all
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


def send_for_member(client, store, slack_user_id, member_id):
    member = store.members_by_id.get(member_id)
    if member is None:
        print(f"  [skip] 알 수 없는 member_id: {member_id}")
        return

    path = cache_store.cache_path("evidence_package", member_id, QUARTER)
    record = cache_store.load(path)
    if record is None:
        print(f"  [skip] {member_id}: 생성된 평가 근거 패키지가 없습니다 "
              f"(먼저 reports.generate_reports --report evidence --members {member_id} 실행 필요)")
        return

    content = record["content"]
    if content.get("_parse_error"):
        print(f"  [skip] {member_id}: LLM 응답 파싱 실패 캐시라 PDF로 만들 수 없습니다.")
        return

    display_name = display_name_for(member)
    member_for_pdf = {**member, "name": display_name}  # PDF에도 표시 이름 오버라이드 반영 (mock 데이터 자체는 그대로 둠)
    pdf_path = os.path.join(tempfile.gettempdir(), f"evidence_{member_id}_{QUARTER}.pdf")
    build_evidence_pdf(member_for_pdf, content, pdf_path)

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


def main():
    from slack_sdk import WebClient

    parser = argparse.ArgumentParser(description="평가 근거 패키지를 PDF로 만들어 DM 발송")
    parser.add_argument("--member", help="member_id 하나만 발송 (예: M03)")
    parser.add_argument("--all", action="store_true", help="매핑된 전원 발송")
    args = parser.parse_args()

    if not args.member and not args.all:
        sys.exit("--member M03 또는 --all 중 하나를 지정하세요")

    load_env()
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    store = DataStore()
    member_map = load_member_map()

    if args.all:
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
