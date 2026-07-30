#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
새 코칭 카드 파이프라인(coaching/, LangGraph + 결정론 탐지) 결과를 팀 리더에게 Slack DM으로 발송한다.

기존 slack_app/send_coaching_dm.py 는 구 reports.generate_reports 캐시(category/summary 스키마)를
읽어 보내는 스크립트다. 이 스크립트는 별도이며, coaching.graph.run_coaching 을 직접 호출해
그 결과(headline/body/confidence/evidence 스키마)를 그대로 Slack 블록으로 렌더링해 보낸다.
캐시를 거치지 않으므로 먼저 reports.generate_reports 를 실행해둘 필요가 없다.

발송 대상 = coaching.data_adapter.build_team_context 가 정한 팀의 관리 책임자(1차평가자).
그 member_id 를 data/slack_user_map.json 에서 Slack user_id 로 역매핑해 chat.postMessage 로 보낸다.
매핑이 없으면 무엇을 추가해야 하는지 안내하고 종료한다(가짜로 아무 데나 보내지 않음).

1차평가자 본인이 카드/알림의 대상(subjects)으로 걸리는 경우(1차평가자도 그 팀 구성원이라
자기 업무일지를 쓰고, 5종 탐지기가 팀 전체를 도는 과정에서 본인이 병목 당사자로 잡힐 수 있다)는
자기참조를 피하기 위해 2차평가자에게 대신 보낸다(_split_by_recipient). 2차평가자가 없거나
Slack 매핑이 없으면 1차평가자에게 그대로 남긴다.

긴급 알림은 격주 배치와 분리해 개별 메시지로 먼저 보낸다(명세 §7-5, 즉시 DM 경로).

사용 예:
  # 실제 Gemini로 서술 생성 후 발송
  python -m slack_app.send_team_coaching --team 사업부 --start 2026-06-08 --end 2026-06-21

  # LLM 없이(오프라인) 코드 템플릿 서술로 발송 — API 없이 형태만 확인
  python -m slack_app.send_team_coaching --team 사업부 --start 2026-06-08 --end 2026-06-13 --no-llm

  # Slack 전송 없이 대상/건수만 확인
  python -m slack_app.send_team_coaching --team 사업부 --start 2026-06-08 --end 2026-06-21 --dry-run
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from env_loader import load_env
from coaching.graph import run_coaching
from reports.data_access import DataStore
from slack_app.member_map import display_name_for, member_to_slack_id
from slack_app.send_coaching_cards import send_coaching_result


def _split_by_recipient(items, manager_id):
    """1차평가자(manager_id) 본인이 카드/알림의 대상(subjects)에 포함된 항목은 2차평가자에게
    올려보내고, 그 외 일반 팀원 대상 항목은 그대로 1차평가자에게 남긴다.

    이유: detect_signals/5종 탐지기는 팀 전체 로그를 도는데, 1차평가자도 그 팀의 members에
    포함돼 자기 업무일지를 쓰는 한 사람이다. 그래서 1차평가자 본인이 병목 당사자로 잡히는
    카드가 나올 수 있는데, 지금까지는 그 카드가 1차평가자 본인에게 그대로 갔다 -- 자기 문제를
    자기가 받는 자기참조 구조였다. 이미 탐지되고 있던 신호를 새로 만들 필요 없이, "누구에게
    보내는가"만 바꿔서 해결한다."""
    normal, escalate = [], []
    for item in items:
        (escalate if manager_id in item.subjects else normal).append(item)
    return normal, escalate


def main():
    p = argparse.ArgumentParser(description="새 코칭 카드 파이프라인 결과를 팀 리더에게 Slack DM 발송")
    p.add_argument("--team", default="사업부")
    p.add_argument("--start", default="2026-06-08", help="기간 시작 (YYYY-MM-DD)")
    p.add_argument("--end", default="2026-06-21", help="기간 끝 (YYYY-MM-DD, 격주=14일)")
    p.add_argument("--no-llm", action="store_true", help="LLM 없이 코드 템플릿으로 서술(오프라인)")
    p.add_argument("--dry-run", action="store_true", help="Slack 전송 없이 발송 대상/건수만 콘솔에 출력")
    args = p.parse_args()

    load_env()
    ps, pe = date.fromisoformat(args.start), date.fromisoformat(args.end)
    used_llm = not args.no_llm

    result = run_coaching(args.team, ps, pe, use_llm=used_llm)
    tc = result["team_context"]
    cards, urgent = result["cards"], result["urgent_alerts"]

    store = DataStore()

    manager_cards, escalate_cards = _split_by_recipient(cards, tc.manager_id)
    manager_urgent, escalate_urgent = _split_by_recipient(urgent, tc.manager_id)

    # 2차평가자가 없거나 Slack 매핑이 없으면, 올려보내려던 항목을 잃어버리지 않도록
    # 1차평가자 쪽으로 다시 합친다(자기참조보다 아예 전달 안 되는 쪽이 더 나쁘다).
    senior_slack_id = member_to_slack_id(tc.senior_manager_id) if tc.senior_manager_id else None
    if senior_slack_id is None:
        manager_cards = manager_cards + escalate_cards
        manager_urgent = manager_urgent + escalate_urgent
        escalate_cards, escalate_urgent = [], []

    slack_user_id = member_to_slack_id(tc.manager_id)
    if slack_user_id is None:
        sys.exit(
            f"리더 {tc.manager_id} 에 매핑된 Slack 사용자가 data/slack_user_map.json 에 없습니다.\n"
            f'  다음 항목을 추가하세요: {{"<받을 사람의 Slack user_id>": "{tc.manager_id}"}}\n'
            f"  Slack user_id 확인법: Slack 프로필 사진 클릭 > ⋮ 더보기 > \"멤버 ID 복사\""
        )

    manager = store.members_by_id.get(tc.manager_id)
    display_name = display_name_for(manager) if manager else tc.manager_id
    senior = store.members_by_id.get(tc.senior_manager_id) if tc.senior_manager_id else None
    senior_display_name = display_name_for(senior) if senior else None

    if args.dry_run:
        print(json.dumps({
            "team": args.team, "period": f"{args.start}~{args.end}",
            "manager_id": tc.manager_id, "manager_name": display_name,
            "slack_user_id": slack_user_id,
            "urgent_count": len(manager_urgent), "card_count": len(manager_cards),
            "senior_manager_id": tc.senior_manager_id or None, "senior_manager_name": senior_display_name,
            "senior_slack_user_id": senior_slack_id,
            "escalated_urgent_count": len(escalate_urgent), "escalated_card_count": len(escalate_cards),
        }, ensure_ascii=False, indent=2))
        return

    from slack_sdk import WebClient
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    narration = "llm" if used_llm else "template"

    sent = send_coaching_result(client, slack_user_id, args.team, args.start, args.end,
                                 manager_cards, manager_urgent, narration=narration)
    print(f"  [sent] 긴급 알림 {sent['urgent']}건 + 코칭 카드 {sent['cards']}건 "
          f"-> {display_name}({tc.manager_id}) / slack user {slack_user_id}")

    if senior_slack_id is not None and (escalate_cards or escalate_urgent):
        sent_senior = send_coaching_result(client, senior_slack_id, args.team, args.start, args.end,
                                            escalate_cards, escalate_urgent, narration=narration)
        print(f"  [sent] (1차평가자 본인 관련) 긴급 알림 {sent_senior['urgent']}건 + "
              f"코칭 카드 {sent_senior['cards']}건 -> {senior_display_name}({tc.senior_manager_id}) "
              f"/ slack user {senior_slack_id}")


if __name__ == "__main__":
    main()
