#!/usr/bin/env python3
"""
소원 매칭 (v10_2 문서 §5-3 "협업 연결 지원" 구현): 업무일지에서 "막힌 상황"을 감지하고,
과거 기록에서 비슷한 문제를 해결한 동료를 찾아 연결한다.

두 가지 진입 경로가 있다:

1. **일지 기반 자동 경로** (--scan / --check-replies): 이슈 키워드가 있는 최근 로그를 후보로
   뽑아 LLM으로 "막힌 상황인지 + 과거 해결 사례"를 판단. 매칭되면 당사자(막힌 사람)에게 DM으로
   확인 메시지를 보낸다. 이 경로는 **본인이 요청한 게 아니라 시스템이 먼저 제안**하는 것이라
   과탐지에 특히 보수적이어야 한다 -- 오늘 처음 언급한 사소한 어려움까지 매번 "도와줄까요?"라고
   물으면, 스스로 해결할 수 있는 것도 습관적으로 남에게 떠넘기게 만들거나(과도한 위임 조장)
   반대로 매번 무시당해 신뢰를 잃는다(§10 "오탐률이 탐지율보다 중요하다"와 동일한 논리를
   협업 연결에도 적용). 그래서 **같은 이슈가 서로 다른 날짜에 2회 이상 나타날 때만**(연속일
   필요 없음 -- §6-2 "연속이 아니라 누적으로 센다") 후보로 삼는다(_persistent_candidate_logs).
2. **온디맨드 커맨드 경로** (Slack `/지원요청`, slack_app/socket_app.py): 본인이 직접
   "어떤 상황에서 막혔는지"를 텍스트로 적어 명시적으로 요청한다. 이미 스스로 판단해서 커맨드를
   친 것이므로 지속성 요건을 적용하지 않는다(match_for_issue_text). 확인은 DM 텍스트 답장이
   아니라 Block Kit 버튼으로 받고, 상태 머신(아래)을 socket_app.py의 버튼 핸들러가 굴린다.

공통 원칙: AI는 "막힌 상황인지 + 매칭이 그럴듯한가"만 판단하고, 그 log_id/member_id 가 실제
존재하는지는 코드가 검증한다(_verify_and_extract_helper) -- data_dictionary.md 의 "인용
검증은 AI를 쓰지 않는다" 원칙과 동일.

프라이버시 원칙(2026-07-28 수정): 요청자(막힌 사람)에게 보내는 제안에는 helper의 원문
발췌를 넣지 않는다. 대신 LLM이 만든 일반화된 주제(helper_topic, 2~5단어)와 팀, 월 단위
시점만 노출한다 -- "누구에게 물어볼까"를 알려주는 것이 목적이지 상대가 정확히 뭘 썼는지
보여주는 게 목적이 아니다(상세는 소원이 확정된 뒤 당사자에게 직접 묻는 구조).

DM 기반이라 대화가 각자의 1:1 채널에만 남고, 다른 팀원(예: 매핑 안 된 동료)에게 노출되지 않는다.

**온디맨드 경로의 상태 머신** (data/wish_pending.json 의 status 필드, socket_app.py 가 전이시킴):
  awaiting_requester_confirm  -> (요청자가 "요청 보내기" 클릭)
  awaiting_helper_response    -> (helper 가 "도와줄게요"/"이번엔 어려워요" 클릭)
  helping                     -> (helper 가 "도움을 마쳤어요" 클릭)
  awaiting_requester_seed     -> (요청자가 "씨앗 보내기" 클릭)
  awaiting_helper_plant_choice -> (helper 가 화초/작물 선택)
  completed                   (종결, reports.collaboration 의 협업 집계에 포함됨)
  cancelled / declined        (종결, 집계 제외)

사용 예:
  python3 -m slack_app.wish_match --scan
  python3 -m slack_app.wish_match --check-replies
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone

from env_loader import load_env
from reports import prompts
from reports.coaching_signals import _cluster_by_objects, _object_tokens
from reports.collaboration import COMPLETED_STATUSES, HELPER_ACTION_STATUSES, TERMINAL_STATUSES  # noqa: F401 (재노출)
from reports.data_access import DataStore, DATA_DIR
from reports.llm_client import generate_json
from slack_app.member_map import load_member_map, display_name_for, member_to_slack_id

WISH_PENDING_PATH = os.path.join(DATA_DIR, "wish_pending.json")
STATE_PATH = os.path.join(DATA_DIR, "slack_ingest_state.json")
WISH_STATE_KEY_PREFIX = "wish_confirm_dm:"

# 코칭 카드와 동일 주기(격주)로 맞춘다 -- data_dictionary.md §8-1-1 교훈: "넓은 기간을 한 번에
# 조회하면 서로 무관한 후보들이 우연한 어휘 겹침으로 묶일 위험이 실제 배치 주기보다 커진다."
PERSISTENCE_LOOKBACK_DAYS = 14

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


def _persistent_candidate_logs(store, member_id, today=None):
    """
    "같은 이슈를 서로 다른 날짜에 2회 이상 언급"한 경우만 자동 제안 후보로 삼는다(연속일
    필요 없음). coaching_signals.py 가 팀 전체에서 "여러 사람이 같은 대상을 기다리는지"를
    토큰 군집화로 찾는 것과 동일한 방법을, 여기서는 "한 사람이 같은 대상을 반복 언급하는지"에
    적용한다 -- min_waiting_df=2 가 바로 그 "2회 이상" 조건이다.
    """
    cutoff = (today or date.today()) - timedelta(days=PERSISTENCE_LOOKBACK_DAYS)
    recent = [l for l in store.logs_by_member.get(member_id, []) if date.fromisoformat(l["date"]) >= cutoff]
    waiting = [l for l in recent if _has_issue_keyword(l["text"])]
    if not waiting:
        return []
    nonwaiting = [l for l in recent if not _has_issue_keyword(l["text"])]
    object_tokens = _object_tokens(waiting, nonwaiting, min_waiting_df=2)

    candidates = []
    for cluster in _cluster_by_objects(waiting, object_tokens):
        dates = {l["date"] for l in cluster}
        if len(dates) >= 2:  # 연속일 필요 없음 -- 하루 건너뛰고 다시 나와도 인정 (누적 원칙, §6-2)
            candidates.append(max(cluster, key=lambda l: l["date"]))  # 가장 최근 로그를 대표로 매칭에 사용
    return candidates


def _verify_and_extract_helper(store, result):
    """
    LLM이 지목한 helper_log_id/helper_member_id 가 실제 slack_logs 에 존재하고 서로 일치하는지
    코드가 검증한다(AI가 지어낸 근거를 그대로 믿지 않음). 검증 통과 시 정리된 dict, 실패/매칭
    없음 시 None -- 두 진입 경로(scan/온디맨드)가 공유하는 유일한 신뢰 경계.
    """
    if not (result.get("is_stuck") and result.get("match_found")):
        return None
    helper_id = result.get("helper_member_id")
    helper_log_id = result.get("helper_log_id")
    helper_log = store.logs_by_id.get(helper_log_id)
    if not (helper_log and helper_log["member_id"] == helper_id and helper_id in store.members_by_id):
        return None
    return {
        "helper_member_id": helper_id,
        "helper_log_id": helper_log_id,
        "helper_log": helper_log,
        "helper_topic": (result.get("helper_topic") or "").strip() or "관련 업무",
        "problem_summary": result.get("problem_summary", ""),
        "reason": result.get("reason", ""),
    }


def match_for_issue_text(store, stuck_member, issue_text):
    """
    온디맨드 경로(Slack `/지원요청 <설명>`): 본인이 직접 적은 텍스트를 이슈 로그처럼 취급해
    매칭한다. 명시적으로 스스로 요청한 것이므로 _persistent_candidate_logs 의 "2회 이상 반복"
    요건은 적용하지 않는다 -- 커맨드를 친 행위 자체가 이미 "지금 도움이 필요하다"는 본인의
    판단이기 때문이다.

    반환: {"is_stuck", "match_found", ...} (match_found=True 면 _verify_and_extract_helper
    필드들도 함께 포함). is_stuck/match_found 가 False 인 경우도 그대로 반환해 호출부가
    적절한 안내 문구를 고를 수 있게 한다.
    """
    others = _other_issue_logs(store, stuck_member["member_id"])
    synthetic_log = {"date": date.today().isoformat(), "text": issue_text}
    prompt = prompts.build_wish_match_prompt(stuck_member, synthetic_log, others)
    text, _, _ = generate_json(prompt)
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return {"is_stuck": False, "match_found": False}

    if not result.get("is_stuck"):
        return {"is_stuck": False, "match_found": False}

    verified = _verify_and_extract_helper(store, result)
    if verified is None:
        return {"is_stuck": True, "match_found": False}
    return {"is_stuck": True, "match_found": True, **verified}


# -- 온디맨드 경로 상태 저장/조회 헬퍼 (socket_app.py 가 버튼 핸들러에서 사용) --

def load_pending():
    return _load_json(WISH_PENDING_PATH, [])


def save_pending(records):
    _save_json(WISH_PENDING_PATH, records)


def find_record(records, wish_id):
    return next((r for r in records if r["id"] == wish_id), None)


def _new_wish_id(records):
    nums = [int(r["id"][1:]) for r in records if r["id"][1:].isdigit()]
    return f"W{(max(nums) + 1) if nums else 1:04d}"


def create_manual_request(records, stuck_member_id, issue_text, match_result):
    """
    /지원요청 커맨드로 만들어지는 대기 레코드. candidate_log_id 가 없으므로(직접 입력한
    텍스트라 실제 slack_logs 항목이 아님) source="slash_command" 로 scan() 이 만든
    레코드와 구분해둔다.
    """
    record = {
        "id": _new_wish_id(records),
        "source": "slash_command",
        "candidate_log_id": None,
        "stuck_member_id": stuck_member_id,
        "issue_text": issue_text,
        "checked_at": _now_iso(),
        "is_stuck": True,
        "match_found": True,
        "status": "awaiting_requester_confirm",
        "helper_member_id": match_result["helper_member_id"],
        "helper_log_id": match_result["helper_log_id"],
        "helper_topic": match_result["helper_topic"],
        "problem_summary": match_result.get("problem_summary", ""),
        "reason": match_result.get("reason", ""),
    }
    records.append(record)
    return record


def transition(records, wish_id, new_status, **extra):
    """레코드 상태를 바꾸고(+ 부가 필드 갱신) 반환한다. 저장은 호출부가 save_pending()으로 한다
    (한 요청 처리 중 Slack API 호출과 뒤섞여 여러 번 갱신될 수 있어, 마지막에 한 번만 쓰기 위함)."""
    record = find_record(records, wish_id)
    if record is None:
        return None
    record["status"] = new_status
    record.update(extra)
    return record


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

    next_n = len(pending) + 1
    made = 0
    for member in store.members:
        member_id = member["member_id"]
        for log in _persistent_candidate_logs(store, member_id):
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

            verified = _verify_and_extract_helper(store, result)
            if result.get("is_stuck") and not verified and result.get("match_found"):
                print(f"  [rejected] {log['log_id']}: LLM이 지목한 helper_log_id="
                      f"{result.get('helper_log_id')!r} / helper_member_id="
                      f"{result.get('helper_member_id')!r} 가 실제 데이터와 불일치해 폐기")
            elif verified:
                helper_id = verified["helper_member_id"]
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
                helper_team = store.members_by_id[helper_id]["team"]
                helper_month_label = f"{int(verified['helper_log']['date'][5:7])}월경"
                msg = (
                    f"🌱 *소원 매칭 제안*\n"
                    f"{stuck_display}님, {helper_display}님({helper_team})이 {helper_month_label} "
                    f"{verified['helper_topic']} 관련 작업 기록이 있어요.\n"
                    f"소원을 보낼까요? *'네'* 라고 답장해주세요."
                )
                dm_channel_id = _open_dm(client, stuck_slack_id)
                resp = client.chat_postMessage(channel=dm_channel_id, text=msg)
                record.update({
                    "match_found": True,
                    "status": "pending",
                    "helper_member_id": helper_id,
                    "helper_log_id": verified["helper_log_id"],
                    "helper_topic": verified["helper_topic"],
                    "problem_summary": verified["problem_summary"],
                    "reason": verified["reason"],
                    "suggestion_ts": resp["ts"],
                    "channel_id": dm_channel_id,
                })
                made += 1
                print(f"  [suggested] {log['log_id']} ({stuck_display}) <- 도움: {helper_display} "
                      f"({verified['helper_log_id']}) via DM")

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
