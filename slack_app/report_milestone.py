#!/usr/bin/env python3
"""
정성 목표 마일스톤 본인 보고 CLI.

인터랙티비티 엔드포인트(Slack 버튼 클릭 처리 서버)가 아직 없어서(다른 버튼들과 동일한 한계,
data_dictionary.md 12-2/13절 참고) 홈 탭 버튼 대신 목표 소유자가 직접 실행하는 CLI로 마일스톤
상태를 self-report 한다. wish_match.py --check-replies 와 같은 "서버 없을 때 수동 CLI로 대체"
패턴이다.

핵심 원칙(리더 승인 없음, 본인 보고만):
- 목표 소유자 본인만 자신의 마일스톤을 보고할 수 있다(--member 가 그 goal의 member_id와
  다르면 거부) -- 남이 대신 보고 못 하게 코드 레벨로 강제한다.
- "완료"로 보고하려면 반드시 --cite 근거(slack_logs log_id)가 있어야 한다 -- 근거 없는 완료
  보고 자체를 쓰기 단계에서 막는다.
- 리더 승인 절차는 없다. 이 리포트는 참고자료이고 리더가 최종 평가를 직접 다시 쓰므로, 리더가
  근거 패키지를 읽는 순간이 검증 단계다. 대신 모든 화면에서 "본인 보고"라고 투명하게 표시한다.

사용 예:
  python3 -m slack_app.report_milestone --goal G09 --milestone 2 --status 완료 --cite L0123,L0130 --member M07
  python3 -m slack_app.report_milestone --goal G09 --milestone 2 --status 완료 --cite L0123 --member M07 --dry-run
"""

import argparse
import sys
from datetime import datetime, timezone

from reports.data_access import DataStore, get_connection

VALID_STATUSES = ("미착수", "진행중", "완료")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def report_milestone(store, *, goal_id, order_index, status, member_id, cite_log_ids, dry_run=False):
    goal = store.goals_by_id.get(goal_id)
    if goal is None:
        sys.exit(f"goal_id {goal_id!r} 를 찾을 수 없습니다.")
    if goal["type"] != "정성":
        sys.exit(f"{goal_id} 는 정량 목표입니다 -- 마일스톤은 정성 목표에만 있습니다.")
    if goal["member_id"] != member_id:
        sys.exit(f"{goal_id} 의 소유자는 {goal['member_id']} 입니다 -- 본인만 자신의 마일스톤을 보고할 수 있습니다.")

    milestone_id = f"{goal_id}-MS{order_index}"
    milestones = {m["milestone_id"]: m for m, _ in store.goal_milestones(goal_id)}
    if milestone_id not in milestones:
        available = ", ".join(sorted(milestones)) or "(없음)"
        sys.exit(f"{milestone_id} 를 찾을 수 없습니다. 이 목표의 마일스톤: {available}")

    if status == "완료" and not cite_log_ids:
        sys.exit("--status 완료 로 보고하려면 --cite 근거(log_id)가 최소 1개 필요합니다.")

    for log_id in cite_log_ids:
        log = store.logs_by_id.get(log_id)
        if log is None:
            sys.exit(f"log_id {log_id!r} 가 존재하지 않습니다.")
        if log["member_id"] != member_id:
            sys.exit(f"log_id {log_id!r} 는 {member_id} 님의 업무일지가 아닙니다.")
        if log["linked_goal_id"] != goal_id:
            sys.exit(f"log_id {log_id!r} 는 {goal_id} 에 연결된 로그가 아닙니다 (연결: {log['linked_goal_id']!r}).")

    reported_at = _now_iso()
    print(f"[{'dry-run' if dry_run else '보고'}] {milestone_id} ({milestones[milestone_id]['title']}) "
          f"-> {status} · 본인 보고: {member_id} ({reported_at})")
    print(f"  근거: {', '.join(cite_log_ids) if cite_log_ids else '(없음)'}")

    if dry_run:
        return

    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE goal_milestones SET status = ?, self_reported_at = ?, reported_by = ? "
                "WHERE milestone_id = ?",
                (status, reported_at, member_id, milestone_id),
            )
            conn.execute("DELETE FROM milestone_evidence WHERE milestone_id = ?", (milestone_id,))
            conn.executemany(
                "INSERT INTO milestone_evidence (milestone_id, log_id) VALUES (?, ?)",
                [(milestone_id, log_id) for log_id in cite_log_ids],
            )
    finally:
        conn.close()
    print("  DB 반영 완료.")


def main():
    parser = argparse.ArgumentParser(description="정성 목표 마일스톤 본인 보고 (리더 승인 없음)")
    parser.add_argument("--goal", required=True, help="goal_id (예: G09)")
    parser.add_argument("--milestone", required=True, type=int, choices=(1, 2, 3), help="마일스톤 순번 (1~3)")
    parser.add_argument("--status", required=True, choices=VALID_STATUSES, help="보고할 상태")
    parser.add_argument("--member", required=True, help="본인 member_id (그 목표의 소유자와 일치해야 함)")
    parser.add_argument("--cite", default="", help="쉼표로 구분된 근거 log_id 목록 (완료 보고 시 필수)")
    parser.add_argument("--dry-run", action="store_true", help="실제로 DB에 쓰지 않고 결과만 출력")
    args = parser.parse_args()

    cite_log_ids = [x.strip() for x in args.cite.split(",") if x.strip()]
    store = DataStore()
    report_milestone(
        store, goal_id=args.goal, order_index=args.milestone, status=args.status,
        member_id=args.member, cite_log_ids=cite_log_ids, dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
