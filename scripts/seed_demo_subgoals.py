#!/usr/bin/env python3
"""
'하위 목표(건물)' 데이터 시딩 -- 계량기(gauge)가 아니라 계수기(counter) 원칙 적용.

레몬베이스(외부 OKR 툴) API 연동이 없어서, 그 툴에 원래 있을 법한 goal별 하위 목표 breakdown을
mock으로 채워넣는다. 정성 목표 마일스톤(goal_milestones, 가설수립/실행/결과측정 3단계 고정)과는
완전히 별개의 개념/테이블이다 -- 그건 HR 평가 근거용이고, 이건 홈탭 "진행 중인 목표" 표시용이다.

핵심 원칙 (재설계 배경): 처음엔 각 하위목표의 "누적 일수"를 손으로 예시 숫자로 채워넣었는데,
이건 실제로 존재하지 않는 값을 지어낸 것이라 이 프로젝트 전체 원칙("AI는 진척률을 추정하지
않는다", 비율은 분모를 요구하고 그 분모는 곧 추정이 된다)에 어긋난다는 지적을 받아 재설계했다.

이제는 모든 건물이 예외 없이 고정 5단계(reports/progress.py의 SUBGOAL_TOTAL_STAGES) x 5영업일
(SUBGOAL_DAYS_PER_STAGE) = 25영업일 구조이고, 그 진행 단계는 그 하위목표에 실제로 연결된
slack_logs 의 distinct 날짜 수를 세어서(subgoal_stage()) 결정론적으로 계산한다 -- 지어낸 숫자가
전혀 없다. 이를 위해 goal에 이미 존재하는 실제 로그를 하위목표별로 순서대로 나눠 배정한다
(subgoal_evidence 테이블, goal_kpi_link/milestone_evidence와 동일한 다대다 근거 연결 패턴).

실측 확인 (2026-07-27): 현재 mock 데이터는 goal 하나 전체를 통틀어도 최대 20일(G19)이라, 하위목표
단위로 쪼개면 25일(5단계 완주)에 도달하는 건물이 하나도 없다 -- 그래서 '본인완료'/'확정완료'는 지금
데이터로는 하나도 안 나온다(정직하게: 분기 중반이라 아직 아무 하위목표도 다 안 지어진 상태가 오히려
현실적이라고 판단해 로그를 인위적으로 늘리지 않기로 함). '본인완료'/'확정완료'는 실제
report_subgoal.py/confirm_subgoal.py 를 통해 나중에 실제로 25일이 쌓였을 때만 나오게 된다.

배분 방식은 균등 분할에서 그리디(앞 하위목표부터 5일씩 채우고 다음으로 넘어감) 방식으로 바꿨다
(_split_greedy) -- 로그를 지어내지 않으면서(여전히 실제 존재하는 로그만 사용), "여러 개를 동시에
조금씩 건드린다"보다 "하나씩 순서대로 채운다"는 더 현실적인 배분이라 최소 일부 하위목표는 1단계
이상 진행 표시가 뜬다.

사용 예:
  python3 -m scripts.seed_demo_subgoals
"""

from reports.data_access import get_connection
from reports.progress import SUBGOAL_DAYS_PER_STAGE

SCHEMA = """
DROP TABLE IF EXISTS subgoal_evidence;
DROP TABLE IF EXISTS sub_goals;

CREATE TABLE sub_goals (
    sub_goal_id       TEXT PRIMARY KEY,
    goal_id           TEXT NOT NULL REFERENCES goals(goal_id),
    title             TEXT NOT NULL,
    order_index       INTEGER NOT NULL,
    status            TEXT CHECK (status IN ('본인완료','확정완료')),  -- NULL = 미착수/공사중(계산값으로 판단)
    self_reported_at  TEXT,
    confirmed_at      TEXT,
    confirmed_by      TEXT REFERENCES members(member_id),
    UNIQUE (goal_id, order_index)
);

CREATE TABLE subgoal_evidence (
    sub_goal_id TEXT NOT NULL REFERENCES sub_goals(sub_goal_id),
    log_id      TEXT NOT NULL REFERENCES slack_logs(log_id),
    PRIMARY KEY (sub_goal_id, log_id)
);
CREATE INDEX idx_subgoal_evidence_log ON subgoal_evidence(log_id);

CREATE TABLE IF NOT EXISTS home_view_state (
    member_id TEXT PRIMARY KEY REFERENCES members(member_id),
    last_seen_personal_weekly_generated_at TEXT
);
"""

