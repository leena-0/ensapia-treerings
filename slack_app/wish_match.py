#!/usr/bin/env python3
"""
소원 매칭: 업무일지에서 "막힌 상황"을 감지하고, 과거 기록에서 비슷한 문제를 해결한 동료를 찾아 연결한다.

흐름 (DM 기반 -- im:history/im:read 스코프 추가 후 전환):
1. --scan: 이슈 키워드가 있는 최근 로그를 후보로 뽑아 LLM으로 "막힌 상황인지 + 과거 해결 사례"를 판단.
   매칭되면 당사자(막힌 사람)에게 **DM**으로 확인 메시지를 보내고 data/wish_pending.json 에 대기 기록을 남긴다.
2. --check-replies: 대기 중인 각 요청의 DM 채널을 폴링해서, 당사자가 "네/응" 등으로 답장했는지 확인.
   확인되면 helper 에게 "도와줄 수 있나요?" 형태의 질문 DM 을 보낸다(일방 통보가 아니라 협조 요청).

원칙: AI는 "매칭이 그럴듯한가"만 판단하고, 그 log_id/member_id 가 실제 존재하는지는 코드가
검증한다 (data_dictionary.md 의 "인용 검증은 AI를 쓰지 않는다" 원칙과 동일).

프라이버시 원칙(2026-07-28 수정): 요청자(막힌 사람)에게 보내는 제안 DM에는 helper의 원문
발췌를 넣지 않는다. 대신 LLM이 만든 일반화된 주제(helper_topic, 2~5단어)와 팀, 월 단위
시점만 노출한다 -- "누구에게 물어볼까"를 알려주는 것이 목적이지 상대가 정확히 뭘 썼는지
보여주는 게 목적이 아니다(상세는 소원이 확정된 뒤 당사자에게 직접 묻는 구조).

DM 기반이라 대화가 각자의 1:1 채널에만 남고, 다른 팀원(예: 매핑 안 된 동료)에게 노출되지 않는다.

사용 예:
  python3 -m slack_app.wish_match --scan
  python3 -m slack_app.wish_match --check-replies
"""

import json
import os
import re
from datetime import datetime, timezone

from env_loader import load_env
from reports import prompts
from reports.data_access import DataStore, DATA_DIR
from reports.llm_client import generate_json
from slack_app.member_map import load_member_map, display_name_for, member_to_slack_id

WISH_PENDING_PATH = os.path.join(DATA_DIR, "wish_pending.json")
STATE_PATH = os.path.join(DATA_DIR, "slack_ingest_state.json")
WISH_STATE_KEY_PREFIX = "wish_confirm_dm:"

ISSUE_KEYWORDS = ["지연", "이슈", "크래시", "우려", "부족", "오차", "오류", "어뷰징", "병목",
                  "막혀", "막힘", "미해결", "협업 요청", "확인 요청", "지원 필요", "제약", "저항"]

CONFIRM_YES_RE = re.compile(r"^(네+|넵|응|어|좋아요?|okay?|yes)[!.\s]*$", re.IGNORECASE)
CONFIRM_NO_RE = re.compile(r"^(아니\S*|no)[!.\s]*$", re.IGNORECASE)


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _has_issue_keyword(text):
    return any(kw in text for kw in ISSUE_KEYWORDS)


def _candidate_logs(store, member_id, n=3):
    logs = store.logs_by_member.get(member_id, [])
    return [l for l in logs[-n:] if _has_issue_keyword(l["text"])]


def _other_issue_logs(store, exclude_member_id, limit=40):
    out = []
    for log in store.slack_logs:
        if log["member_id"] == exclude_member_id:
            continue
        if not _has_issue_keyword(log["text"]):
            continue
        member = store.members_by_id[log["member_id"]]
        out.append({**log, "member_name": member["name"]})
    return out[-limit:]


def _open_dm(client, slack_user_id):
    resp = client.conversations_open(users=[slack_user_id])
    return resp["channel"]["id"]


