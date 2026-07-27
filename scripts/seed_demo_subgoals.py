#!/usr/bin/env python3
"""
'하위 목표(건물)' 데모 데이터 시딩.

레몬베이스(외부 OKR 툴) API 연동이 없어서, 그 툴에 원래 있을 법한 goal별 하위 목표 breakdown을
mock으로 채워넣는다. 정성 목표 마일스톤(goal_milestones, 가설수립/실행/결과측정 3단계 고정)과는
완전히 별개의 개념/테이블이다 -- 그건 HR 평가 근거용이고, 이건 홈탭 "진행 중인 목표" 표시용이다.

완료 확정은 2단계: 본인 '완료 신고'(가역, self_reported_at만 채워짐) -> 리더 '확정'(불가역,
confirmed_at/confirmed_by가 채워짐). 이 스크립트는 데모용으로 두 goal(G09, G12)에만 하위 목표를
채운다 -- 전체 19개 목표에 다 채우는 건 이후 필요할 때.

사용 예:
  python3 -m scripts.seed_demo_subgoals
"""

from reports.data_access import get_connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS sub_goals (
    sub_goal_id       TEXT PRIMARY KEY,
    goal_id           TEXT NOT NULL REFERENCES goals(goal_id),
    title             TEXT NOT NULL,
    order_index       INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('미착수','공사중','본인완료','확정완료')) DEFAULT '미착수',
    cumulative_days   INTEGER NOT NULL DEFAULT 0,
    self_reported_at  TEXT,
    confirmed_at      TEXT,
    confirmed_by      TEXT REFERENCES members(member_id),
    UNIQUE (goal_id, order_index)
);

CREATE TABLE IF NOT EXISTS home_view_state (
    member_id TEXT PRIMARY KEY REFERENCES members(member_id),
    last_seen_personal_weekly_generated_at TEXT
);
"""

# goal_id, [(title, status, cumulative_days), ...]  -- order_index는 리스트 순서로 자동 부여
DEMO_SUBGOALS = {
    "G09": [
        ("요구사항 정의", "확정완료", 0),
        ("1차 자동화 스크립트 개발", "공사중", 8),
        ("실제 릴리즈 프로세스 시범 연동", "공사중", 3),
        ("검출 정확도 검증", "미착수", 0),
    ],
    "G12": [
        ("렌더링 이상 감지 알고리즘 구현", "확정완료", 0),
        ("샘플셋 테스트 및 캘리브레이션", "본인완료", 5),  # 리더 확정 대기 상태 데모용
        ("전사 배포 가이드 작성", "미착수", 0),
    ],
}

# goal_id -> 확정완료 항목을 실제로 확정한 리더 member_id (owner 본인이 아니라 그 목표 소유자의
# 상위 평가자). G09 소유자 M07은 R&D 1차평가자 본인이라, 그 위 2차평가자(M08)가 확정한 것으로 시딩.
DEMO_CONFIRMED_BY = {
    "G09": "M08",
    "G12": "M07",
}


def seed():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)

        goal_owner = dict(conn.execute("SELECT goal_id, member_id FROM goals").fetchall())

        for goal_id, subgoals in DEMO_SUBGOALS.items():
            owner = goal_owner.get(goal_id)
            if owner is None:
                print(f"  [skip] {goal_id}: goals 테이블에 없음")
                continue
            for order_index, (title, status, days) in enumerate(subgoals, start=1):
                sub_goal_id = f"{goal_id}-SG{order_index}"
                self_reported_at = "2026-07-20T00:00:00+00:00" if status in ("본인완료", "확정완료") else None
                confirmed_at = "2026-07-22T00:00:00+00:00" if status == "확정완료" else None
                confirmed_by = DEMO_CONFIRMED_BY.get(goal_id) if status == "확정완료" else None
                conn.execute(
                    "INSERT INTO sub_goals (sub_goal_id, goal_id, title, order_index, status, "
                    "cumulative_days, self_reported_at, confirmed_at, confirmed_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(goal_id, order_index) DO UPDATE SET "
                    "title=excluded.title, status=excluded.status, cumulative_days=excluded.cumulative_days, "
                    "self_reported_at=excluded.self_reported_at, confirmed_at=excluded.confirmed_at, "
                    "confirmed_by=excluded.confirmed_by",
                    (sub_goal_id, goal_id, title, order_index, status, days,
                     self_reported_at, confirmed_at, confirmed_by),
                )
            print(f"  [seeded] {goal_id}: 하위목표 {len(subgoals)}개")

        conn.commit()
    finally:
        conn.close()

    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM sub_goals").fetchone()[0]
    conn.close()
    print(f"sub_goals: {count} rows")


if __name__ == "__main__":
    seed()
