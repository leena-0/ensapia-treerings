#!/usr/bin/env python3
"""
멘토링 데모용 "매일 현황" 홈탭 임시 정적 미리보기.

주의: 이 스크립트는 실제 하위 항목(subitem) 추적 로직을 구현한 것이 아니다. 목업 이미지에
나온 문구를 그대로 하드코딩한 "정적 미리보기"일 뿐이다 (사용자 요청: "구현은 하지 말고 임시로
글만 써줘"). 실제 구획 대시보드로 되돌리려면 `python3 -m slack_app.publish_home --member M01`
을 다시 실행하면 된다 (이 스크립트는 그 홈탭을 일시적으로 덮어쓴다).

도시 뷰(세계 시각화)는 이번 범위 밖이라 텍스트로만 자리를 표시한다.

사용 예:
  python3 -m slack_app.publish_mentoring_preview --member M01
"""

import argparse
import os
import sys

from env_loader import load_env
from reports.data_access import DataStore
from slack_app.member_map import load_member_map, display_name_for


def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _header(text):
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def _context(text):
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _divider():
    return {"type": "divider"}


def build_preview_blocks(display_name):
    return {
        "type": "home",
        "blocks": [
            _header("📋 매일 현황"),
            _context("⚠️ 멘토링 데모용 임시 정적 미리보기입니다 (실제 하위 항목 추적 로직 미구현, 문구는 하드코딩)."),
            _divider(),
            _section(f"*오늘의 현황* — {display_name}"),
            _section(
                "*진행 중인 목표*\n"
                "\n"
                "▸ *아이템 제작 자동화*\n"
                "    ✅ 완공 — 데이터셋 구축\n"
                "    🚧 공사중 — 경량화 CNN 구축  ·  누적 8일\n"
                "    🚧 공사중 — 모델 융합  ·  누적 3일\n"
                "    ⬜ 미착수 — 검증 파이프라인\n"
            ),
            _section(
                "▸ *온보딩 플로우 개선*\n"
                "    🚧 공사중 — 이탈 지점 분석  ·  누적 2일\n"
            ),
            _divider(),
            _section("*확정 대기*        1건\n*받은 씨앗*        2"),
            _divider(),
            _context("[앱에서 도시 보기] (세계/도시 시각화는 이번 범위 밖 — 추후 별도 구현 예정)"),
        ],
    }


def main():
    from slack_sdk import WebClient

    parser = argparse.ArgumentParser(description="멘토링 데모용 임시 홈탭 미리보기 발행")
    parser.add_argument("--member", required=True, help="member_id (예: M01)")
    args = parser.parse_args()

    load_env()
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    store = DataStore()
    member_map = load_member_map()

    slack_user_id = next((sid for sid, mid in member_map.items() if mid == args.member), None)
    if not slack_user_id:
        sys.exit(f"{args.member} 에 매핑된 Slack 사용자가 없습니다.")

    member = store.members_by_id[args.member]
    display_name = display_name_for(member)

    view = build_preview_blocks(display_name)
    client.views_publish(user_id=slack_user_id, view=view)
    print(f"[published] 멘토링 데모 미리보기 -> {display_name}({args.member})")


if __name__ == "__main__":
    main()
