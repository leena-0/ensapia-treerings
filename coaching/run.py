# -*- coding: utf-8 -*-
"""
코칭 카드 생성 실행 CLI — 결과를 콘솔에 보여주고 JSON 로그로 저장한다.

사용 예:
  # 실제 LLM으로 서술 생성 (LLM_PROVIDER 환경변수, 기본 Solar Pro)
  python -m coaching.run --team 사업부 --start 2026-06-08 --end 2026-06-21

  # LLM 없이(오프라인) 코드 템플릿 서술 — API/쿼터 없이 결과 확인
  python -m coaching.run --team 사업부 --start 2026-06-08 --end 2026-06-21 --no-llm

  # 저장 위치 지정
  python -m coaching.run --out output/사업부.json

탐지·선별·인용 검증은 항상 코드가 결정론적으로 수행하며, --no-llm 이면 서술만 템플릿으로 대체된다.
결과 JSON: output/coaching_<team>_<start>_<end>.json (기본)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import date, datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from coaching.graph import run_coaching


def _provider_label():
    """실제 호출되는 프로바이더 이름(LLM_PROVIDER)을 그대로 보여준다.
    예전엔 "Gemini"로 하드코딩돼 있어서, 기본 프로바이더를 Solar Pro로 바꾼 뒤에도
    콘솔에 계속 "Gemini"라고 잘못 찍혔다(reports.llm_client의 실제 동작과 라벨이 따로 놀았음)."""
    provider = os.environ.get("LLM_PROVIDER", "upstage").strip().lower()
    return "Solar Pro" if provider == "upstage" else "Gemini"


def _print_summary(team, ps, pe, manager_id, cards, urgent, used_llm):
    print(f"=== 코칭 카드 결과: {team} / {ps} ~ {pe} ===")
    print(f"서술 방식: {f'실제 LLM({_provider_label()})' if used_llm else '코드 템플릿(오프라인)'}")
    print(f"수신 관리 책임자: {manager_id}\n")

    print(f"--- 코칭 카드 {len(cards)}건 ---")
    for c in cards:
        print(f"\n[{c.card_type}] 확신도={c.confidence} score={c.priority_score} "
              f"영향={c.affected_count}명 지속={c.duration_days}일")
        print(f"  헤드라인: {c.headline}")
        print(f"  본문: {c.body}")
        print(f"  대상자: {c.subjects}")
        for e in c.evidence:
            print(f"    근거 {e.message_id} ({e.user_name}) {e.permalink}: {e.excerpt}")

    print(f"\n--- 긴급 알림 {len(urgent)}건 (즉시 DM 경로) ---")
    for u in urgent:
        print(f"\n[URGENT/{u.card_type}] 사유={u.reason}")
        print(f"  {u.headline}")
        print(f"  대상자: {u.subjects}")
        for e in u.evidence:
            print(f"    근거 {e.message_id} ({e.user_name}): {e.excerpt}")


def main():
    p = argparse.ArgumentParser(description="ENSAPIA 코칭 카드 생성/조회 실행")
    p.add_argument("--team", default="사업부")
    p.add_argument("--start", default="2026-06-08", help="기간 시작 (YYYY-MM-DD)")
    p.add_argument("--end", default="2026-06-21", help="기간 끝 (YYYY-MM-DD, 격주=14일)")
    p.add_argument("--no-llm", action="store_true", help="LLM 없이 코드 템플릿으로 서술(오프라인)")
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--out", default=None, help="JSON 저장 경로 (기본 output/coaching_<team>_<기간>.json)")
    args = p.parse_args()

    ps, pe = date.fromisoformat(args.start), date.fromisoformat(args.end)
    used_llm = not args.no_llm

    result = run_coaching(args.team, ps, pe, use_llm=used_llm, top_n=args.top_n)
    tc = result["team_context"]
    cards, urgent = result["cards"], result["urgent_alerts"]

    _print_summary(args.team, ps, pe, tc.manager_id, cards, urgent, used_llm)

    # JSON 로그 저장
    out = args.out or os.path.join("output", f"coaching_{args.team}_{args.start}_{args.end}.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    payload = {
        "team_id": args.team,
        "period_start": args.start,
        "period_end": args.end,
        "manager_id": tc.manager_id,
        "narration": "llm" if used_llm else "template",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cards": [c.model_dump(mode="json") for c in cards],
        "urgent_alerts": [u.model_dump(mode="json") for u in urgent],
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n📄 JSON 로그 저장: {out}")


if __name__ == "__main__":
    main()
