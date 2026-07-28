#!/usr/bin/env python3
"""
main 브랜치가 `seed/generate_mock_data.py`에 심어둔 코칭 카드 탐지기 검증용 로그
(`PLANTED_LOGS`, 38건 -- 의존 병목/미해결 요청/재작업 반복/부하 편중/미인지 성과 5종)를
기존 `data/treerings.db`(SQLite)에 반영하는 1회성 스크립트.

왜 CSV를 그대로 재이전하지 않는가: main의 `slack_logs.csv`는 `PLANTED_LOGS`를 날짜순
재정렬에 포함시켜 만들었기 때문에, 삽입 지점 이후 로그 265건 중 164건의 log_id가
**기존과 다른 내용을 가리키도록 재배정**됐다(직접 대조로 확인). 이 DB는 이미 그 옛
log_id를 참조하는 실데이터를 갖고 있다(sub_goals/subgoal_evidence 배분, goal_milestones/
milestone_evidence, report_cache에 캐시된 실제 리포트의 citation, 실제 ingest.py로
수집된 로그의 permalink 등) -- CSV를 그대로 재이전하면 이 참조들이 전부 다른 내용을
가리키게 되어 조용히 깨진다. 그래서 log_id는 절대 재사용/재배정하지 않고, 기존 최대
log_id 이후에 이어서 추가한다(이 프로젝트의 "append-only, 기존 값은 안 건드림" 원칙과
동일 -- slack_app/ingest.py가 실제 수집 로그를 다루는 방식과 같은 패턴).

참고: 이 스크립트 실행 후 "실제 ingest 로그가 항상 최대 log_id"라는 이전 관례(§8-2)는
깨진다(심어둔 로그가 더 큰 id를 받으므로) -- 실제 로그 여부는 이제부터 log_id 크기가
아니라 `channel_id`/`ts` 컬럼이 비어있지 않은지로 판별한다(코드는 원래부터 이 방식만
썼다 -- ingest.py의 permalink 생성 로직 참고).

사용 예:
  python3 -m scripts.import_planted_coaching_logs
  python3 -m scripts.import_planted_coaching_logs --dry-run
"""

import argparse

from reports.data_access import DataStore, get_connection

