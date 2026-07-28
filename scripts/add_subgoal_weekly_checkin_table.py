#!/usr/bin/env python3
"""
subgoal_weekly_checkin 테이블 추가 (기존 data/treerings.db에 1회 적용).

개인 주간 리포트의 "확인 요청"(프로젝트 개요 5-1절 "이 시스템의 관문") 구현: 이번 주 로그가
없었던 하위 목표(건물)에 대해 본인이 진행중/대기/보류/막힘 중 하나를 Slack 메시지의 select
메뉴로 직접 선택한 이력을 저장한다. sub_goals.status(본인완료/확정완료, 불가역 완료 판정)와는
완전히 다른 축이라 같은 컬럼에 섞지 않는다 (6-2절 표 그대로):

  - 진행중: 기록에 안 남았을 뿐 실제로는 작업 중 -> 크레인 상태 그대로 유지
  - 대기:   타인/외부 요인으로 못 하고 있음      -> 크레인 "멈춤"으로 즉시 표시
  - 보류:   본인 의도로 미룸                     -> 크레인 "장기중단"으로 즉시 표시(경과일 무관)
  - 막힘:   문제로 못 하고 있음                  -> 크레인 "멈춤"+문제 표시, 코칭 카드 후보로 전달

같은 주(week_start)에 다시 선택하면 upsert (가역 -- 정정 가능, PK가 sub_goal_id+week_start).

사용 예:
  python3 -m scripts.add_subgoal_weekly_checkin_table
"""

from reports.data_access import get_connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS subgoal_weekly_checkin (
    sub_goal_id TEXT NOT NULL REFERENCES sub_goals(sub_goal_id),
    week_start  TEXT NOT NULL,
    week_end    TEXT NOT NULL,
    member_id   TEXT NOT NULL REFERENCES members(member_id),
    status      TEXT NOT NULL CHECK (status IN ('진행중','대기','보류','막힘')),
    reported_at TEXT NOT NULL,
    PRIMARY KEY (sub_goal_id, week_start)
);
CREATE INDEX IF NOT EXISTS idx_checkin_member ON subgoal_weekly_checkin(member_id);
"""


def main():
    conn = get_connection()
    try:
        with conn:
            conn.executescript(SCHEMA)
        print("subgoal_weekly_checkin 테이블 준비 완료 (이미 있었다면 변경 없음).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
