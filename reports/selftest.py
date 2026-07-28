# -*- coding: utf-8 -*-
"""
개인 주간 리포트 / 평가 근거 패키지 / 홈탭 크레인 표시 자체 테스트.

coaching/selftest.py 와 동일한 스타일: LLM은 안 부르고(합성 데이터로 postprocess/표시
로직만 검증), 실제 treerings.db 에도 의존하지 않는다 -- 가벼운 가짜 store로 격리해서
재시드/데이터 변경에 안 깨지게 한다.

실행: python -m reports.selftest
"""
from __future__ import annotations

import io
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from reports.generate_reports import (
    _postprocess_personal,
    _postprocess_evidence,
    _subgoal_next_week_priorities,
    _subgoal_summary_for_goal,
    _subgoal_weekly_status,
)
from reports.progress import SUBGOAL_DAYS_PER_STAGE, SUBGOAL_TOTAL_STAGES, _business_days_elapsed, subgoal_stage
from slack_app.home_view import CHECKIN_VALID_DAYS, _crane_state, _subgoal_display


class FakeStore:
    """_postprocess_personal/_postprocess_evidence/_subgoal_display 가 요구하는 최소 인터페이스만
    구현한 가짜 store. 실제 DataStore(SQLite)와 완전히 격리해서, 시드 데이터가 바뀌어도
    이 테스트들은 항상 같은 결과를 내야 한다."""

    def __init__(self):
        self.members_by_id = {}
        self._sub_goals_by_goal = defaultdict(list)
        self._evidence_log_ids_by_subgoal = defaultdict(list)
        self._logs_by_id = {}
        self._checkins_by_subgoal = defaultdict(list)
        self._milestones_by_goal = defaultdict(list)

    # -- 등록 헬퍼 --
    def add_sub_goal(self, goal_id, sub_goal_id, title, status=None):
        self._sub_goals_by_goal[goal_id].append({"sub_goal_id": sub_goal_id, "title": title, "status": status,
                                                   "confirmed_at": None})

    def add_log(self, log_id, log_date):
        self._logs_by_id[log_id] = {"log_id": log_id, "date": log_date}

    def link_evidence(self, sub_goal_id, log_id):
        self._evidence_log_ids_by_subgoal[sub_goal_id].append(log_id)

    def add_checkin(self, sub_goal_id, week_start, week_end, status, reported_at):
        self._checkins_by_subgoal[sub_goal_id].append(
            {"sub_goal_id": sub_goal_id, "week_start": week_start, "week_end": week_end,
             "status": status, "reported_at": reported_at})

    def add_milestone(self, goal_id, milestone, evidence_logs):
        self._milestones_by_goal[goal_id].append((milestone, evidence_logs))

    # -- DataStore 인터페이스 --
    @property
    def evidence_log_ids_by_subgoal(self):
        return self._evidence_log_ids_by_subgoal

    def sub_goals(self, goal_id):
        return self._sub_goals_by_goal.get(goal_id, [])

    def subgoal_logs(self, sub_goal_id):
        return [self._logs_by_id[lid] for lid in self._evidence_log_ids_by_subgoal.get(sub_goal_id, [])
                if lid in self._logs_by_id]

    def latest_checkin(self, sub_goal_id, week_start=None):
        rows = self._checkins_by_subgoal.get(sub_goal_id, [])
        if week_start is not None:
            return next((r for r in rows if r["week_start"] == week_start), None)
        return rows[-1] if rows else None

    def goal_milestones(self, goal_id):
        return self._milestones_by_goal.get(goal_id, [])


def _worked_days_log_ids(sub_goal_id, n_days, end_date):
    """sub_goal_id 에 n_days 만큼(누적, end_date 로 끝나는) 로그를 붙인 log_id 목록을 반환.
    호출부가 store.add_log/link_evidence 로 실제로 등록해야 한다."""
    return [(f"{sub_goal_id}-L{i}", (end_date - timedelta(days=n_days - 1 - i)).isoformat()) for i in range(n_days)]


def _make_subgoal_with_logs(store, goal_id, sub_goal_id, title, worked_days, end_date, status=None):
    store.add_sub_goal(goal_id, sub_goal_id, title, status=status)
    for log_id, log_date in _worked_days_log_ids(sub_goal_id, worked_days, end_date):
        store.add_log(log_id, log_date)
        store.link_evidence(sub_goal_id, log_id)


def test_subgoal_weekly_status_classification():
    """§5-1 "목표별 상태": 확정완료/이번주 언급(진행)/그 외(미언급)를 정확히 분류하는지."""
    store = FakeStore()
    today = date(2026, 7, 20)
    _make_subgoal_with_logs(store, "G1", "G1-SG1", "완료된 것", 5, today, status="확정완료")
    _make_subgoal_with_logs(store, "G1", "G1-SG2", "이번 주 작업함", 3, today)
    _make_subgoal_with_logs(store, "G1", "G1-SG3", "손도 안 댐", 0, today)

    week_log_ids = {lid for lid, _ in _worked_days_log_ids("G1-SG2", 3, today)}
    result = {r["sub_goal_id"]: r["status"] for r in _subgoal_weekly_status(store, "G1", week_log_ids)}
    assert result == {"G1-SG1": "완료", "G1-SG2": "진행", "G1-SG3": "미언급"}, result
    print("PASS subgoal_weekly_status: 완료/진행/미언급 분류 정확")