# origin/main:seed/generate_mock_data.py 의 PLANTED_LOGS 상수를 그대로 옮김
# (member_id, date, text, linked_goal_id)
PLANTED_LOGS = [
    ("M03", "2026-06-10", "여름 가챠 이벤트 기획안 확정. 리워드 구성안을 파트장께 승인 요청함. 승인이 나와야 디자인·개발이 착수할 수 있어 우선 올림.", "G05"),
    ("M03", "2026-06-15", "가챠 이벤트 리워드 구성안 승인 회신을 아직 못 받음. 승인 대기로 후속 작업이 멈춰 있어 파트장께 리마인드했음.", "G05"),
    ("M03", "2026-06-18", "리워드 구성안 승인 재요청(두 번째 리마인드). 여전히 회신 대기 중이라 출시 일정이 밀릴 우려가 있음.", "G05"),
    ("M04", "2026-06-12", "가챠 이벤트용 아바타 아이템 러프 시안 다수 작업. 다만 리워드 구성안 승인 대기 중이라 어떤 안으로 확정할지 못 정해 클린업 착수 보류.", "G06"),
    ("M04", "2026-06-16", "리워드 구성 승인이 아직 안 나 아이템 방향을 확정하지 못함. 대기 지속, 타 UI 건 병행 중.", "G06"),
    ("M05", "2026-06-12", "가챠 뽑기 로직 설계 착수. 리워드 구성·확률 스펙이 승인 전이라 구현 착수가 불가해 스펙 확정을 대기 중.", "G07"),
    ("M05", "2026-06-16", "리워드 스펙 승인 대기로 뽑기 구현을 보류하고 결제 관련 타 태스크로 전환함.", "G07"),

    ("M02", "2026-05-04", "해외 결제대행사 라이선스 갱신 검토를 법무팀에 요청함. 검토 회신을 기다리는 중.", "G03"),
    ("M02", "2026-05-06", "해외 결제대행사 라이선스 갱신 검토 회신을 아직 못 받아 계약 절차가 확정되지 않음.", "G03"),
    ("M06", "2026-05-05", "해외 결제대행사 라이선스 갱신 검토 대기 중이라 이벤트 정산 방식 변경을 보류함.", "G08"),
    ("M06", "2026-05-07", "여전히 라이선스 갱신 검토 전이라 이벤트 정산 방식을 확정 못 하고 대기 중.", "G08"),

    ("M04", "2026-05-25", "모바일 SDK 업그레이드 승인을 요청해뒀는데 회신이 없어 착수를 못 하고 있음.", "G06"),
    ("M04", "2026-05-29", "SDK 업그레이드 승인 대기가 길어져 이번 주 작업도 보류 상태.", "G06"),
    ("M05", "2026-05-26", "SDK 업그레이드 승인이 안 나서 관련 테스트를 미루고 있음.", "G07"),
    ("M06", "2026-05-27", "SDK 업그레이드 승인 대기 중이라 이벤트 소재 반영 일정을 보류함.", "G08"),

    ("M03", "2026-06-22", "신규 이벤트 배너 문구 컨펌을 디자인리드에게 요청함. 컨펌 나야 다음 스텝 진행 가능.", "G05"),
    ("M03", "2026-06-24", "배너 문구 컨펌 대기 중, 아직 회신 없음.", "G05"),
    ("M04", "2026-06-23", "배너 문구 컨펌이 안 나서 최종 노출 버전을 못 정하고 있음.", "G06"),

    ("M05", "2026-05-11", "결제 QA 환경 접근 권한 요청함. 권한 없이는 결제 테스트 진행이 어려움.", "G07"),
    ("M05", "2026-05-18", "결제 QA 환경 접근 권한 관련 재요청. 아직 회신 없어 테스트 착수를 못 하고 있음.", "G07"),

    ("M06", "2026-05-15", "캠페인 예산 증액 요청함. 승인돼야 다음 단계 진행 가능.", "G08"),
    ("M06", "2026-05-23", "캠페인 예산 증액 재요청. 아직 회신 없어 다음 단계를 못 정하고 있음.", "G08"),

    ("M02", "2026-05-19", "QA에서 반려되어 결제 실패 안내 문구를 다시 수정함.", "G03"),
    ("M02", "2026-05-22", "리뷰 피드백으로 결제 실패 문구를 롤백 후 재작업 진행.", "G03"),
    ("M02", "2026-05-26", "세 번째로 같은 결제 실패 문구를 다시 작업 중, 요구사항이 계속 바뀜.", "G03"),

    ("M04", "2026-05-10", "디자인 검수에서 반려되어 아이템 아이콘을 다시 수정함.", "G06"),
    ("M04", "2026-05-13", "리뷰 피드백으로 아이콘 롤백 후 재작업 진행.", "G06"),
    ("M04", "2026-05-17", "세 번째로 같은 아이콘을 다시 작업 중, 요구사항이 계속 바뀜.", "G06"),

    ("M06", "2026-05-18", "캠페인 소재 3종 추가 제작 착수.", "G08"),
    ("M06", "2026-05-20", "캠페인 타겟 세그먼트 재정의 작업 진행.", "G08"),
    ("M06", "2026-05-21", "캠페인 발송 채널별 성과 비교 정리 중.", "G08"),
    ("M06", "2026-05-24", "이번 주도 캠페인 소재 준비 순조롭게 진행.", "G08"),
    ("M06", "2026-05-28", "캠페인 리마인드 문구 초안 3종 작성.", "G08"),
    ("M06", "2026-05-30", "캠페인 발송 대상 세그먼트 재검토 진행.", "G08"),

    ("M04", "2026-05-08", "바이럴 초대 로직 A/B 테스트 완료, 초대 성공률이 개선됨.", "G06"),
    ("M04", "2026-06-02", "초대 리워드 정산 자동화 스크립트 배포 완료로 처리 성공률이 개선됨.", "G06"),

    ("M02", "2026-06-20", "결제 실패 자동 재시도 로직 배포 완료, 결제 성공률이 개선됨.", "G03"),
    ("M02", "2026-06-27", "환불 프로세스 자동화 스크립트 배포 완료로 처리 시간이 단축됨.", "G03"),
]


def _next_log_id(max_existing_id):
    n = int(max_existing_id[1:])
    return f"L{n + 1:04d}"


def import_planted_logs(store, *, dry_run=False):
    existing_texts = {(l["member_id"], l["date"], l["text"]) for l in store.slack_logs}
    already_planted = [row for row in PLANTED_LOGS if (row[0], row[1], row[2]) in existing_texts]
    if already_planted:
        print(f"이미 반영된 것으로 보이는 로그 {len(already_planted)}건 발견 -- 중복 삽입을 피하려 건너뜁니다.")

    to_insert = [row for row in PLANTED_LOGS if (row[0], row[1], row[2]) not in existing_texts]
    if not to_insert:
        print("추가할 planted 로그가 없습니다 (이미 전부 반영됨).")
        return

    max_id = max(l["log_id"] for l in store.slack_logs)
    rows = []
    next_id = max_id
    for member_id, log_date, text, goal_id in to_insert:
        next_id = _next_log_id(next_id)
        rows.append((next_id, member_id, log_date, text, goal_id, "", ""))

    print(f"{len(rows)}건의 planted 로그를 {rows[0][0]} ~ {rows[-1][0]} 로 추가합니다:")
    for r in rows:
        print(f"  {r[0]} {r[1]} [{r[2]}] -> {r[4]} : {r[3][:50]}")

    if dry_run:
        print("(dry-run: DB에 쓰지 않음)")
        return

    conn = get_connection()
    try:
        with conn:
            conn.executemany(
                "INSERT INTO slack_logs (log_id, member_id, date, text, linked_goal_id, channel_id, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
    finally:
        conn.close()
    print("DB 반영 완료.")


def main():
    parser = argparse.ArgumentParser(description="main이 심어둔 코칭 카드 검증용 로그를 treerings.db에 추가")
    parser.add_argument("--dry-run", action="store_true", help="실제로 DB에 쓰지 않고 결과만 출력")
    args = parser.parse_args()

    store = DataStore()
    import_planted_logs(store, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