def scan(client, store):
    pending = _load_json(WISH_PENDING_PATH, [])
    already_checked = {p["candidate_log_id"] for p in pending}
    logs_by_id = {l["log_id"]: l for l in store.slack_logs}

    next_n = len(pending) + 1
    made = 0
    for member in store.members:
        member_id = member["member_id"]
        for log in _candidate_logs(store, member_id):
            if log["log_id"] in already_checked:
                continue

            others = _other_issue_logs(store, member_id)
            prompt = prompts.build_wish_match_prompt(member, log, others)
            text, model_name, used_fallback = generate_json(prompt)
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                print(f"  [skip] {log['log_id']} 응답 파싱 실패")
                continue

            record = {
                "id": f"W{next_n:04d}",
                "candidate_log_id": log["log_id"],
                "stuck_member_id": member_id,
                "checked_at": _now_iso(),
                "is_stuck": bool(result.get("is_stuck")),
                "match_found": False,
                "status": "no_match",
            }

            if result.get("is_stuck") and result.get("match_found"):
                helper_id = result.get("helper_member_id")
                helper_log_id = result.get("helper_log_id")
                helper_log = logs_by_id.get(helper_log_id)
                # 코드 레벨 인용 검증: AI가 지목한 log_id/member_id 가 실제로 존재하고 일치하는지 확인
                if helper_log and helper_log["member_id"] == helper_id and helper_id in store.members_by_id:
                    stuck_slack_id = member_to_slack_id(member_id)
                    if not stuck_slack_id:
                        print(f"  [skip] {log['log_id']}: {member_id} 의 Slack 매핑이 없어 DM을 보낼 수 없음")
                        pending.append(record)
                        next_n += 1
                        continue

                    stuck_display = display_name_for(member)
                    helper_display = display_name_for(store.members_by_id[helper_id])
                    # 프라이버시 원칙(data_dictionary.md 5-3절/프로젝트 개요 §5-3): 요청자에게는
                    # 메타데이터(누가/언제/무슨 주제)만 노출하고, 원문 발췌·permalink·해결 방법
                    # 상세는 절대 노출하지 않는다. 시점도 정확한 날짜가 아니라 월 단위로 뭉갠다.
                    helper_topic = (result.get("helper_topic") or "").strip() or "관련 업무"
                    helper_team = store.members_by_id[helper_id]["team"]
                    helper_month_label = f"{int(helper_log['date'][5:7])}월경"
                    msg = (
                        f"🌱 *소원 매칭 제안*\n"
                        f"{stuck_display}님, {helper_display}님({helper_team})이 {helper_month_label} "
                        f"{helper_topic} 관련 작업 기록이 있어요.\n"
                        f"소원을 보낼까요? *'네'* 라고 답장해주세요."
                    )
                    dm_channel_id = _open_dm(client, stuck_slack_id)
                    resp = client.chat_postMessage(channel=dm_channel_id, text=msg)
                    record.update({
                        "match_found": True,
                        "status": "pending",
                        "helper_member_id": helper_id,
                        "helper_log_id": helper_log_id,
                        "helper_topic": helper_topic,
                        "problem_summary": result.get("problem_summary", ""),
                        "reason": result.get("reason", ""),
                        "suggestion_ts": resp["ts"],
                        "channel_id": dm_channel_id,
                    })
                    made += 1
                    print(f"  [suggested] {log['log_id']} ({stuck_display}) <- 도움: {helper_display} ({helper_log_id}) via DM")
                else:
                    print(f"  [rejected] {log['log_id']}: LLM이 지목한 helper_log_id={helper_log_id!r}"
                          f" / helper_member_id={helper_id!r} 가 실제 데이터와 불일치해 폐기")

            pending.append(record)
            next_n += 1

    _save_json(WISH_PENDING_PATH, pending)
    print(f"스캔 완료: 신규 제안 {made}건 (전체 대기 기록 {len(pending)}건)")


