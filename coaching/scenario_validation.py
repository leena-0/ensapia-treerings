# -*- coding: utf-8 -*-
"""
설계한 가중치(v10.2 확정 공식)와 탐지 로직이 실제로 "보내야 할 신호만" 골라내는지 검증하는
독립 시나리오 테스트.

실제 사업부 CSV mock 데이터는 건드리지 않는다 — 이 스크립트 안에서 가짜 팀("테스트팀")과
멤버·로그를 만들어 coaching.graph.run_coaching 전체 파이프라인(탐지 5종 → 병합 → 근거검증 →
확신도 → v10.2 점수식 → 슬롯 분리 선별)에 그대로 흘려보낸다.

10개 시나리오 중 3개만 "진짜 신호"(카드가 되어야 함)이고, 나머지 7개는 서로 다른 이유로
걸러져야 하는 디코이다:
  - 진짜 1: T02/T03 — 의존 병목 (2명, 확신도 high)
  - 진짜 2: T04     — 재작업 반복 (1명, 확신도 medium)
  - 진짜 3: T05     — 미인지 성과 (1명, 확신도 medium, 인정 슬롯)
  - 디코이 1: T06     — 혼자만 대기 중 (2인 이상 군집이 안 되므로 병목 아님)
  - 디코이 2: T07     — 요청 1건뿐, 재언급 없음 (근거 2건 미만 → verify_evidence 폐기)
  - 디코이 3: T08     — 수정 2건뿐 (재작업 반복 임계값 3건 미달)
  - 디코이 4: T09     — 로그량이 팀 평균보다 살짜 많지만 부하 편중 임계값(1.6배·+3) 미달
  - 디코이 5: T10     — 성과 언급 1건뿐 (서로 다른 성과 2건 미달)
  - 디코이 6: T11     — "배포"라는 성과 마커가 있지만 실제로는 "지연 우려"라는 리스크 문장
  - 디코이 7: T12/T13 — 둘 다 "승인 대기 중"이지만 서로 다른 대상을 기다림 (허위 병목 아님)

실행 결과가 기대와 일치하는지 자동으로 대조해 PASS/FAIL을 출력하고, JSON 로그도 저장한다.

사용법:
  python -m coaching.scenario_validation            # 코드 템플릿 서술(오프라인, 기본, 무료)
  python -m coaching.scenario_validation --llm      # 실제 Gemini 서술까지 확인
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import date, datetime, time

from coaching.graph import run_coaching

TEAM = "테스트팀"
PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 14)

MEMBERS = {
    "T01": {"name": "정하늘", "role": "1차평가자"},   # 리더 — 카드 수신자, 시나리오 대상 아님
    "T02": {"name": "김도현", "role": "팀원"},
    "T03": {"name": "이수민", "role": "팀원"},
    "T04": {"name": "박지훈", "role": "팀원"},
    "T05": {"name": "최유나", "role": "팀원"},
    "T06": {"name": "정민재", "role": "팀원"},
    "T07": {"name": "한소영", "role": "팀원"},
    "T08": {"name": "윤태오", "role": "팀원"},
    "T09": {"name": "서은채", "role": "팀원"},
    "T10": {"name": "임재현", "role": "팀원"},
    "T11": {"name": "강다은", "role": "팀원"},
    "T12": {"name": "오승민", "role": "팀원"},
    "T13": {"name": "배지원", "role": "팀원"},
}

# member_id -> [(날짜, 본문), ...]
LOGS = {
    # 진짜 1 — 의존 병목: 2명이 같은 "디자인 시안 컨펌"을 기다림. explicit(대기 로그)=5 → high.
    "T02": [
        ("2026-07-02", "디자인 시안 컨펌을 요청해뒀는데 아직 회신이 없어 착수를 못 하고 있음."),
        ("2026-07-05", "디자인 시안 컨펌 대기가 길어져서 이번 주 작업도 보류 상태."),
        ("2026-07-08", "여전히 디자인 시안 컨펌 전이라 스펙 확정을 못 하고 대기 중."),
    ],
    "T03": [
        ("2026-07-03", "디자인 시안 컨펌이 안 나서 클린업 착수를 미루고 있음."),
        ("2026-07-06", "디자인 시안 컨펌 대기 중, 언제 나올지 몰라 일정 조율이 어려움."),
    ],
    # 진짜 2 — 재작업 반복: 같은 화면이 3번 수정/롤백/재작업됨. affected=1 → medium.
    "T04": [
        ("2026-07-02", "QA에서 반려되어 로그인 플로우를 다시 수정함."),
        ("2026-07-05", "리뷰 피드백으로 결제 화면 롤백 후 재작업 진행."),
        ("2026-07-09", "세 번째로 같은 화면을 다시 작업 중, 요구사항이 계속 바뀜."),
    ],
    # 진짜 3 — 미인지 성과: 서로 다른 성과 2건, 리스크 표현 없음. affected=1 → medium(인정 슬롯).
    "T05": [
        ("2026-07-03", "신규 온보딩 퍼널 A/B 테스트 완료, 가입 완료율이 12%p 상승함."),
        ("2026-07-10", "결제 재시도 로직 배포 완료로 결제 성공률이 개선됨."),
    ],
    # 디코이 1 — 혼자만 대기: 2인 이상이 모여야 병목인데 1명뿐 → 후보 자체가 안 생김.
    "T06": [
        ("2026-07-04", "결제 정책 확정을 기다리는 중이라 관련 작업을 보류함."),
    ],
    # 디코이 2 — 요청 1건, 재언급 없음 → 근거 2건 미만으로 verify_evidence 에서 폐기.
    "T07": [
        ("2026-07-04", "인프라 스케일링 관련 도움을 요청함."),
    ],
    # 디코이 3 — 수정 2건뿐 (재작업 반복 임계값은 3건) → 후보 자체가 안 생김.
    "T08": [
        ("2026-07-03", "디자인 피드백으로 배너 문구를 수정함."),
        ("2026-07-07", "다시 한번 배너 문구를 수정함."),
    ],
    # 디코이 4 — 로그량이 평균보다 조금 많지만 부하 편중 임계값(1.6배 and +3) 미달.
    "T09": [
        ("2026-07-01", "이번 주 일정대로 스프린트 작업 진행 중."),
        ("2026-07-04", "특별한 이슈 없이 계획대로 진행."),
        ("2026-07-08", "정기 업무 처리 중, 별다른 특이사항 없음."),
        ("2026-07-11", "이번 주도 계획된 작업 순조롭게 진행."),
    ],
    # 디코이 5 — 성과 언급이 1건뿐 → 서로 다른 성과 2건 미달로 폐기.
    "T10": [
        ("2026-07-05", "이번 스프린트 목표 하나를 완료함."),
    ],
    # 디코이 6 — "배포"라는 성과 마커는 있지만 실제로는 지연 리스크 문장 → 성과로 안 잡혀야 함.
    "T11": [
        ("2026-07-06", "일정상 이번 배포는 지연될 가능성이 있어 우려됨."),
    ],
    # 디코이 7 — 둘 다 "승인 대기 중"이지만 서로 다른 대상(마케팅 예산 vs 법무 검토)을 기다림.
    # 표현이 겹친다고 같은 병목으로 묶이면 안 된다.
    "T12": [
        ("2026-07-02", "마케팅 예산 승인을 기다리는 중이라 캠페인 착수가 늦어지고 있음."),
    ],
    "T13": [
        ("2026-07-09", "법무 검토 승인을 기다리는 중이라 계약 체결이 늦어지고 있음."),
    ],
}

# subject(member_id) -> 이 시나리오가 최종적으로 카드가 되어야 하는지 + 이유
EXPECTATION = {
    "T02": (True, "의존 병목(T02/T03) — 확신도 high 기대"),
    "T03": (True, "의존 병목(T02/T03) — 확신도 high 기대"),
    "T04": (True, "재작업 반복 — 확신도 medium 기대"),
    "T05": (True, "미인지 성과 — 확신도 medium, 인정 슬롯 기대"),
    "T06": (False, "혼자만 대기 — 2인 미달로 후보 자체가 생기지 않아야 함"),
    "T07": (False, "요청 1건뿐 — 근거 2건 미만으로 폐기돼야 함"),
    "T08": (False, "수정 2건뿐 — 재작업 임계값(3건) 미달로 후보가 생기지 않아야 함"),
    "T09": (False, "부하 편중 임계값 미달 — 후보가 생기지 않아야 함"),
    "T10": (False, "성과 1건뿐 — 서로 다른 성과 2건 미달로 폐기돼야 함"),
    "T11": (False, "성과 마커가 있어도 리스크 문장 — 성과로 잡히면 안 됨"),
    "T12": (False, "서로 다른 대상 대기 — 허위 병목으로 묶이면 안 됨"),
    "T13": (False, "서로 다른 대상 대기 — 허위 병목으로 묶이면 안 됨"),
}


class FakeStore:
    """coaching.data_adapter 가 기대하는 최소 인터페이스만 흉내낸 가짜 저장소."""

    def __init__(self, team, members, logs):
        self.members_by_team = {team: list(members.keys())}
        self.members_by_id = members
        self.logs_by_member = {
            mid: [{"log_id": f"{mid}-{i:02d}", "date": d, "text": t} for i, (d, t) in enumerate(entries, 1)]
            for mid, entries in logs.items()
        }
        # detect_blocked_escalation(§7 세 번째 긴급 조건)이 추가되면서 load_weekly_status가
        # 이 속성을 요구하게 됐다 -- 이 시나리오들엔 자기신고 blocked 케이스가 없으므로 빈
        # 목록이면 충분하다(subgoal_checkins_for_team이 빈 리스트를 순회하고 끝남).
        self.subgoal_checkin_rows = []


def _print_report(result):
    cards, urgent = result["cards"], result["urgent_alerts"]
    surfaced = {s: c for c in cards for s in c.subjects}
    urgent_subjects = {s for u in urgent for s in u.subjects}

    print(f"=== 시나리오 검증: {TEAM} / {PERIOD_START} ~ {PERIOD_END} ===")
    print(f"카드 {len(cards)}건, 긴급 알림 {len(urgent)}건 생성\n")

    ok = True
    for mid, (expect_card, reason) in EXPECTATION.items():
        got_card = mid in surfaced
        got_urgent = mid in urgent_subjects
        status = "PASS" if (got_card == expect_card and not got_urgent) else "FAIL"
        if status == "FAIL":
            ok = False
        detail = ""
        if got_card:
            c = surfaced[mid]
            detail = f" -> [{c.card_type}] 확신도={c.confidence} score={c.priority_score} 헤드라인: {c.headline}"
        elif got_urgent:
            detail = " -> 예상치 못하게 '긴급 알림'으로 분류됨"
        print(f"[{status}] {mid} ({MEMBERS[mid]['name']}): {reason}{detail}")

    n_expected = sum(1 for v in EXPECTATION.values() if v[0])
    n_surfaced_expected = len({s for s in surfaced if EXPECTATION[s][0]})
    n_false_positive = len({s for s in surfaced if not EXPECTATION[s][0]})
    print(f"\n기대한 진짜 신호 대상자 수: {n_expected}명 / 실제로 카드화된 진짜 신호 대상자: {n_surfaced_expected}명")
    print(f"디코이가 잘못 카드화된 건수(오탐): {n_false_positive}건")
    print(f"카드 총 {len(cards)}건 (기대: 3건 근처 — 의존 병목 1 + 재작업 1 + 인정 1)")
    print("\n" + ("✅ 전체 기대와 일치" if ok and n_false_positive == 0 else "❌ 기대와 불일치 — 위 FAIL 항목 확인"))
    return ok and n_false_positive == 0


def _resolve_slack_user(manager_id):
    """검증용 가짜 리더(T01)는 data/slack_user_map.json 에 매핑이 없다. 실제 매핑된 사람이
    정확히 1명(이 스크립트를 돌리는 본인)이면 그 사람에게 보낸다 — 애매하면 보내지 않는다."""
    from slack_app.member_map import load_member_map, member_to_slack_id
    sid = member_to_slack_id(manager_id)
    if sid:
        return sid
    mapping = load_member_map()
    if len(mapping) == 1:
        return next(iter(mapping))
    return None


def _send_to_slack(cards, urgent, narration="template"):
    from slack_sdk import WebClient

    from env_loader import load_env
    from slack_app.send_coaching_cards import send_coaching_result

    load_env()
    slack_user_id = _resolve_slack_user("T01")
    if slack_user_id is None:
        sys.exit("data/slack_user_map.json 에 매핑된 사람이 여러 명이라 누구에게 보낼지 애매합니다. "
                 "--slack-user 로 Slack user_id 를 직접 지정하세요.")

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    sent = send_coaching_result(client, slack_user_id, TEAM, PERIOD_START, PERIOD_END, cards, urgent,
                                 narration=narration)
    print(f"  [sent] 긴급 알림 {sent['urgent']}건 + 코칭 카드 {sent['cards']}건 -> slack user {slack_user_id}")


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")  # 단독 실행 시에만(임포트 시엔 건드리지 않음)
    p = argparse.ArgumentParser(description="10개 시나리오로 코칭 카드 선별 로직 검증")
    p.add_argument("--llm", action="store_true", help="코드 템플릿 대신 실제 Gemini로 서술")
    p.add_argument("--slack", action="store_true", help="선별된 카드/긴급 알림을 실제 Slack DM으로도 발송")
    p.add_argument("--out", default="output/scenario_validation.json")
    args = p.parse_args()

    store = FakeStore(TEAM, MEMBERS, LOGS)
    result = run_coaching(TEAM, PERIOD_START, PERIOD_END, store=store, use_llm=args.llm, top_n=5)
    passed = _print_report(result)

    if args.slack:
        _send_to_slack(result["cards"], result["urgent_alerts"], narration="llm" if args.llm else "template")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    payload = {
        "team_id": TEAM, "period_start": str(PERIOD_START), "period_end": str(PERIOD_END),
        "narration": "llm" if args.llm else "template",
        "generated_at": datetime.now().isoformat(),
        "cards": [c.model_dump(mode="json") for c in result["cards"]],
        "urgent_alerts": [u.model_dump(mode="json") for u in result["urgent_alerts"]],
        "expectation_met": passed,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n📄 JSON 로그 저장: {args.out}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
