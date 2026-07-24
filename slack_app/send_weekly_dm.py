#!/usr/bin/env python3
"""
개인 주간 리포트를 DM 메시지로 발송한다.

여기서는 LLM을 호출하지 않는다 -- report_cache 에 이미 있는 최신 personal_weekly 캐시를
읽어 chat.postMessage 로 보낼 뿐이다. 리포트 생성 자체는
`python3 -m reports.generate_reports --report personal --members M01` 로 먼저 해둬야 한다.

매주 금요일 발송을 의도하지만, 아직 스케줄러(cron)는 붙이지 않았다 -- 지금은 수동 실행이고,
GCE VM 배포 시 cron으로 이 스크립트를 금요일마다 돌리면 된다.

사용 예:
  python3 -m slack_app.send_weekly_dm --member M01
  python3 -m slack_app.send_weekly_dm --all
"""

import argparse
import os
import sys

from env_loader import load_env
from reports import cache_store
from reports.data_access import DataStore
from slack_app.home_view import build_personal_blocks
from slack_app.member_map import load_member_map, display_name_for


def _collect_log_ids(content):
    ids = set()
    for item in content.get("highlights", []):
        ids.update(item.get("log_ids", []))
    for gp in content.get("goal_progress", []):
        for c in gp.get("citations", []):
            if c.get("log_id"):
                ids.add(c["log_id"])
    for item in content.get("issues", []):
        ids.update(item.get("log_ids", []))
    for item in content.get("one_on_one_agenda", []):
        ids.update(item.get("log_ids", []))
    return ids


def _resolve_permalinks(client, store, log_ids):
    """
    log_id -> Slack permalink. 목데이터 로그(channel_id/ts 없음)는 원본 메시지가 아니므로
    링크를 만들지 않고 건너뛴다 (가짜 링크를 지어내지 않음).
    """
    logs_by_id = {log["log_id"]: log for log in store.slack_logs}
    permalinks = {}
    for log_id in log_ids:
        log = logs_by_id.get(log_id)
        if not log or not log.get("channel_id") or not log.get("ts"):
            continue
        try:
            resp = client.chat_getPermalink(channel=log["channel_id"], message_ts=log["ts"])
            permalinks[log_id] = resp["permalink"]
        except Exception as e:  # noqa: BLE001 - 링크 조회 실패는 발송 자체를 막을 이유가 아님
            print(f"  [warn] {log_id} permalink 조회 실패: {e}")
    return permalinks


def send_for_member(client, store, slack_user_id, member_id):
    member = store.members_by_id.get(member_id)
    if member is None:
        print(f"  [skip] 알 수 없는 member_id: {member_id}")
        return

    personal_record = cache_store.latest("personal_weekly", member_id)
    if personal_record is None:
        print(f"  [skip] {member_id}: 생성된 개인 주간 리포트가 없습니다 "
              f"(먼저 reports.generate_reports --report personal --members {member_id} 실행 필요)")
        return

    display_name = display_name_for(member)
    log_ids = _collect_log_ids(personal_record["content"])
    permalinks = _resolve_permalinks(client, store, log_ids)
    blocks = build_personal_blocks(member, personal_record, display_name=display_name, permalinks=permalinks)
    week_range = f"{personal_record['content'].get('week_start', '?')}~{personal_record['content'].get('week_end', '?')}"
    client.chat_postMessage(
        channel=slack_user_id,
        blocks=blocks,
        text=f"{display_name}님의 주간 리포트 ({week_range})",  # 알림/폴백 텍스트
    )
    print(f"  [sent] {display_name}({member_id}) 주간 리포트 -> slack user {slack_user_id}")


def main():
    from slack_sdk import WebClient

    parser = argparse.ArgumentParser(description="개인 주간 리포트를 DM으로 발송")
    parser.add_argument("--member", help="member_id 하나만 발송 (예: M01)")
    parser.add_argument("--all", action="store_true", help="data/slack_user_map.json 에 매핑된 전원 발송")
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
