#!/usr/bin/env python3
"""
하위 목표(건물) 본인 신고 CLI (가역).

레몬베이스 원본 하위 목표에 대응하는 sub_goals 를, 인터랙티비티 서버가 없는 지금 상황에서
본인이 직접 상태를 갱신하는 수단. slack_app/report_milestone.py 와 동일한 패턴이다.

가역: 본인은 '미착수'/'공사중'/'본인완료' 사이를 자유롭게 오갈 수 있다(사실 정정 포함).
'확정완료'는 이 스크립트로 설정할 수 없다 -- 그건 리더 전용(slack_app/confirm_subgoal.py)이며
한 번 확정되면 되돌릴 수 없다.

사용 예:
  python3 -m slack_app.report_subgoal --goal G09 --subgoal 2 --status 본인완료 --member M07
  python3 -m slack_app.report_subgoal --goal G09 --subgoal 2 --status 공사중 --days 10 --member M07
  python3 -m slack_app.report_subgoal --goal G09 --subgoal 4 --status 미착수 --member M07 --dry-run
"""

import argparse
import sys
from datetime import datetime, timezone

from reports.data_access import DataStore, get_connection

SELF_SETTABLE_STATUSES = ("미착수", "공사중", "본인완료")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def report_subgoal(store, *, goal_id, order_index, status, member_id, days, dry_run=False):
    goal = store.goals_by_id.get(goal_id)
    if goal is None:
        sys.exit(f"goal_id {goal_id!r} 를 찾을 수 없습니다.")
    if goal["member_id"] != member_id:
        sys.exit(f"{goal_id} 의 소유자는 {goal['member_id']} 입니다 -- 본인만 자신의 하위 목표를 신고할 수 있습니다.")

    sub_goals = {sg["order_index"]: sg for sg in store.sub_goals(goal_id)}
    sg = sub_goals.get(str(order_index))
    if sg is None:
        available = ", ".join(sorted(sub_goals, key=int)) or "(없음)"
        sys.exit(f"{goal_id} 의 하위 목표 순번 {order_index} 을 찾을 수 없습니다. 존재하는 순번: {available}")

    if sg["status"] == "확정완료":
        sys.exit(f"{sg['sub_goal_id']} 는 이미 리더가 확정(불가역)했습니다 -- 본인이 되돌릴 수 없습니다.")

    cumulative_days = int(days) if days is not None else int(sg["cumulative_days"])
    self_reported_at = _now_iso() if status == "본인완료" else None  # 미착수/공사중으로 되돌리면 NULL(가역성)

    print(f"[{'dry-run' if dry_run else '신고'}] {sg['sub_goal_id']} ({sg['title']}) "
          f"{sg['status']} -> {status}" + (f" (누적 {cumulative_days}일)" if status == "공사중" else ""))

    if dry_run:
        return

    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE sub_goals SET status = ?, cumulative_days = ?, self_reported_at = ? "
                "WHERE sub_goal_id = ?",
                (status, cumulative_days, self_reported_at, sg["sub_goal_id"]),
            )
    finally:
        conn.close()
    print("  DB 반영 완료.")


def main():
    parser = argparse.ArgumentParser(description="하위 목표(건물) 본인 신고 (가역, 리더 확정은 별도)")
    parser.add_argument("--goal", required=True, help="goal_id (예: G09)")
    parser.add_argument("--subgoal", required=True, type=int, help="하위 목표 순번 (order_index)")
    parser.add_argument("--status", required=True, choices=SELF_SETTABLE_STATUSES, help="신고할 상태")
    parser.add_argument("--member", required=True, help="본인 member_id (그 목표의 소유자와 일치해야 함)")
    parser.add_argument("--days", type=int, help="'공사중' 상태일 때 누적 일수 (미지정 시 기존 값 유지)")
    parser.add_argument("--dry-run", action="store_true", help="실제로 DB에 쓰지 않고 결과만 출력")
    args = parser.parse_args()

    store = DataStore()
    report_subgoal(
        store, goal_id=args.goal, order_index=args.subgoal, status=args.status,
        member_id=args.member, days=args.days, dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