# goal_id -> 하위목표 이름 목록 (order_index는 리스트 순서). 상태/누적일수는 더 이상 손으로 안 넣고
# subgoal_stage()가 실제 연결된 로그를 세어서 계산한다.
DEMO_SUBGOAL_TITLES = {
    "G01": ["신규 유입 채널 후보 조사", "채널별 A/B 테스트 설계 및 실행", "첫 세션 리텐션 개선안 적용", "채널 다각화 효과 측정 리포트"],
    "G02": ["추천 로직 현황 분석", "개인화 추천 로직 1차 버전 배포", "실험군/대조군 비교 분석"],
    "G03": ["결제 UX 현황 진단", "신규 상품 패키지 2종 출시", "결제 페이지 개편 적용", "매출 기여도 분석"],
    "G04": ["LTV 구조 분해 분석", "이탈 방지 캠페인 설계", "초대 리워드 개편"],
    "G05": ["온보딩 퍼널 이탈 구간 분석", "튜토리얼 단순화 A/B 테스트", "iOS 크래시 이슈 수정", "전체 적용 여부 결정"],
    "G06": ["경쟁 서비스 벤치마킹", "초대 리워드 구조 개편", "어뷰징 방지 장치 설계"],
    "G07": ["결제 단계 이탈 구간 분석", "결제 단계 3→2단계 축소", "간편결제 연동 오류 수정"],
    "G08": ["휴면 임박군 세그먼트 정의", "리마인드 푸시 캠페인 발송", "발송 정책 조정"],
    "G09": ["요구사항 정의", "1차 자동화 스크립트 개발", "실제 릴리즈 프로세스 시범 연동", "검출 정확도 검증"],
    "G10": ["리서치 논문/벤치마크 조사", "감지 모델 프로토타입 학습", "모델 임계값 조정", "오탐률 개선 방향 정리"],
    "G11": ["팀 내 실험 관리 현황 조사", "실험 템플릿 초안 작성", "경량화 버전 검토"],
    "G12": ["렌더링 이상 감지 알고리즘 구현", "샘플셋 테스트 및 캘리브레이션", "전사 배포 가이드 작성"],
    "G13": ["위험 케이스 분류 체계 수립", "필터링 룰 1차 적용", "신조어/은어 우회 사례 보완"],
    "G14": ["미커버 영역 목록화", "우선순위 테스트케이스 작성", "테스트 환경 자동화"],
    "G15": ["채용 단계별 소요기간 분석", "채용 단계 3단계로 통합", "면접관 가이드 개편", "첫 채용 사이클 트래킹"],
    "G16": ["신규 입사자 만족도 인터뷰", "온보딩 체크리스트 마련", "버디 프로그램 시범 적용"],
    "G17": ["반복 업무 현황 조사", "자동화 도구 시범 적용", "전사 확산 로드맵 수립"],
    "G18": ["임직원 설문 및 벤치마킹", "우선순위 항목 개선안 작성", "경영진 검토 및 예산 협의"],
    "G19": ["채널/도구 사용 현황 조사", "채널 네이밍/권한 체계 표준안", "채널 이관 백업 절차 마련"],
}


def _split_greedy(items, n):
    """
    items(시간순)를 n개 구간으로 나누되, 고르게 쪼개지 않고 앞에서부터 SUBGOAL_DAYS_PER_STAGE(5)개씩
    채운 뒤 다음 구간으로 넘어간다(마지막 구간만 남은 걸 전부 가짐) -- "여러 하위목표를 동시에 조금씩
    건드린다"보다 "하나를 어느 정도 채우고 다음으로 넘어간다"는 실제 작업 패턴에 더 가깝고, 그래야
    적은 로그로도 최소 일부 하위목표는 진행 표시가 뜬다. 로그를 새로 지어내는 게 아니라 기존 로그의
    "배분 방식"만 바꾸는 것이라 계수기 원칙(실제 로그만 센다)은 그대로 유지된다.
    """
    if n <= 0:
        return []
    chunks, remaining = [], list(items)
    for i in range(n):
        take = len(remaining) if i == n - 1 else min(SUBGOAL_DAYS_PER_STAGE, len(remaining))
        chunks.append(remaining[:take])
        remaining = remaining[take:]
    return chunks


def seed():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)

        for goal_id, titles in DEMO_SUBGOAL_TITLES.items():
            logs = conn.execute(
                "SELECT log_id, date FROM slack_logs WHERE linked_goal_id = ? ORDER BY date", (goal_id,)
            ).fetchall()
            chunks = _split_greedy(logs, len(titles))

            for order_index, (title, chunk) in enumerate(zip(titles, chunks), start=1):
                sub_goal_id = f"{goal_id}-SG{order_index}"
                conn.execute(
                    "INSERT INTO sub_goals (sub_goal_id, goal_id, title, order_index) VALUES (?, ?, ?, ?)",
                    (sub_goal_id, goal_id, title, order_index),
                )
                conn.executemany(
                    "INSERT INTO subgoal_evidence (sub_goal_id, log_id) VALUES (?, ?)",
                    [(sub_goal_id, row["log_id"]) for row in chunk],
                )

            distinct_days = [len({row["date"] for row in chunk}) for chunk in chunks]
            print(f"  [seeded] {goal_id}: 하위목표 {len(titles)}개, 배정된 로그 일수 {distinct_days}")

        conn.commit()
    finally:
        conn.close()

    conn = get_connection()
    sub_count = conn.execute("SELECT COUNT(*) FROM sub_goals").fetchone()[0]
    ev_count = conn.execute("SELECT COUNT(*) FROM subgoal_evidence").fetchone()[0]
    conn.close()
    print(f"sub_goals: {sub_count} rows, subgoal_evidence: {ev_count} rows")


if __name__ == "__main__":
    seed()
