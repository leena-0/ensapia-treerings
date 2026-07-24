#!/usr/bin/env python3
"""
팀 코칭 카드를 리더(1차/2차평가자)에게 DM 메시지로 발송한다.

여기서는 LLM을 호출하지 않는다 -- report_cache 에 이미 있는 최신 coaching_card 캐시를 읽어
chat.postMessage 로 보낼 뿐이다. 생성 자체는
`python3 -m reports.generate_reports --report coaching --teams 사업부` 로 먼저 해둬야 한다.

리더가 아닌 멤버(팀원)에게는 발송하지 않는다 (코칭 카드는 리더 전용).

사용 예:
  python3 -m slack_app.send_coaching_dm --member M01
  python3 -m slack_app.send_coaching_dm --all
"""

import argparse
import os
import sys

from env_loader import load_env
from reports import cache_store
from reports.data_access import DataStore
from slack_app.home_view import build_coaching_blocks
from slack_app.member_map import load_member_map, display_name_for


def send_for_member(client, store, slack_user_id, member_id):
    member = store.members_by_id.get(member_id)
    if member is None:
        print(f"  [skip] 알 수 없는 member_id: {member_id}")
        return
    if member["role"] not in ("1차평가자", "2차평가자"):
        print(f"  [skip] {member_id}({member['name']}) 은 리더가 아니라 코칭 카드 대상이 아닙니다.")
        return

    coaching_record = cache_store.latest("coaching_card", member["team"])
    if coaching_record is None:
        print(f"  [skip] {member['team']}: 생성된 코칭 카드가 없습니다 "
              f"(먼저 reports.generate_reports --report coaching --teams {member['team']} 실행 필요)")
        return

    display_name = display_name_for(member)
    blocks = build_coaching_blocks(member["team"], coaching_record)
    period = coaching_record["content"].get("period", "?")
    client.chat_postMessage(
        channel=slack_user_id,
        blocks=blocks,
        text=f"{member['team']} 팀 코칭 카드 ({period})",
    )
    print(f"  [sent] {member['team']} 코칭 카드 -> {display_name}({member_id}) / slack user {slack_user_id}")


def main():
    from slack_sdk import WebClient

    parser = argparse.ArgumentParser(description="팀 코칭 카드를 리더에게 DM으로 발송")
    parser.add_argument("--member", help="member_id 하나만 발송 (리더여야 함, 예: M01)")
    parser.add_argument("--all", action="store_true", help="매핑된 리더 전원 발송")
    args = parser.parse_args()

    if not args.member and not args.all:
        sys.exit("--member M01 또는 --all 중 하나를 지정하세요")

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
