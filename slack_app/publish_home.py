#!/usr/bin/env python3
"""
App Home 탭에 "목표 현황" 정적 대시보드를 발행(publish)한다.

여기서는 LLM을 호출하지 않고 goals/kpis/goal_kpi_link CSV를 그 자리에서 읽어 렌더링하므로,
report_cache 유무와 무관하게 항상 최신 상태를 보여준다. 개인 주간 리포트/팀 코칭 카드는
홈 탭이 아니라 DM 메시지로 전송된다 (send_weekly_dm.py).

사용 예:
  python3 -m slack_app.publish_home --member M01
  python3 -m slack_app.publish_home --all   # 매핑된 전원에게 발행
"""

import argparse
import os
import sys

from env_loader import load_env
from reports.data_access import DataStore
from slack_app.home_view import build_home_view
from slack_app.member_map import load_member_map, display_name_for

QUARTER = "2026-Q2"


def publish_for_member(client, store, slack_user_id, member_id):
    member = store.members_by_id.get(member_id)
    if member is None:
        print(f"  [skip] 알 수 없는 member_id: {member_id}")
        return

    display_name = display_name_for(member)
    view = build_home_view(member, store, QUARTER, display_name=display_name)
    client.views_publish(user_id=slack_user_id, view=view)
    print(f"  [published] {display_name}({member_id}) -> slack user {slack_user_id}")


def main():
    from slack_sdk import WebClient

    parser = argparse.ArgumentParser(description="목표 현황을 App Home 탭에 발행")
    parser.add_argument("--member", help="member_id 하나만 발행 (예: M01)")
    parser.add_argument("--all", action="store_true", help="data/slack_user_map.json 에 매핑된 전원 발행")
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
            publish_for_member(client, store, slack_user_id, member_id)
    else:
        slack_user_id = next((sid for sid, mid in member_map.items() if mid == args.member), None)
        if not slack_user_id:
            sys.exit(f"{args.member} 에 매핑된 Slack 사용자가 data/slack_user_map.json 에 없습니다.")
        publish_for_member(client, store, slack_user_id, args.member)


if __name__ == "__main__":
    main()
