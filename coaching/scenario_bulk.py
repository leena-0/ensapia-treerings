# -*- coding: utf-8 -*-
"""
대량 시나리오 배치로 (1) 선별 로직이 새 어휘에도 일반화되는지, (2) 실제 LLM이 카드마다
어떤 문장을 쓰는지를 한 번에 모아 확인하는 도구.

coaching/scenario_validation.py 의 10개 고정 시나리오(진짜 신호 3 + 디코이 7)를 여러
"사이클"(각자 독립된 가짜 팀)로 반복 생성한다. 사이클마다 이름·병목 대상 문구를 바꿔서
같은 구조를 다른 어휘로 재현한다 — 사이클마다 완전히 독립된 팀(FakeStore)이라 사이클
간 어휘가 겹쳐도 서로 영향을 주지 않는다(객체 토큰 배타성 계산이 팀 범위로 한정되므로).

사용법:
  python -m coaching.scenario_bulk --cycles 8             # 기본: 8사이클 x 10시나리오 = 80개, 실제 LLM 서술
  python -m coaching.scenario_bulk --cycles 8 --no-llm    # 서술 없이 선별 정확도만 대량 확인(무료·빠름)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import date, datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from coaching.graph import run_coaching
from coaching.scenario_validation import FakeStore

PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 14)

NAMES = [
    "김도현", "이수민", "박지훈", "최유나", "정민재", "한소영", "윤태오", "서은채", "임재현", "강다은",
    "오승민", "배지원", "노현우", "장서인", "권도윤", "황지민", "송하은", "조민준", "백서윤", "신유진",
    "홍지호", "문채원", "양준서", "임수아", "고은우", "전예린", "심동현", "안서현", "류지안", "곽민서",
]
LEADER_NAMES = ["정하늘", "구자윤", "남지수", "설민아", "천우진", "탁은서", "표승현", "옥다혜"]
BOTTLENECK_TOPICS = [
    "디자인 시안 컨펌", "결제 정책 확정", "인프라 증설 승인", "법무 검토 승인", "브랜드 가이드 확정",
    "보안 심사 승인", "번역 리소스 배정", "재고 확정 승인", "마케팅 예산 승인", "QA 사인오프",
]
REWORK_TOPICS = ["로그인", "결제", "검색", "알림", "온보딩", "정산", "쿠폰", "리뷰"]
ACHIEVEMENT_TOPICS = ["온보딩 퍼널", "결제 재시도", "추천 알고리즘", "푸시 발송", "정산 배치", "검색 랭킹"]


def _build_cycle(idx: int):
    """사이클 하나(팀 1개, 시나리오 10개)를 만든다. idx로 어휘 풀을 회전시켜 사이클마다 다른 이름/대상을 쓴다."""
    team = f"검증팀{idx:02d}"
    leader = LEADER_NAMES[idx % len(LEADER_NAMES)]
    names = [NAMES[(idx * 7 + i) % len(NAMES)] for i in range(12)]
    bt = BOTTLENECK_TOPICS[idx % len(BOTTLENECK_TOPICS)]
    d1t = BOTTLENECK_TOPICS[(idx + 3) % len(BOTTLENECK_TOPICS)]
    d6t = BOTTLENECK_TOPICS[(idx + 5) % len(BOTTLENECK_TOPICS)].split()[0]
    d7a = BOTTLENECK_TOPICS[(idx + 7) % len(BOTTLENECK_TOPICS)]
    d7b = BOTTLENECK_TOPICS[(idx + 9) % len(BOTTLENECK_TOPICS)]
    rt = REWORK_TOPICS[idx % len(REWORK_TOPICS)]
    at = ACHIEVEMENT_TOPICS[idx % len(ACHIEVEMENT_TOPICS)]

    ids = [f"C{idx:02d}L{i:02d}" for i in range(13)]
    members = {ids[0]: {"name": leader, "role": "1차평가자"}}
    for i in range(12):
        members[ids[i + 1]] = {"name": names[i], "role": "팀원"}
    m = {f"m{i}": ids[i] for i in range(1, 13)}  # 시나리오 자리(m1..m12) -> 이 사이클의 실제 member_id

    logs = {
        m["m1"]: [  # 진짜 1 — 의존 병목 (m1, m2 두 명)
            ("2026-07-02", f"{bt}을 요청해뒀는데 아직 회신이 없어 착수를 못 하고 있음."),
            ("2026-07-05", f"{bt} 대기가 길어져서 이번 주 작업도 보류 상태."),
            ("2026-07-08", f"여전히 {bt} 전이라 스펙 확정을 못 하고 대기 중."),
        ],
        m["m2"]: [
            # ({bt} 이 항상 "승인/컨펌"으로 끝난다는 보장이 없으므로, 대기 마커를 토픽과
            # 무관하게 명시적으로 넣는다 — "기다리며"/"회신" 은 토픽이 뭐든 항상 마커로 잡힘.
            ("2026-07-03", f"{bt} 관련 회신을 기다리며 클린업 착수를 미루고 있음."),
            ("2026-07-06", f"{bt} 대기 중, 언제 나올지 몰라 일정 조율이 어려움."),
        ],
        m["m3"]: [  # 진짜 2 — 재작업 반복
            ("2026-07-02", f"QA에서 반려되어 {rt} 플로우를 다시 수정함."),
            ("2026-07-05", f"리뷰 피드백으로 {rt} 화면 롤백 후 재작업 진행."),
            ("2026-07-09", f"세 번째로 같은 {rt} 화면을 다시 작업 중, 요구사항이 계속 바뀜."),
        ],
        m["m4"]: [  # 진짜 3 — 미인지 성과
            ("2026-07-03", f"신규 {at} A/B 테스트 완료, 관련 지표가 상승함."),
            ("2026-07-10", f"{at} 로직 배포 완료로 처리 성공률이 개선됨."),
        ],
        m["m5"]: [("2026-07-04", f"{d1t}을 기다리는 중이라 관련 작업을 보류함.")],  # 디코이1 — 혼자만 대기
        m["m6"]: [("2026-07-04", "인프라 스케일링 관련 도움을 요청함.")],  # 디코이2 — 요청 1건뿐
        m["m7"]: [  # 디코이3 — 수정 2건뿐
            ("2026-07-03", "디자인 피드백으로 배너 문구를 수정함."),
            ("2026-07-07", "다시 한번 배너 문구를 수정함."),
        ],
        m["m8"]: [  # 디코이4 — 부하 편중 임계값 미달
            ("2026-07-01", "이번 주 일정대로 스프린트 작업 진행 중."),
            ("2026-07-04", "특별한 이슈 없이 계획대로 진행."),
            ("2026-07-08", "정기 업무 처리 중, 별다른 특이사항 없음."),
            ("2026-07-11", "이번 주도 계획된 작업 순조롭게 진행."),
        ],
        m["m9"]: [("2026-07-05", "이번 스프린트 목표 하나를 완료함.")],  # 디코이5 — 성과 1건뿐
        m["m10"]: [("2026-07-06", f"일정상 이번 {d6t} 배포는 지연될 가능성이 있어 우려됨.")],  # 디코이6 — 성과+리스크
        m["m11"]: [("2026-07-02", f"{d7a} 승인을 기다리는 중이라 캠페인 착수가 늦어지고 있음.")],  # 디코이7a
        m["m12"]: [("2026-07-09", f"{d7b} 승인을 기다리는 중이라 계약 체결이 늦어지고 있음.")],  # 디코이7b
    }

    expectation = {
        m["m1"]: (True, "의존 병목"), m["m2"]: (True, "의존 병목"),
        m["m3"]: (True, "재작업 반복"), m["m4"]: (True, "미인지 성과"),
        m["m5"]: (False, "혼자만 대기"), m["m6"]: (False, "요청 1건뿐"),
        m["m7"]: (False, "수정 2건뿐"), m["m8"]: (False, "부하 편중 미달"),
        m["m9"]: (False, "성과 1건뿐"), m["m10"]: (False, "성과+리스크 문장"),
        m["m11"]: (False, "서로 다른 대상 대기"), m["m12"]: (False, "서로 다른 대상 대기"),
    }
    return team, members, logs, expectation, ids[0], leader


def _run_cycle(idx: int, use_llm: bool):
    team, members, logs, expectation, leader_id, leader_name = _build_cycle(idx)
    store = FakeStore(team, members, logs)
    result = run_coaching(team, PERIOD_START, PERIOD_END, store=store, use_llm=use_llm, top_n=5)
    cards, urgent = result["cards"], result["urgent_alerts"]
    surfaced = {s: c for c in cards for s in c.subjects}
    urgent_subjects = {s for u in urgent for s in u.subjects}

    mismatches = []
    for mid, (expect_card, reason) in expectation.items():
        got_card, got_urgent = mid in surfaced, mid in urgent_subjects
        if got_urgent:
            mismatches.append(f"{mid}({reason}): 예상치 못한 '긴급 알림'")
        elif got_card != expect_card:
            mismatches.append(f"{mid}({reason}): 기대={expect_card} 실제={got_card}")

    narrations = [
        {"team": team, "leader": leader_name, "card_type": c.card_type, "confidence": c.confidence,
         "headline": c.headline, "body": c.body, "affected_count": c.affected_count,
         "duration_days": c.duration_days}
        for c in cards
    ]
    return {"idx": idx, "team": team, "ok": not mismatches, "mismatches": mismatches,
            "n_cards": len(cards), "n_urgent": len(urgent), "narrations": narrations}


def main():
    p = argparse.ArgumentParser(description="대량 시나리오로 선별 일반화 + LLM 서술 다양성 확인")
    p.add_argument("--cycles", type=int, default=8, help="독립된 팀 사이클 수 (사이클당 시나리오 10개)")
    p.add_argument("--no-llm", action="store_true", help="코드 템플릿으로만(오프라인, 정확도만 대량 확인)")
    p.add_argument("--out", default="output/scenario_bulk.json")
    args = p.parse_args()

    use_llm = not args.no_llm
    results = [_run_cycle(i, use_llm) for i in range(args.cycles)]

    n_total = len(results) * 12
    n_ok_cycles = sum(1 for r in results if r["ok"])
    all_mismatches = [(r["team"], m) for r in results for m in r["mismatches"]]

    print(f"=== 대량 시나리오 검증: {args.cycles}사이클 x 12명 = {n_total}명 "
          f"(서술: {'실제 LLM' if use_llm else '코드 템플릿'}) ===\n")
    for r in results:
        status = "OK" if r["ok"] else f"FAIL({len(r['mismatches'])})"
        print(f"[{status}] {r['team']}: 카드 {r['n_cards']}건, 긴급 {r['n_urgent']}건")

    print(f"\n사이클 {n_ok_cycles}/{len(results)} 완전 일치, 불일치 총 {len(all_mismatches)}건")
    for team, m in all_mismatches:
        print(f"  - {team}: {m}")

    if use_llm:
        print(f"\n=== 실제 LLM 서술 {sum(len(r['narrations']) for r in results)}건 ===")
        for r in results:
            for n in r["narrations"]:
                print(f"\n[{r['team']} / {n['card_type']} / {n['confidence']}] "
                      f"영향 {n['affected_count']}명 · {n['duration_days']}일")
                print(f"  헤드라인: {n['headline']}")
                print(f"  본문: {n['body']}")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({
            "cycles": args.cycles, "narration": "llm" if use_llm else "template",
            "generated_at": datetime.now().isoformat(),
            "cycles_ok": n_ok_cycles, "mismatches": [{"team": t, "detail": m} for t, m in all_mismatches],
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n📄 JSON 로그 저장: {args.out}")
    sys.exit(0 if not all_mismatches else 1)


if __name__ == "__main__":
    main()
