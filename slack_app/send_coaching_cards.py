# -*- coding: utf-8 -*-
"""
코칭 카드/긴급 알림을 Slack DM으로 "보내는" 기능만 전담하는 모듈.

이전에는 이 발송 로직이 send_team_coaching.py(실시간 생성 후 발송)와
coaching/scenario_validation.py(검증 후 발송) 두 곳에 거의 그대로 복붙돼 있었다. 여기에
한 번만 구현해두고 양쪽에서 재사용한다.

이 모듈을 CLI로 직접 실행하면 "생성"과 완전히 분리해서 -- 이미 만들어진 JSON 결과 파일
(coaching.run / coaching.scenario_validation 이 저장한 output/*.json)을 읽어 그대로
Slack에만 보낼 수 있다(재계산·재생성 없음. 파이프라인을 다시 돌리지 않아도 나중에 다시
같은 결과를 보내거나, 다른 사람에게 전달하고 싶을 때 쓴다).

사용 예:
  # 이미 생성된 결과 파일을 그대로 Slack DM 발송 (manager_id -> Slack 매핑 자동 조회)
  python -m slack_app.send_coaching_cards --from-json output/coaching_사업부_2026-06-08_2026-06-21.json

  # 받을 사람의 Slack user_id를 직접 지정 (매핑 없이)
  python -m slack_app.send_coaching_cards --from-json output/scenario_validation.json --slack-user U0BHY57MBBR

  # 전송 없이 무엇을 보낼지만 확인
  python -m slack_app.send_coaching_cards --from-json output/x.json --dry-run
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

from coaching.schemas import CoachingCard, UrgentAlert
from slack_app.home_view import build_coaching_cards_blocks, build_urgent_alert_blocks


def send_coaching_result(client, slack_user_id, team, period_start, period_end,
                          cards, urgent_alerts, narration="template"):
    """cards/urgent_alerts(coaching.schemas.CoachingCard/UrgentAlert 객체 리스트)를
    slack_user_id 에게 DM으로 보낸다. 긴급 알림을 개별 메시지로 먼저, 배치 카드는 하나의
    메시지로 나중에 보낸다(명세 §7-5: 긴급은 격주를 기다리지 않고 즉시 DM 경로).
    반환: {"urgent": 보낸 긴급 건수, "cards": 보낸 카드 건수}."""
    sent_urgent = 0
    for alert in urgent_alerts:
        msg = build_urgent_alert_blocks(team, alert)
        client.chat_postMessage(channel=slack_user_id, blocks=msg["blocks"], attachments=msg["attachments"],
                                 text=f"[긴급] {team} 팀: {alert.headline}")
        sent_urgent += 1

    msg = build_coaching_cards_blocks(team, str(period_start), str(period_end), cards, narration=narration)
    client.chat_postMessage(channel=slack_user_id, blocks=msg["blocks"], attachments=msg["attachments"],
                             text=f"{team} 팀 코칭 카드 ({period_start}~{period_end})")
    return {"urgent": sent_urgent, "cards": len(cards)}


def _load_payload(path):
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    cards = [CoachingCard(**c) for c in payload.get("cards", [])]
    urgent = [UrgentAlert(**u) for u in payload.get("urgent_alerts", [])]
    return payload, cards, urgent


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")  # 단독 실행 시에만(임포트 시엔 건드리지 않음)
    p = argparse.ArgumentParser(description="이미 생성된 코칭 카드 JSON 결과를 Slack DM으로 발송")
    p.add_argument("--from-json", required=True, help="coaching.run / coaching.scenario_validation 이 저장한 JSON 경로")
    p.add_argument("--slack-user", default=None, help="받을 사람의 Slack user_id (미지정 시 manager_id로 매핑 조회)")
    p.add_argument("--dry-run", action="store_true", help="전송 없이 대상/건수만 확인")
    args = p.parse_args()

    payload, cards, urgent = _load_payload(args.from_json)
    team = payload.get("team_id", "?")
    ps, pe = payload.get("period_start", "?"), payload.get("period_end", "?")
    narration = payload.get("narration", "template")

    slack_user_id = args.slack_user
    if slack_user_id is None:
        from slack_app.member_map import member_to_slack_id
        manager_id = payload.get("manager_id")
        slack_user_id = member_to_slack_id(manager_id) if manager_id else None
        if slack_user_id is None:
            sys.exit(
                f"manager_id={manager_id!r} 에 매핑된 Slack 사용자가 data/slack_user_map.json 에 없습니다.\n"
                f"  --slack-user 로 Slack user_id 를 직접 지정하세요."
            )

    if args.dry_run:
        print(json.dumps({
            "from_json": args.from_json, "team": team, "period": f"{ps}~{pe}",
            "narration": narration, "slack_user_id": slack_user_id,
            "card_count": len(cards), "urgent_count": len(urgent),
        }, ensure_ascii=False, indent=2))
        return

    from env_loader import load_env
    from slack_sdk import WebClient
    load_env()
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

    sent = send_coaching_result(client, slack_user_id, team, ps, pe, cards, urgent, narration=narration)
    print(f"  [sent] 긴급 알림 {sent['urgent']}건 + 코칭 카드 {sent['cards']}건 -> slack user {slack_user_id}")


if __name__ == "__main__":
    main()