def test_next_week_priorities_includes_untouched():
    """2026-07-28 회귀: worked_days==0(미착수)인 것도 확인 요청 대상에 포함돼야 한다
    (예전엔 제외해서 '목표별 상태'엔 미언급으로 보이는데 확인요청엔 안 뜨는 불일치가 있었음)."""
    store = FakeStore()
    today = date(2026, 7, 20)
    _make_subgoal_with_logs(store, "G1", "G1-SG1", "이번주 언급됨", 2, today)
    _make_subgoal_with_logs(store, "G1", "G1-SG2", "미착수", 0, today)
    _make_subgoal_with_logs(store, "G1", "G1-SG3", "확정완료", 5, today, status="확정완료")

    week_log_ids = {lid for lid, _ in _worked_days_log_ids("G1-SG1", 2, today)}
    week_logs = [{"log_id": lid} for lid in week_log_ids]
    out = {item["sub_goal_id"] for item in
           _subgoal_next_week_priorities(store, [{"goal_id": "G1", "title": "목표1"}], week_logs, "2026-07-14")}
    assert out == {"G1-SG2"}, out  # 언급된 SG1 제외, 확정완료 SG3 제외, 미착수 SG2 는 포함
    print("PASS next_week_priorities: worked_days==0 이어도 확인 요청에 포함됨 (회귀 확인)")


def test_checkin_current_status_scoped_to_week():
    """2026-07-28 회귀: 지난 주차 체크인이 이번 주 select 의 '이미 선택됨'으로 보이면 안 된다."""
    store = FakeStore()
    today = date(2026, 7, 20)
    _make_subgoal_with_logs(store, "G1", "G1-SG1", "대상", 0, today)
    store.add_checkin("G1-SG1", "2026-06-01", "2026-06-07", "막힘", "2026-06-05T00:00:00+00:00")

    week_logs = []
    out = _subgoal_next_week_priorities(store, [{"goal_id": "G1", "title": "목표1"}], week_logs, "2026-07-14")
    item = next(x for x in out if x["sub_goal_id"] == "G1-SG1")
    assert item["current_status"] is None, item
    print("PASS checkin current_status: 지난 주차 체크인은 이번 주 select에 안 나타남")


def test_postprocess_personal_drops_hallucinated_citations():
    """§8-1 인용 검증: 이번 주 로그에 없는 log_id 를 인용하면 코드가 폐기해야 한다."""
    store = FakeStore()
    week_logs = [{"log_id": "L1"}]
    content = {
        "goal_progress": [{"goal_id": "G1", "citations": [{"log_id": "L1"}, {"log_id": "L999-지어냄"}]}],
        "highlights": [{"text": "x", "log_ids": ["L1", "L999-지어냄"]}],
        "issues": [],
    }
    out = _postprocess_personal(content, store, [{"goal_id": "G1"}], week_logs, "2026-07-14", "2026-07-20")
    assert out["goal_progress"][0]["citations"] == [{"log_id": "L1"}]
    assert out["highlights"][0]["log_ids"] == ["L1"]
    print("PASS postprocess_personal: 실존하지 않는 log_id 인용 폐기")


def test_postprocess_personal_injects_milestones_regardless_of_llm():
    """정성 목표 마일스톤은 LLM 서술과 무관하게 store 값으로 코드가 덮어써야 한다."""
    store = FakeStore()
    store.add_milestone("G1", {"milestone_id": "G1-MS1", "title": "가설수립", "status": "완료",
                                "self_reported_at": "2026-06-01T00:00:00+00:00", "reported_by": "M01"},
                         [{"log_id": "L1"}])
    content = {"goal_progress": [{"goal_id": "G1", "citations": []}], "highlights": [], "issues": []}
    out = _postprocess_personal(content, store, [{"goal_id": "G1"}], [], "2026-07-14", "2026-07-20")
    ms = out["goal_progress"][0]["milestones"]
    assert len(ms) == 1 and ms[0]["milestone_id"] == "G1-MS1" and ms[0]["evidence_log_ids"] == ["L1"]
    print("PASS postprocess_personal: 마일스톤이 store 값으로 주입됨(LLM 무관)")


