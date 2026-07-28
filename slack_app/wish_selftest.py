# -*- coding: utf-8 -*-
"""
소원 매칭(wish_match.py) 자체 테스트. reports/selftest.py 와 동일한 스타일: LLM은 호출하지
않고(순수 로직만 검증), 실제 treerings.db/wish_pending.json 에도 의존하지 않는다.

실행: python -m slack_app.wish_selftest
"""
from __future__ import annotations

import io
import sys
from collections import defaultdict
from datetime import date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from reports.collaboration import quarter_wishes
from slack_app.wish_match import _persistent_candidate_logs, _verify_and_extract_helper, transition


class FakeStore:
    """_persistent_candidate_logs/_verify_and_extract_helper 가 요구하는 최소 인터페이스만 구현."""

    def __init__(self):
        self.logs_by_member = defaultdict(list)
        self.logs_by_id = {}
        self.members_by_id = {}

    def add_log(self, log_id, member_id, log_date, text):
        log = {"log_id": log_id, "member_id": member_id, "date": log_date, "text": text}
        self.logs_by_member[member_id].append(log)
        self.logs_by_id[log_id] = log

    def add_member(self, member_id, name="테스트"):
        self.members_by_id[member_id] = {"member_id": member_id, "name": name}


def test_persistent_candidate_requires_two_distinct_dates():
    """한 번만 언급된 이슈는 자동 제안 후보에서 제외되고(과도한 위임 방지), 날짜만 다르면
    연속이 아니어도(하루 건너뛰어도) 후보가 되어야 한다."""
    store = FakeStore()
    # M01: "PG사 정산" 이슈를 06-01, 06-03(하루 건너뜀)에 반복 언급 -> 후보가 되어야 함
    store.add_log("L1", "M01", "2026-06-01", "PG사 정산 지연 이슈로 확인 요청 드립니다")
    store.add_log("L2", "M01", "2026-06-02", "정상적으로 기능 개발 진행 중")  # 무관한 평시 로그
    store.add_log("L3", "M01", "2026-06-03", "PG사 정산 지연 여전히 미해결 상태입니다")
    out = _persistent_candidate_logs(store, "M01", today=date(2026, 6, 10))
    assert len(out) == 1 and out[0]["log_id"] == "L3", out  # 가장 최근 로그가 대표로 선택됨
    print("PASS persistent_candidate: 서로 다른 날짜 2회 언급 시 후보로 채택(연속 불필요)")


def test_persistent_candidate_excludes_single_mention():
    store = FakeStore()
    store.add_log("L1", "M02", "2026-06-05", "SDK 업그레이드 승인 대기 중입니다")
    store.add_log("L2", "M02", "2026-06-06", "다른 기능 작업 진행")
    out = _persistent_candidate_logs(store, "M02", today=date(2026, 6, 10))
    assert out == [], out
    print("PASS persistent_candidate: 단 한 번의 언급은 후보에서 제외됨 (과도한 위임 방지)")


def test_verify_and_extract_helper_rejects_mismatched_citation():
    """LLM이 지목한 helper_log_id/helper_member_id 가 실제 데이터와 어긋나면 폐기해야 한다
    (data_dictionary.md '인용 검증은 AI를 쓰지 않는다' 원칙)."""
    store = FakeStore()
    store.add_member("M09")
    store.add_log("L100", "M09", "2026-05-01", "iOS 크래시를 메모리 캐싱 로직 수정으로 해결")

    mismatched = {"is_stuck": True, "match_found": True,
                  "helper_member_id": "M09", "helper_log_id": "L999-지어냄"}
    assert _verify_and_extract_helper(store, mismatched) is None
    print("PASS verify_and_extract_helper: 존재하지 않는 log_id 인용은 폐기됨")

    wrong_owner = {"is_stuck": True, "match_found": True,
                   "helper_member_id": "M03", "helper_log_id": "L100"}  # L100은 실제로 M09 소유
    assert _verify_and_extract_helper(store, wrong_owner) is None
    print("PASS verify_and_extract_helper: member_id 불일치 인용은 폐기됨")

    valid = {"is_stuck": True, "match_found": True, "helper_member_id": "M09",
             "helper_log_id": "L100", "helper_topic": "  ", "reason": "해결 이력 있음"}
    verified = _verify_and_extract_helper(store, valid)
    assert verified is not None and verified["helper_log_id"] == "L100"
    assert verified["helper_topic"] == "관련 업무"  # 빈 topic은 기본값으로 대체
    print("PASS verify_and_extract_helper: 유효한 인용은 통과하고 빈 topic은 기본값 처리됨")


def test_transition_updates_status_and_extra_fields():
    records = [{"id": "W0001", "status": "awaiting_requester_confirm", "stuck_member_id": "M01"}]
    out = transition(records, "W0001", "awaiting_helper_response", helper_channel_id="D123")
    assert out["status"] == "awaiting_helper_response" and out["helper_channel_id"] == "D123"
    assert transition(records, "W9999", "cancelled") is None  # 존재하지 않는 id는 조용히 무시
    print("PASS transition: 상태 전이 + 부가 필드 갱신, 존재하지 않는 id는 None")


def test_quarter_wishes_counts_both_completed_statuses():
    """§14 배치 경로(confirmed)와 /지원요청 경로(completed) 둘 다 협업 집계에 포함돼야 한다."""
    records = [
        {"status": "confirmed", "checked_at": "2026-05-01T00:00:00+00:00",
         "stuck_member_id": "M01", "helper_member_id": "M02"},
        {"status": "completed", "checked_at": "2026-05-02T00:00:00+00:00",
         "stuck_member_id": "M01", "helper_member_id": "M03"},
        {"status": "awaiting_helper_response", "checked_at": "2026-05-03T00:00:00+00:00",
         "stuck_member_id": "M01", "helper_member_id": "M04"},  # 아직 미완결 -> 제외돼야 함
    ]
    out = quarter_wishes("M01", records, date(2026, 4, 1), date(2026, 6, 30))
    assert {r["status"] for r in out} == {"confirmed", "completed"}, out
    print("PASS quarter_wishes: confirmed/completed 둘 다 포함, 미완결 상태는 제외")


if __name__ == "__main__":
    test_persistent_candidate_requires_two_distinct_dates()
    test_persistent_candidate_excludes_single_mention()
    test_verify_and_extract_helper_rejects_mismatched_citation()
    test_transition_updates_status_and_extra_fields()
    test_quarter_wishes_counts_both_completed_statuses()
    print("\n✅ 전체 통과")
