#!/usr/bin/env python3
"""
하위 목표(건물) 리더 확정 CLI (불가역).

본인이 '본인완료'로 신고(report_subgoal.py, 가역)한 하위 목표를, 그 목표 소유자 팀의
1차/2차평가자가 최종 확정한다. 확정되면 되돌리는 경로가 코드에 아예 없다 -- 불가역을
코드 레벨로 강제한다(문서 규칙이 아니라).

'확정 대기 N건' 홈탭 알림([처리] 버튼, 지금은 인터랙티비티 서버가 없어 장식용)은 이 스크립트가
실제 처리 수단이다 -- slack_app/report_milestone.py 와 동일한 "서버 없을 때 CLI로 대체" 패턴.

사용 예:
  python3 -m slack_app.confirm_subgoal --goal G09 --subgoal 2 --leader M08
  python3 -m slack_app.confirm_subgoal --goal G09 --subgoal 2 --leader M08 --dry-run
"""

import argparse
import sys
from datetime import datetime, timezone

from reports.data_access import DataStore, get_connection

LEADER_ROLES = ("1차평가자", "2차평가자")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def confirm_subgoal(store, *, goal_id, order_index, leader_id, dry_run=False):
    goal = store.goals_by_id.get(goal_id)
    if goal is None:
        sys.exit(f"goal_id {goal_id!r} 를 찾을 수 없습니다.")

    leader = store.members_by_id.get(leader_id)
    if leader is None:
        sys.exit(f"leader_id {leader_id!r} 를 찾을 수 없습니다.")
    if leader["role"] not in LEADER_ROLES:
        sys.exit(f"{leader_id}({leader['name']}) 은 1차/2차평가자가 아닙니다 -- 하위 목표를 확정할 수 없습니다.")

    owner = store.members_by_id[goal["member_id"]]
    if leader["team"] != owner["team"]:
        sys.exit(f"{leader_id} 은 {owner['team']} 팀 소속이 아닙니다 -- {goal_id}(소유자 {owner['team']} 팀) 를 확정할 수 없습니다.")
    if leader_id == owner["member_id"]:
        sys.exit(f"{leader_id} 본인 소유 목표는 스스로 확정할 수 없습니다 -- 같은 팀의 다른 1차/2차평가자가 확정해야 합니다 "
                  f"(리더도 리더가 있다는 원칙 -- 1차평가자 본인 것은 2차평가자가, 2차평가자 본인 것은 1차평가자가 확정).")

    sub_goals = {sg["order_index"]: sg for sg in store.sub_goals(goal_id)}
    sg = sub_goals.get(str(order_index))
    if sg is None:
        available = ", ".join(sorted(sub_goals, key=int)) or "(없음)"
        sys.exit(f"{goal_id} 의 하위 목표 순번 {order_index} 을 찾을 수 없습니다. 존재하는 순번: {available}")

    if sg["status"] == "확정완료":
        print(f"[안내] {sg['sub_goal_id']} 는 이미 {sg['confirmed_by']} 가 확정했습니다 (변경 없음).")
        return
    if sg["status"] != "본인완료":
        sys.exit(f"{sg['sub_goal_id']} 는 아직 본인 완료 신고(상태={sg['status']!r})가 안 됐습니다 -- 확정할 대상이 없습니다.")

    confirmed_at = _now_iso()
    print(f"[{'dry-run' if dry_run else '확정'}] {sg['sub_goal_id']} ({sg['title']}) "
          f"본인완료 -> 확정완료 (확정: {leader_id}, {confirmed_at})")

    if dry_run:
        return

    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE sub_goals SET status = '확정완료', confirmed_at = ?, confirmed_by = ? "
                "WHERE sub_goal_id = ?",
                (confirmed_at, leader_id, sg["sub_goal_id"]),
            )
    finally:
        conn.close()
    print("  DB 반영 완료 (불가역 -- 되돌리는 경로 없음).")


def main():
    parser = argparse.ArgumentParser(description="하위 목표(건물) 리더 확정 (불가역, 1차/2차평가자 전용)")
    parser.add_argument("--goal", required=True, help="goal_id (예: G09)")
    parser.add_argument("--subgoal", required=True, type=int, help="하위 목표 순번 (order_index)")
    parser.add_argument("--leader", required=True, help="확정하는 리더의 member_id (1차/2차평가자여야 함)")
    parser.add_argument("--dry-run", action="store_true", help="실제로 DB에 쓰지 않고 결과만 출력")
    args = parser.parse_args()

    store = DataStore()
    confirm_subgoal(store, goal_id=args.goal, order_index=args.subgoal, leader_id=args.leader, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
