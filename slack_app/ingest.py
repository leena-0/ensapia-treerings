#!/usr/bin/env python3
"""
Slack 업무일지 수집 (polling 방식).

실시간 Events API(Socket Mode 또는 공인 HTTPS 웹훅)는 App-Level Token 또는 배포된 서버가
필요해 아직 준비되지 않았다. 대신 conversations.history 를 필요할 때(또는 주기적으로) 호출해
새 메시지를 data/treerings.db 의 slack_logs 테이블에 추가하는 폴링 방식으로 동작한다. 필요한 봇 스코프
(channels:history, channels:read, users:read)는 이미 보유하고 있어 추가 설정 없이 동작한다.

goal 연결 규칙:
- 메시지에 "#G05" 같은 태그가 있으면 해당 goal 에 연결 (그 멤버 소유 goal 인지 검증).
- 태그가 없으면 그 멤버의 가장 최근 slack_log 의 linked_goal_id 를 재사용.
- 그마저 없으면(첫 로그) 그 멤버의 첫 번째 goal 에 연결.
- 어느 경우든 linked_goal_id 는 사후에 slack_logs 테이블에서 수동 수정 가능하다.

사용 예:
  python3 -m slack_app.ingest --channel C0BJH7F2FC4 --dry-run
  python3 -m slack_app.ingest --channel C0BJH7F2FC4
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

from env_loader import load_env
from reports.data_access import DataStore, DATA_DIR, get_connection
from slack_app.member_map import load_member_map

STATE_PATH = os.path.join(DATA_DIR, "slack_ingest_state.json")

GOAL_TAG_RE = re.compile(r"#(G\d+)", re.IGNORECASE)

# 소원 매칭(wish_match.py)의 "네/아니오" 확인 답장을 업무일지로 잘못 수집하지 않도록 거르는 패턴.
# 같은 채널을 폴링하기 때문에 이 짧은 확인 답장도 이 스크립트 눈에는 새 메시지로 보인다.
_CONFIRM_REPLY_RE = re.compile(r"^(네+|넵|응|어|좋아요?|okay?|yes|아니\S*|no)[!.\s]*$", re.IGNORECASE)


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _next_log_id(existing_logs):
    max_n = 0
    for row in existing_logs:
        m = re.match(r"L(\d+)", row["log_id"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def _pick_goal_id(member_id, tag_ids, store):
    member_goal_ids = {g["goal_id"] for g in store.member_goals(member_id)}
    for tag in tag_ids:
        if tag.upper() in member_goal_ids:
            return tag.upper()
    member_logs = store.logs_by_member.get(member_id, [])
    if member_logs:
        return member_logs[-1]["linked_goal_id"]
    goals = store.member_goals(member_id)
    return goals[0]["goal_id"] if goals else None


def ingest_channel(client, channel_id, *, dry_run=False):
    load_env()
    store = DataStore()
    member_map = load_member_map()
    if not member_map:
        print("경고: data/slack_user_map.json 이 비어 있습니다. 메시지를 어떤 member_id 로 "
              "연결할지 알 수 없어 전부 건너뜁니다.")

    state = _load_json(STATE_PATH, {})
    last_ts = state.get(channel_id, "0")

    resp = client.conversations_history(channel=channel_id, oldest=last_ts, limit=200)
    messages = [m for m in resp["messages"] if float(m["ts"]) > float(last_ts)]
    messages.sort(key=lambda m: float(m["ts"]))  # 오래된 것부터 처리

    new_rows = []
    max_ts_seen = last_ts
    skipped = 0
    for msg in messages:
        max_ts_seen = msg["ts"]
        if msg.get("subtype") or msg.get("bot_id"):
            continue  # 채널 참여 알림 등 시스템 메시지 / 봇 메시지는 업무일지 아님
        slack_user = msg.get("user")
        member_id = member_map.get(slack_user)
        if not member_id:
            print(f"  [skip] 매핑 안 된 Slack 사용자({slack_user})의 메시지: {msg.get('text', '')[:40]!r}")
            skipped += 1
            continue
        if _CONFIRM_REPLY_RE.match((msg.get("text") or "").strip()):
            skipped += 1
            continue  # 소원 매칭 확인 답장("네"/"아니오" 등) -- 업무일지가 아니므로 건너뜀
        tag_ids = GOAL_TAG_RE.findall(msg.get("text", ""))
        goal_id = _pick_goal_id(member_id, tag_ids, store)
        date_str = datetime.fromtimestamp(float(msg["ts"]), tz=timezone.utc).strftime("%Y-%m-%d")
        text = GOAL_TAG_RE.sub("", msg.get("text", "")).strip()
        new_rows.append({
            "log_id": None,
            "member_id": member_id,
            "date": date_str,
            "text": text,
            "linked_goal_id": goal_id,
            "channel_id": channel_id,  # 실제 메시지라 원본 채널/타임스탬프를 남겨 permalink 생성에 쓴다
            "ts": msg["ts"],
        })

    if not new_rows:
        print(f"새 업무일지 없음 (건너뜀 {skipped}건).")
        if not dry_run:
            state[channel_id] = max_ts_seen
            _save_json(STATE_PATH, state)
        return []

    next_n = _next_log_id(store.slack_logs)
    for i, row in enumerate(new_rows):
        row["log_id"] = f"L{next_n + i:04d}"

    print(f"{len(new_rows)}건의 새 업무일지 발견 (건너뜀 {skipped}건):")
    for row in new_rows:
        print(f"  {row['log_id']} {row['member_id']} [{row['date']}] -> {row['linked_goal_id']} : {row['text'][:50]}")

    if dry_run:
        print("(dry-run: DB에 쓰지 않음)")
        return new_rows

    conn = get_connection()
    try:
        with conn:
            conn.executemany(
                "INSERT INTO slack_logs (log_id, member_id, date, text, linked_goal_id, channel_id, ts) "
                "VALUES (:log_id, :member_id, :date, :text, :linked_goal_id, :channel_id, :ts)",
                new_rows,
            )
    finally:
        conn.close()

    state[channel_id] = max_ts_seen
    _save_json(STATE_PATH, state)
    return new_rows


def main():
    from slack_sdk import WebClient

    parser = argparse.ArgumentParser(description="Slack 채널 업무일지를 slack_logs 테이블로 수집 (폴링)")
    parser.add_argument("--channel", help="채널 ID (미지정 시 SLACK_INGEST_CHANNEL_ID 환경변수)")
    parser.add_argument("--dry-run", action="store_true", help="실제로 DB에 쓰지 않고 결과만 출력")
    args = parser.parse_args()

    load_env()
    channel_id = args.channel or os.environ.get("SLACK_INGEST_CHANNEL_ID")
    if not channel_id:
        sys.exit("채널 ID가 필요합니다 (--channel 또는 SLACK_INGEST_CHANNEL_ID 환경변수)")

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    ingest_channel(client, channel_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