def check_replies(client, store):
    pending = _load_json(WISH_PENDING_PATH, [])
    open_records = [p for p in pending if p.get("status") == "pending" and p.get("channel_id")]
    if not open_records:
        print("확인 대기 중인 소원 제안이 없습니다.")
        return

    state = _load_json(STATE_PATH, {})
    member_map = load_member_map()
    resolved = 0

    # 같은 DM 채널을 여러 record 가 공유할 수 있으니(같은 사람의 여러 제안), 채널별로 한 번만 폴링한다.
    channels = sorted({r["channel_id"] for r in open_records})
    for channel_id in channels:
        state_key = f"{WISH_STATE_KEY_PREFIX}{channel_id}"
        last_ts = state.get(state_key, "0")

        resp = client.conversations_history(channel=channel_id, oldest=last_ts, limit=200)
        messages = [m for m in resp["messages"] if float(m["ts"]) > float(last_ts)]
        messages.sort(key=lambda m: float(m["ts"]))

        max_ts_seen = last_ts
        records_for_channel = [r for r in open_records if r["channel_id"] == channel_id]

        for msg in messages:
            max_ts_seen = msg["ts"]
            if msg.get("subtype") or msg.get("bot_id"):
                continue
            sender_member_id = member_map.get(msg.get("user"))
            text = (msg.get("text") or "").strip()

            for record in records_for_channel:
                if record["status"] != "pending":
                    continue
                if record["stuck_member_id"] != sender_member_id:
                    continue
                if float(msg["ts"]) <= float(record["suggestion_ts"]):
                    continue  # 제안 메시지보다 먼저 온 메시지는 답장이 아님

                if CONFIRM_YES_RE.match(text):
                    helper_id = record["helper_member_id"]
                    helper_slack_id = member_to_slack_id(helper_id)
                    stuck_display = display_name_for(store.members_by_id[record["stuck_member_id"]])
                    if helper_slack_id:
                        helper_dm = _open_dm(client, helper_slack_id)
                        client.chat_postMessage(
                            channel=helper_dm,
                            text=(f"🌱 *소원 요청이 도착했어요*\n{stuck_display}님이 비슷한 문제로 도움을 요청했어요. "
                                  f"도와주실 수 있나요?\n"
                                  f"문제: {record.get('problem_summary', '')}\n"
                                  f"(근거: 회원님의 과거 로그 {record['helper_log_id']} - {record.get('reason', '')})"),
                        )
                        record["status"] = "confirmed"
                        resolved += 1
                        print(f"  [confirmed] {record['id']} -> {helper_id} 에게 DM으로 협조 요청 전송")
                    else:
                        print(f"  [warn] {record['id']}: helper({helper_id})의 Slack 매핑이 없어 전송 실패")
                        record["status"] = "confirmed_no_recipient"
                    break
                elif CONFIRM_NO_RE.match(text):
                    record["status"] = "declined"
                    resolved += 1
                    print(f"  [declined] {record['id']}")
                    break

        state[state_key] = max_ts_seen

    _save_json(STATE_PATH, state)
    _save_json(WISH_PENDING_PATH, pending)
    print(f"답장 확인 완료: {resolved}건 처리, 여전히 대기 중 {sum(1 for p in pending if p['status'] == 'pending')}건")


def main():
    import argparse
    import sys
    from slack_sdk import WebClient

    parser = argparse.ArgumentParser(description="소원 매칭 스캔 및 확인 (DM 기반)")
    parser.add_argument("--scan", action="store_true", help="이슈 로그를 스캔해 소원 매칭 후보를 찾고 당사자에게 DM으로 제안")
    parser.add_argument("--check-replies", action="store_true", help="DM 답장을 확인해 확정/전송")
    args = parser.parse_args()

    if not args.scan and not args.check_replies:
        sys.exit("--scan 또는 --check-replies 중 하나를 지정하세요")

    load_env()
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    store = DataStore()

    if args.scan:
        scan(client, store)
    if args.check_replies:
        check_replies(client, store)


if __name__ == "__main__":
    main()