def test_subgoal_summary_for_goal_labels():
    """평가 근거 패키지 §5-4 "완료·미완 항목": 확정완료/본인완료/진행중/미착수 라벨이 정확한지."""
    store = FakeStore()
    today = date(2026, 7, 20)
    _make_subgoal_with_logs(store, "G1", "G1-SG1", "확정완료", 25, today, status="확정완료")
    _make_subgoal_with_logs(store, "G1", "G1-SG2", "본인완료 대기", 25, today, status="본인완료")
    _make_subgoal_with_logs(store, "G1", "G1-SG3", "진행중", 5, today)
    _make_subgoal_with_logs(store, "G1", "G1-SG4", "미착수", 0, today)

    labels = {r["sub_goal_id"]: r["status"] for r in _subgoal_summary_for_goal(store, "G1")}
    assert labels == {
        "G1-SG1": "완료(확정)", "G1-SG2": "완료(본인 보고, 리더 확인 대기)",
        "G1-SG3": "진행중", "G1-SG4": "미착수",
    }, labels
    print("PASS subgoal_summary_for_goal: 완료(확정)/완료(본인보고)/진행중/미착수 라벨 정확")


def test_postprocess_evidence_drops_hallucinated_citations():
    store = FakeStore()
    store.members_by_id["M01"] = {"member_id": "M01", "team": "사업부"}
    logs = [{"log_id": "L1", "linked_goal_id": "G1"}]
    goals_with_logs = [({"goal_id": "G1"}, [], logs)]
    content = {"goal_evidence": [{"goal_id": "G1", "citations": [{"log_id": "L1"}, {"log_id": "L999"}]}]}
    out = _postprocess_evidence(content, store, goals_with_logs, "M01", date(2026, 4, 1), date(2026, 6, 30))
    assert out["goal_evidence"][0]["citations"] == [{"log_id": "L1"}]
    print("PASS postprocess_evidence: 실존하지 않는 log_id 인용 폐기")


def test_business_days_elapsed():
    """금요일 작업 후 월요일 아침 = 1영업일 경과 (완성본 §6-2 예시 그대로)."""
    fri, mon = date(2026, 7, 24), date(2026, 7, 27)
    assert _business_days_elapsed(fri, mon) == 1
    assert _business_days_elapsed(fri, fri) == 0
    assert _business_days_elapsed(date(2026, 7, 20), date(2026, 7, 30)) == 8  # 2주 중 주말 2번 빠짐
    print("PASS business_days_elapsed: 주말 제외 영업일 계산 정확")


def test_crane_state_thresholds():
    """작업중 0~2 / 멈춤 3~9 / 장기중단 10+ (완성본 §6-2 임계값)."""
    assert _crane_state(0)[1] == "작업 중"
    assert _crane_state(2)[1] == "작업 중"
    assert "멈춤" in _crane_state(3)[1]
    assert "멈춤" in _crane_state(9)[1]
    assert "장기 중단" in _crane_state(10)[1]
    print("PASS crane_state: 작업중/멈춤/장기중단 임계값 정확")


def test_crane_checkin_override_and_expiry():
    """2026-07-28 회귀: 최신 체크인은 크레인을 덮어쓰고, CHECKIN_VALID_DAYS 지난 체크인은 무시돼야 한다."""
    store = FakeStore()
    today = date(2026, 7, 20)
    _make_subgoal_with_logs(store, "G1", "G1-SG1", "대상", 5, today - timedelta(days=20))  # 오래 전 작업
    sg = store.sub_goals("G1")[0]
    p = subgoal_stage(store, "G1-SG1", today=today)

    # 만료된 체크인(week_end 로부터 CHECKIN_VALID_DAYS 훌쩍 지남) -> 자동판정(장기중단)이 이겨야 함
    old_week_end = (today - timedelta(days=CHECKIN_VALID_DAYS + 5)).isoformat()
    store.add_checkin("G1-SG1", "old", old_week_end, "막힘", f"{old_week_end}T00:00:00+00:00")
    icon, text = _subgoal_display(sg, p, store)
    assert "장기 중단" in text, text

    # 최신 체크인(방금 week_end) -> 오버라이드 적용돼야 함
    fresh_week_end = today.isoformat()
    store.add_checkin("G1-SG1", "fresh", fresh_week_end, "막힘", f"{fresh_week_end}T00:00:00+00:00")
    icon, text = _subgoal_display(sg, p, store)
    assert "막힘" in text, text
    print("PASS crane checkin override: 최신 체크인은 반영, 만료된 체크인은 무시됨 (회귀 확인)")


if __name__ == "__main__":
    test_subgoal_weekly_status_classification()
    test_next_week_priorities_includes_untouched()
    test_checkin_current_status_scoped_to_week()
    test_postprocess_personal_drops_hallucinated_citations()
    test_postprocess_personal_injects_milestones_regardless_of_llm()
    test_subgoal_summary_for_goal_labels()
    test_postprocess_evidence_drops_hallucinated_citations()
    test_business_days_elapsed()
    test_crane_state_thresholds()
    test_crane_checkin_override_and_expiry()
    print("\n✅ 전체 통과")
