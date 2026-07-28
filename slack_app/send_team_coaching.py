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

    slack_user_id = member_to_slack_id(tc.manager_id)
    if slack_user_id is None:
        sys.exit(
            f"리더 {tc.manager_id} 에 매핑된 Slack 사용자가 data/slack_user_map.json 에 없습니다.\n"
            f'  다음 항목을 추가하세요: {{"<받을 사람의 Slack user_id>": "{tc.manager_id}"}}\n'
            f"  Slack user_id 확인법: Slack 프로필 사진 클릭 > ⋮ 더보기 > \"멤버 ID 복사\""
        )

    store = DataStore()
    manager = store.members_by_id.get(tc.manager_id)
    display_name = display_name_for(manager) if manager else tc.manager_id

    if args.dry_run:
        print(json.dumps({
            "team": args.team, "period": f"{args.start}~{args.end}",
            "manager_id": tc.manager_id, "manager_name": display_name,
            "slack_user_id": slack_user_id, "urgent_count": len(urgent), "card_count": len(cards),
        }, ensure_ascii=False, indent=2))
        return

    from slack_sdk import WebClient
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

    sent = send_coaching_result(client, slack_user_id, args.team, args.start, args.end, cards, urgent,
                                 narration="llm" if used_llm else "template")
    print(f"  [sent] 긴급 알림 {sent['urgent']}건 + 코칭 카드 {sent['cards']}건 "
          f"-> {display_name}({tc.manager_id}) / slack user {slack_user_id}")


if __name__ == "__main__":
    main()
