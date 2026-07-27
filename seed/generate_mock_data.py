#!/usr/bin/env python3
"""
ENSAPIA Slack 기반 AI 목표-평가관리 시스템 - 목 데이터 생성 스크립트

재현성: SEED=42 고정. 이 스크립트를 다시 실행해도 완전히 동일한 CSV가 생성된다.
산술 정합성: DAU/매출/LTV 등 파생 지표는 절대 독립적으로 랜덤 생성하지 않고,
             기준 지표(MAU, 고착도, ARPU, 이탈률, K-factor)로부터 코드 내에서 계산한다.
             (data_dictionary.md 의 "지표 간 산술 관계식" 절 참고)
"""

import csv
import os
import random
from datetime import date, timedelta

SEED = 42
random.seed(SEED)
rng = random.Random(SEED)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
QUARTER = "2026-Q2"
QUARTER_START = date(2026, 4, 1)
QUARTER_END = date(2026, 6, 30)

# ---------------------------------------------------------------------------
# 1. members
# ---------------------------------------------------------------------------
MEMBERS = [
    # member_id, name,   team,      role
    ("M01", "정지원", "사업부",   "1차평가자"),
    ("M02", "한서준", "사업부",   "2차평가자"),
    ("M03", "김도윤", "사업부",   "팀원"),
    ("M04", "이서연", "사업부",   "팀원"),
    ("M05", "박준혁", "사업부",   "팀원"),
    ("M06", "최유나", "사업부",   "팀원"),
    ("M07", "오승민", "R&D",     "1차평가자"),
    ("M08", "강태양", "R&D",     "2차평가자"),
    ("M09", "윤하은", "R&D",     "팀원"),
    ("M10", "신동혁", "R&D",     "팀원"),
    ("M11", "배수아", "R&D",     "팀원"),
    ("M12", "임재현", "경영관리", "1차평가자"),
    ("M13", "조민경", "경영관리", "2차평가자"),
    ("M14", "문가영", "경영관리", "팀원"),
    ("M15", "송지훈", "경영관리", "팀원"),
]

# ---------------------------------------------------------------------------
# 2. kpis - 기준 지표만 상수로 정의하고, 파생 지표는 반드시 계산으로 도출한다.
#    벤치마크 근거:
#      - 고착도(DAU/MAU) 20~40% : 소셜 아바타 앱 카테고리 기준 (10~20% 평균, 20~50% 강한 참여)
#      - D7 리텐션 ~10% 전후 기준선
#      - LTV = 월 ARPU x 평균 생존기간(1/이탈률) x (1+바이럴계수)  → 수익화·리텐션·바이럴리티 3축 구조
# ---------------------------------------------------------------------------
MAU_CURRENT = 320_000
MAU_TARGET = 350_000

STICKINESS_CURRENT = 0.27  # DAU/MAU, 20~40% 밴드 내 (참여 양호~강한 참여 경계)
STICKINESS_TARGET = 0.32

DAU_CURRENT = round(MAU_CURRENT * STICKINESS_CURRENT)   # 역산: MAU x 고착도
DAU_TARGET = round(MAU_TARGET * STICKINESS_TARGET)

D7_RETENTION_CURRENT = 9.5   # %, ~10% 기준선 대비 약간 미달
D7_RETENTION_TARGET = 12.0

CHURN_CURRENT = 45.0  # %, 월간 이탈률 (낮을수록 좋음)
CHURN_TARGET = 38.0

ARPU_CURRENT = 1200  # 원, 월 블렌디드 ARPU (전체 가입자 기준)
ARPU_TARGET = 1500

REVENUE_CURRENT = MAU_CURRENT * ARPU_CURRENT   # 역산: MAU x ARPU
REVENUE_TARGET = MAU_TARGET * ARPU_TARGET

K_FACTOR_CURRENT = 0.15  # 바이럴 계수 (1명이 평균 유입시키는 추가 유저 가치 비율)
K_FACTOR_TARGET = 0.20

AVG_LIFETIME_CURRENT = 1 / (CHURN_CURRENT / 100)   # 평균 생존기간(개월) = 1/월 이탈률
AVG_LIFETIME_TARGET = 1 / (CHURN_TARGET / 100)

LTV_CURRENT = round(ARPU_CURRENT * AVG_LIFETIME_CURRENT * (1 + K_FACTOR_CURRENT))
LTV_TARGET = round(ARPU_TARGET * AVG_LIFETIME_TARGET * (1 + K_FACTOR_TARGET))

KPIS = [
    # kpi_id, name,                 unit, target,          current_value,     team
    ("K01", "MAU",                  "명", MAU_TARGET,        MAU_CURRENT,        "사업부"),
    ("K02", "Stickiness_DAU_MAU",   "%",  STICKINESS_TARGET*100, STICKINESS_CURRENT*100, "사업부"),
    ("K03", "DAU",                  "명", DAU_TARGET,        DAU_CURRENT,         "사업부"),
    ("K04", "D7_Retention",         "%",  D7_RETENTION_TARGET, D7_RETENTION_CURRENT, "사업부"),
    ("K05", "Monthly_Churn_Rate",   "%",  CHURN_TARGET,       CHURN_CURRENT,       "사업부"),
    ("K06", "ARPU_Monthly",         "원", ARPU_TARGET,        ARPU_CURRENT,        "사업부"),
    ("K07", "Monthly_Revenue",      "원", REVENUE_TARGET,     REVENUE_CURRENT,     "사업부"),
    ("K08", "K_Factor",             "배", K_FACTOR_TARGET,    K_FACTOR_CURRENT,    "사업부"),
    ("K09", "LTV",                  "원", LTV_TARGET,         LTV_CURRENT,         "사업부"),
]

# ---------------------------------------------------------------------------
# 3. goals  (정량 목표는 반드시 goal_kpi_link 로 KPI 연결, 정성 목표는 예외)
# ---------------------------------------------------------------------------
GOALS = [
    # goal_id, member_id, quarter, title, type, total_stages
    # total_stages: 이 목표를 완료하는 데 필요하다고 예상되는 "구획" 수 (1구획 = 누적 업무일지
    # 작성일 5영업일). strategy_weights.priority_score 와 마찬가지로 하드코딩된 진실이 아니라
    # 목표 생성 시 리더/본인이 입력하는 예시 파라미터 (일종의 스토리포인트 사이징).
    ("G01", "M01", QUARTER, "리브리 아일랜드 핵심 성장지표(MAU/DAU) 확대", "정량", 8),
    ("G02", "M01", QUARTER, "리텐션 개선을 통한 락인 강화", "정량", 6),
    ("G03", "M02", QUARTER, "매출 성장 및 ARPU 개선", "정량", 6),
    ("G04", "M02", QUARTER, "LTV 기반 유저 가치 극대화", "정량", 6),
    ("G05", "M03", QUARTER, "신규 유저 온보딩 개선 실험", "정량", 5),
    ("G06", "M04", QUARTER, "바이럴 초대 기능 강화", "정량", 5),
    ("G07", "M05", QUARTER, "결제 퍼널 최적화", "정량", 5),
    ("G08", "M06", QUARTER, "이탈 방지 캠페인", "정량", 5),
    ("G09", "M07", QUARTER, "AI 콘텐츠 QA 자동화 파이프라인 구축", "정성", 8),
    ("G10", "M07", QUARTER, "할루시네이션 감지 모델 PoC", "정성", 6),
    ("G11", "M08", QUARTER, "R&D 프로세스 표준화 및 실험 문화 정착", "정성", 5),
    ("G12", "M09", QUARTER, "아바타 렌더링 품질 자동 검수 도구 개발", "정성", 6),
    ("G13", "M10", QUARTER, "LLM 응답 안전성 가이드라인 수립", "정성", 5),
    ("G14", "M11", QUARTER, "QA 테스트케이스 커버리지 확대", "정성", 5),
    ("G15", "M12", QUARTER, "3개월 채용 파이프라인 정비", "정성", 6),
    ("G16", "M12", QUARTER, "조직 온보딩 프로세스 개선", "정성", 5),
    ("G17", "M13", QUARTER, "전사 운영 효율화 프로젝트", "정성", 6),
    ("G18", "M14", QUARTER, "복리후생 제도 개선안 수립", "정성", 5),
    ("G19", "M15", QUARTER, "사내 커뮤니케이션 툴 정비", "정성", 4),
]

# ---------------------------------------------------------------------------
# 4. goal_kpi_link (다대다 junction. weight 는 goal 내부에서 합=1.0 이 되도록 설계)
# ---------------------------------------------------------------------------
GOAL_KPI_LINKS = [
    # goal_id, kpi_id, weight, direction
    ("G01", "K01", 0.4, "+"),
    ("G01", "K03", 0.3, "+"),
    ("G01", "K02", 0.3, "+"),
    ("G02", "K04", 0.6, "+"),
    ("G02", "K05", 0.4, "-"),
    ("G03", "K06", 0.5, "+"),
    ("G03", "K07", 0.5, "+"),
    ("G04", "K09", 0.5, "+"),
    ("G04", "K08", 0.3, "+"),
    ("G04", "K05", 0.2, "-"),
    ("G05", "K04", 0.5, "+"),
    ("G05", "K03", 0.5, "+"),
    ("G06", "K08", 0.6, "+"),
    ("G06", "K01", 0.4, "+"),
    ("G07", "K06", 0.6, "+"),
    ("G07", "K07", 0.4, "+"),
    ("G08", "K05", 0.7, "-"),
    ("G08", "K04", 0.3, "+"),
]

# ---------------------------------------------------------------------------
# 5. strategy_weights - 운영자(인사팀/리더) 입력 파라미터.
#    실제 서비스에서는 하드코딩하지 않고 관리자 설정 화면에서 분기마다 입력받는다.
#    아래 값은 "이번 분기 리텐션/이탈 최우선 > 매출/ARPU/LTV > DAU/MAU/바이럴" 시나리오를
#    시연하기 위한 예시 입력값이다 (data_dictionary.md 참고).
# ---------------------------------------------------------------------------
STRATEGY_WEIGHTS = [
    # quarter, kpi_name, priority_score
    (QUARTER, "MAU", 1),
    (QUARTER, "Stickiness_DAU_MAU", 1),
    (QUARTER, "DAU", 1),
    (QUARTER, "D7_Retention", 3),
    (QUARTER, "Monthly_Churn_Rate", 3),
    (QUARTER, "ARPU_Monthly", 2),
    (QUARTER, "Monthly_Revenue", 2),
    (QUARTER, "K_Factor", 1),
    (QUARTER, "LTV", 2),
]

# ---------------------------------------------------------------------------
# 6. slack_logs - goal 별 3단계(가설/실행/결과) 내러티브 + 이슈 멘션을 섞어 생성
# ---------------------------------------------------------------------------
GOAL_NARRATIVES = {
    "G01": {
        "stage1": ["MAU/DAU 확대 가설 정리: 신규 유입 채널 다각화 + 첫 세션 리텐션 개선이 핵심이라고 보고 이번 주 데이터 분석 착수."],
        "stage2": ["신규 유입 채널 A/B 테스트 진행 중. 유입 채널별 7일 잔존율 비교 데이터 수집 중."],
        "stage3": [f"이번 주 기준 MAU {MAU_CURRENT:,}명, 고착도(DAU/MAU) {STICKINESS_CURRENT*100:.0f}% 확인. 목표 MAU {MAU_TARGET:,}명, 고착도 {STICKINESS_TARGET*100:.0f}% 대비 채널 다각화 효과 측정 중.",
                    f"DAU {DAU_CURRENT:,}명으로 전주 대비 소폭 상승. 목표 {DAU_TARGET:,}명까지 갭 축소 필요."],
        "issue": ["신규 유입 채널 예산 증액 승인이 지연되어 실험 규모 확대에 제약이 있음. 대표님 1on1에서 논의 필요."],
    },
    "G02": {
        "stage1": ["리텐션 락인 강화 가설: 첫 주 콘텐츠 추천 로직 개선 → D7 리텐션 상승, 이탈률 하락 기대. 현행 추천 로직 문제점 정리."],
        "stage2": ["개인화 추천 로직 1차 버전 배포, 일부 유저군 대상 실험 적용 중."],
        "stage3": [f"D7 리텐션 {D7_RETENTION_CURRENT}% → 목표 {D7_RETENTION_TARGET}%. 월 이탈률 {CHURN_CURRENT:.0f}% → 목표 {CHURN_TARGET:.0f}%. 실험군에서 이탈률 소폭 개선 신호 확인.",
                    "실험군 리텐션 곡선이 대조군 대비 완만하게 우상향. 다음 분기 전체 적용 검토 중."],
        "issue": ["추천 로직 서버 부하로 인해 일부 유저 응답 지연 이슈 발생, 인프라팀 협업 요청."],
    },
    "G03": {
        "stage1": ["ARPU/매출 개선 가설: 결제 유도 UX 개선 + 상품 구성 다변화가 ARPU 상승 견인할 것으로 보고 현황 데이터 정리."],
        "stage2": ["신규 상품 패키지 2종 출시, 결제 페이지 UX 개편 적용 중."],
        "stage3": [f"월 ARPU {ARPU_CURRENT:,}원 → 목표 {ARPU_TARGET:,}원. 월 매출 {REVENUE_CURRENT:,}원 → 목표 {REVENUE_TARGET:,}원. 신규 패키지 매출 기여분 집계 중.",
                    "결제 전환율이 개편 전 대비 개선되는 추세, 상품별 기여도 분석 중."],
        "issue": ["결제 모듈 PG사 정산 지연 이슈로 매출 집계에 일부 오차 발생, 재무팀 확인 요청."],
    },
    "G04": {
        "stage1": ["LTV 극대화 가설: ARPU x 평균 생존기간(1/이탈률) x (1+바이럴계수) 구조에서 이탈률 감소가 가장 레버리지가 크다고 판단, 우선순위 설정."],
        "stage2": ["이탈 방지 캠페인 및 초대 리워드 개편을 병행 실행 중."],
        "stage3": [f"LTV {LTV_CURRENT:,}원 → 목표 {LTV_TARGET:,}원. K-factor {K_FACTOR_CURRENT} → 목표 {K_FACTOR_TARGET}. 이탈률 개선분이 LTV 상승에 가장 크게 기여하는 것으로 분석됨."],
        "issue": ["LTV 산출 로직에 대해 재무팀과 정의 차이가 있어 지표 정합성 재검증 필요."],
    },
    "G05": {
        "stage1": ["온보딩 개선 가설: 첫 세션 튜토리얼 단순화 → D7 리텐션 및 DAU 개선 기대. 기존 온보딩 퍼널 이탈 구간 분석 착수."],
        "stage2": ["단순화된 튜토리얼 A/B 테스트 적용 중. 1주차 데이터 기준 튜토리얼 완료율 상승 확인."],
        "stage3": [f"실험군 D7 리텐션이 대조군 대비 개선 신호(전사 목표 {D7_RETENTION_TARGET}% 방향) 확인, DAU 기여분은 다음 주 추가 측정 예정.",
                    "튜토리얼 완료율 상승이 초기 리텐션 개선으로 이어지는 것을 확인, 전체 적용 여부 검토 중."],
        "issue": ["튜토리얼 단순화 버전에서 일부 iOS 기기 크래시 리포트 발생, 앱개발팀 긴급 확인 요청."],
    },
    "G06": {
        "stage1": ["바이럴 초대 기능 강화 가설: 초대 리워드 구조 개편이 K-factor 상승과 MAU 확대로 이어질 것으로 가정, 경쟁 서비스 벤치마킹."],
        "stage2": ["초대 리워드 개편안 적용, 초대 링크 공유 UX 개선 실험 진행 중."],
        "stage3": [f"K-factor {K_FACTOR_CURRENT} → 목표 {K_FACTOR_TARGET}. 초대를 통한 신규 유입이 MAU {MAU_CURRENT:,}명 중 일부 기여, 목표 MAU {MAU_TARGET:,}명까지 기여도 확대 필요."],
        "issue": ["초대 리워드 어뷰징(다중 계정) 우려 제기되어 안전장치 설계 필요, 어뷰징 방지팀 협업 요청."],
    },
    "G07": {
        "stage1": ["결제 퍼널 최적화 가설: 결제 단계 축소와 실패 사유 안내 개선이 ARPU/매출 상승에 기여할 것으로 판단, 현재 퍼널 이탈 구간 분석."],
        "stage2": ["결제 단계 3→2단계로 축소, 결제 실패 안내 문구 개선 적용."],
        "stage3": [f"결제 완료율 상승 추세, ARPU {ARPU_CURRENT:,}원 → 목표 {ARPU_TARGET:,}원 갭 축소 기여분 측정 중."],
        "issue": ["일부 간편결제 수단 연동 오류로 결제 실패 케이스 발생, 결제 PG사 문의 중."],
    },
    "G08": {
        "stage1": ["이탈 방지 캠페인 가설: 휴면 전환 임박 유저 대상 리마인드 푸시 + 리워드 제공이 이탈률 감소에 기여할 것으로 판단."],
        "stage2": ["휴면 임박군 세그먼트 정의 완료, 리마인드 푸시 캠페인 1차 발송."],
        "stage3": [f"월 이탈률 {CHURN_CURRENT:.0f}% → 목표 {CHURN_TARGET:.0f}%. 캠페인 대상군에서 재방문율 소폭 상승 확인, D7 리텐션 {D7_RETENTION_CURRENT}%와의 연관성 추가 분석 중."],
        "issue": ["푸시 발송 빈도에 대한 유저 불만(옵트아웃 증가) 접수, 발송 정책 조정 논의 필요."],
    },
    "G09": {
        "stage1": ["AI 콘텐츠 QA 자동화 파이프라인 요구사항 정의 및 기존 수동 QA 프로세스 병목 구간 분석."],
        "stage2": ["1차 자동화 스크립트(콘텐츠 정합성 체크) 개발 및 사내 테스트 데이터셋으로 검증 중."],
        "stage3": ["자동화 파이프라인 1차 버전으로 수동 QA 대비 검수 시간 단축 확인, 검출 정확도 보완 필요.",
                   "파이프라인을 실제 콘텐츠 릴리즈 프로세스에 시범 연동, QA팀 피드백 수집 중."],
        "issue": ["자동화 스크립트가 일부 엣지 케이스(다국어 콘텐츠)에서 오탐지 발생, 룰셋 보완 필요."],
    },
    "G10": {
        "stage1": ["할루시네이션 감지 모델 PoC 설계, 기존 리서치 논문 및 오픈소스 벤치마크 조사."],
        "stage2": ["감지 모델 프로토타입 학습 및 사내 QA 로그 기반 평가셋 구축 중."],
        "stage3": ["프로토타입 모델의 할루시네이션 탐지 정확도 1차 검증 완료, 오탐률 개선 방향 수립.",
                   "실제 서비스 응답 샘플에 적용해 본 결과 기반으로 모델 임계값 조정 중."],
        "issue": ["평가셋 라벨링 인력 부족으로 PoC 일정이 1주 지연될 가능성 있음, 리소스 지원 요청."],
    },
    "G11": {
        "stage1": ["R&D 프로세스 표준화를 위해 팀 내 실험 관리 방식 현황 조사 및 문제점 정리."],
        "stage2": ["실험 설계-실행-회고 템플릿 초안 작성, 팀원 대상 시범 적용."],
        "stage3": ["표준 템플릿 적용 후 실험 문서화 누락 사례 감소 확인, 팀 전체 확산 준비 중."],
        "issue": ["템플릿이 다소 무겁다는 피드백 있어 경량화 버전 검토 필요."],
    },
    "G12": {
        "stage1": ["아바타 렌더링 품질 자동 검수 도구 요구사항 정의, 기존 수동 검수 기준 정리."],
        "stage2": ["렌더링 이상(깨짐/텍스처 오류) 감지 알고리즘 1차 구현 및 샘플셋 테스트."],
        "stage3": ["자동 검수 도구가 기존 수동 검수 대비 주요 이상 케이스를 유사한 수준으로 탐지함을 확인.",
                   "일부 구형 iOS 기기에서 렌더링 중 앱이 강제 종료되는 크래시 원인을 찾아 메모리 캐싱 로직을 수정해 해결함. 이후 동일 기기군에서 크래시 리포트가 재발하지 않음을 확인."],
        "issue": ["특정 저사양 기기 렌더링 결과물에서 오탐이 잦아 기기별 캘리브레이션 필요."],
    },
    "G13": {
        "stage1": ["LLM 응답 안전성 가이드라인 초안을 위한 위험 케이스(혐오/개인정보 등) 분류 체계 수립."],
        "stage2": ["분류 체계 기반 필터링 룰 1차 적용 및 내부 레드팀 테스트 진행."],
        "stage3": ["레드팀 테스트에서 주요 위험 케이스 대부분 필터링됨을 확인, 가이드라인 문서화 진행 중."],
        "issue": ["신조어/은어 기반 우회 사례가 발견되어 필터링 룰 추가 보완 필요."],
    },
    "G14": {
        "stage1": ["QA 테스트케이스 커버리지 현황 분석, 미커버 영역(엣지 케이스 중심) 목록화."],
        "stage2": ["우선순위가 높은 미커버 영역부터 테스트케이스 추가 작성 중."],
        "stage3": ["테스트케이스 커버리지가 목록화 시점 대비 확대됨을 확인, 회귀 테스트 자동화 연계 검토 중."],
        "issue": ["테스트 환경 데이터 세팅 자동화가 안 되어 있어 케이스 작성 속도가 예상보다 느림."],
    },
    "G15": {
        "stage1": ["3개월 채용 파이프라인 정비를 위해 현재 채용 단계별 소요 기간과 병목 구간을 정리."],
        "stage2": ["채용 단계 축소(서류-실무면접-임원면접 3단계로 통합) 및 면접관 가이드 개편 적용."],
        "stage3": ["개편된 프로세스 기준 첫 채용 사이클 진행 중, 단계별 소요 기간 단축 여부 트래킹 중."],
        "issue": ["실무면접관 일정 조율이 어려워 일부 포지션에서 프로세스 지연 발생."],
    },
    "G16": {
        "stage1": ["조직 온보딩 프로세스 개선을 위해 최근 입사자 대상 온보딩 만족도 인터뷰 진행."],
        "stage2": ["온보딩 체크리스트 및 첫 2주 버디 프로그램 초안 마련, 시범 적용."],
        "stage3": ["시범 적용 대상 신규 입사자의 온보딩 만족도 피드백 긍정적, 전사 확대 검토 중."],
        "issue": ["버디 프로그램 참여를 위한 시니어 리소스 확보가 팀별로 불균등하다는 의견 있음."],
    },
    "G17": {
        "stage1": ["전사 운영 효율화를 위해 반복 업무(품의/정산/보고) 프로세스 현황 및 소요 시간 조사."],
        "stage2": ["반복 업무 중 일부를 템플릿/자동화 도구로 대체하는 시범 프로젝트 진행 중."],
        "stage3": ["시범 적용 부서에서 반복 업무 처리 시간이 단축됨을 확인, 전사 확산 로드맵 수립 중."],
        "issue": ["부서별 기존 업무 방식 차이가 커서 표준화 저항이 일부 존재, 변화관리 필요."],
    },
    "G18": {
        "stage1": ["복리후생 제도 개선을 위해 임직원 설문 및 타사 벤치마킹 데이터 수집."],
        "stage2": ["설문 결과 기반 우선순위 항목(리프레시 휴가, 건강검진 등) 개선안 초안 작성."],
        "stage3": ["개선안 초안에 대한 경영진 1차 검토 완료, 예산 협의 단계 진행 중."],
        "issue": ["일부 항목은 예산 제약으로 이번 분기 내 반영이 어려워 우선순위 재조정 필요."],
    },
    "G19": {
        "stage1": ["사내 커뮤니케이션 툴 정비를 위해 현재 채널/도구 사용 현황과 불편사항 조사."],
        "stage2": ["채널 네이밍/권한 체계 표준안 마련 및 일부 팀 대상 시범 적용."],
        "stage3": ["시범 적용 팀 기준 채널 검색성과 커뮤니케이션 혼선이 개선됨을 확인, 전사 적용 준비 중."],
        "issue": ["기존 채널 이관 과정에서 과거 이력 유실 우려가 제기되어 백업 절차 마련 필요."],
    },
}

# ---------------------------------------------------------------------------
# 6-b. 심어둔 정답(planted ground truth) — 코칭 카드 탐지 검증용
#      (프로젝트_개요 v10 §B-1 "가챠 이벤트 3직군 교차" 데모를 기존 사업부 멤버에 매핑)
#
#   ● 의존 병목: M03(기획 역할)·M04(디자인)·M05(개발) 세 사람이 같은 시기에
#     "가챠 이벤트 리워드 구성안"에 대한 파트장 M01의 승인을 기다린다.
#     → 개인별로는 각자 "대기 중"일 뿐이지만, 가로질러 읽으면 승인 하나가
#       3인분의 시간을 잡아먹는 구조 병목임이 드러난다 (리더만 해소 가능).
#   ● 미인지 성과: M03이 병목을 인지하고 두 차례 리마인드/조율했으나 어떤
#     산출물에도 남지 않는다 → 기획의 핵심 기여가 평가에서 사라지는 지점.
#
#   data_dictionary.md "심어둔 정답" 절에 정답 목록을 문서화한다(§8 탐지율 검증).
# ---------------------------------------------------------------------------
PLANTED_LOGS = [
    # member_id, date, text, linked_goal_id
    # --- M03 (기획 역할): 병목 유발 지점 + 미인지 성과(리마인드/조율) ---
    ("M03", "2026-06-10", "여름 가챠 이벤트 기획안 확정. 리워드 구성안을 파트장께 승인 요청함. 승인이 나와야 디자인·개발이 착수할 수 있어 우선 올림.", "G05"),
    ("M03", "2026-06-15", "가챠 이벤트 리워드 구성안 승인 회신을 아직 못 받음. 승인 대기로 후속 작업이 멈춰 있어 파트장께 리마인드했음.", "G05"),
    ("M03", "2026-06-18", "리워드 구성안 승인 재요청(두 번째 리마인드). 여전히 회신 대기 중이라 출시 일정이 밀릴 우려가 있음.", "G05"),
    # --- M04 (디자인 역할): 같은 승인 대기로 클린업 착수 불가 ---
    ("M04", "2026-06-12", "가챠 이벤트용 아바타 아이템 러프 시안 다수 작업. 다만 리워드 구성안 승인 대기 중이라 어떤 안으로 확정할지 못 정해 클린업 착수 보류.", "G06"),
    ("M04", "2026-06-16", "리워드 구성 승인이 아직 안 나 아이템 방향을 확정하지 못함. 대기 지속, 타 UI 건 병행 중.", "G06"),
    # --- M05 (개발 역할): 같은 승인 대기로 구현 착수 불가 ---
    ("M05", "2026-06-12", "가챠 뽑기 로직 설계 착수. 리워드 구성·확률 스펙이 승인 전이라 구현 착수가 불가해 스펙 확정을 대기 중.", "G07"),
    ("M05", "2026-06-16", "리워드 스펙 승인 대기로 뽑기 구현을 보류하고 결제 관련 타 태스크로 전환함.", "G07"),

    # -----------------------------------------------------------------------
    # 6-c. 확장 심어둔 정답 (2026-07-27 추가) — data_dictionary.md 8-1절이
    # "아직 심지 않음"으로 남겨둔 4종(부하 편중/재작업 반복/미해결 요청 x2셋,
    # + high 확신도 카드 예시가 없다는 실사용 피드백)을 채운다. 위 가챠 병목과
    # 겹치지 않는 별도 대상/기간/인물을 써서 서로 무관하게 남긴다.
    # -----------------------------------------------------------------------

    # ● 의존 병목(2인, high 확신도, 짧은 지속) — affected=2라 3인 임계값을 안 넘어
    #   urgent로 안 빠지고 "코칭 카드"로 남는 유일한 실제 사례. M02/M06, PG사 계약 검토.
    ("M02", "2026-05-04", "해외 결제대행사 라이선스 갱신 검토를 법무팀에 요청함. 검토 회신을 기다리는 중.", "G03"),
    ("M02", "2026-05-06", "해외 결제대행사 라이선스 갱신 검토 회신을 아직 못 받아 계약 절차가 확정되지 않음.", "G03"),
    ("M06", "2026-05-05", "해외 결제대행사 라이선스 갱신 검토 대기 중이라 이벤트 정산 방식 변경을 보류함.", "G08"),
    ("M06", "2026-05-07", "여전히 라이선스 갱신 검토 전이라 이벤트 정산 방식을 확정 못 하고 대기 중.", "G08"),

    # ● 의존 병목(3인, 짧은 지속·5일↑ → 긴급 경로) — 가챠 건과는 다른 시기/대상이라
    #   긴급 분리가 특정 데이터 하나에만 우연히 맞는 게 아님을 다시 확인하는 용도.
    ("M04", "2026-05-25", "모바일 SDK 업그레이드 승인을 요청해뒀는데 회신이 없어 착수를 못 하고 있음.", "G06"),
    ("M04", "2026-05-29", "SDK 업그레이드 승인 대기가 길어져 이번 주 작업도 보류 상태.", "G06"),
    ("M05", "2026-05-26", "SDK 업그레이드 승인이 안 나서 관련 테스트를 미루고 있음.", "G07"),
    ("M06", "2026-05-27", "SDK 업그레이드 승인 대기 중이라 이벤트 소재 반영 일정을 보류함.", "G08"),

    # ● 의존 병목(2인, 3일 지속 — duration_factor 하한 근처 사례) — M03/M04, 이벤트 배너 컨펌.
    ("M03", "2026-06-22", "신규 이벤트 배너 문구 컨펌을 디자인리드에게 요청함. 컨펌 나야 다음 스텝 진행 가능.", "G05"),
    ("M03", "2026-06-24", "배너 문구 컨펌 대기 중, 아직 회신 없음.", "G05"),
    ("M04", "2026-06-23", "배너 문구 컨펌이 안 나서 최종 노출 버전을 못 정하고 있음.", "G06"),

    # ● 미해결 요청 — M05, QA 환경 접근 권한 재요청(같은 사람의 별개 요청 2건이 병합돼야
    #   근거 2건 요건을 충족하므로 반드시 2건 이상 심는다).
    ("M05", "2026-05-11", "결제 QA 환경 접근 권한 요청함. 권한 없이는 결제 테스트 진행이 어려움.", "G07"),
    ("M05", "2026-05-18", "결제 QA 환경 접근 권한 관련 재요청. 아직 회신 없어 테스트 착수를 못 하고 있음.", "G07"),

    # ● 미해결 요청 — M06, 캠페인 예산 증액 재요청.
    ("M06", "2026-05-15", "캠페인 예산 증액 요청함. 승인돼야 다음 단계 진행 가능.", "G08"),
    ("M06", "2026-05-23", "캠페인 예산 증액 재요청. 아직 회신 없어 다음 단계를 못 정하고 있음.", "G08"),

    # ● 재작업 반복 — M02, 결제 실패 안내 문구 3회 재작업.
    ("M02", "2026-05-19", "QA에서 반려되어 결제 실패 안내 문구를 다시 수정함.", "G03"),
    ("M02", "2026-05-22", "리뷰 피드백으로 결제 실패 문구를 롤백 후 재작업 진행.", "G03"),
    ("M02", "2026-05-26", "세 번째로 같은 결제 실패 문구를 다시 작업 중, 요구사항이 계속 바뀜.", "G03"),

    # ● 재작업 반복 — M04, 아이템 아이콘 3회 재작업.
    ("M04", "2026-05-10", "디자인 검수에서 반려되어 아이템 아이콘을 다시 수정함.", "G06"),
    ("M04", "2026-05-13", "리뷰 피드백으로 아이콘 롤백 후 재작업 진행.", "G06"),
    ("M04", "2026-05-17", "세 번째로 같은 아이콘을 다시 작업 중, 요구사항이 계속 바뀜.", "G06"),

    # ● 부하 편중 — M06, 좁은 기간(05-18~05-31)에 짧은 로그를 몰아넣어 팀 평균 대비
    #   과다하게 만든다(로직: 팀 평균×1.6 그리고 평균+3 모두 충족해야 함).
    ("M06", "2026-05-18", "캠페인 소재 3종 추가 제작 착수.", "G08"),
    ("M06", "2026-05-20", "캠페인 타겟 세그먼트 재정의 작업 진행.", "G08"),
    ("M06", "2026-05-21", "캠페인 발송 채널별 성과 비교 정리 중.", "G08"),
    ("M06", "2026-05-24", "이번 주도 캠페인 소재 준비 순조롭게 진행.", "G08"),
    ("M06", "2026-05-28", "캠페인 리마인드 문구 초안 3종 작성.", "G08"),
    ("M06", "2026-05-30", "캠페인 발송 대상 세그먼트 재검토 진행.", "G08"),

    # ● 미인지 성과 — M04, 서로 다른 성과 문장 2건(리스크 표현 없음).
    ("M04", "2026-05-08", "바이럴 초대 로직 A/B 테스트 완료, 초대 성공률이 개선됨.", "G06"),
    ("M04", "2026-06-02", "초대 리워드 정산 자동화 스크립트 배포 완료로 처리 성공률이 개선됨.", "G06"),

    # ● 미인지 성과 — M02, 서로 다른 성과 문장 2건.
    ("M02", "2026-06-20", "결제 실패 자동 재시도 로직 배포 완료, 결제 성공률이 개선됨.", "G03"),
    ("M02", "2026-06-27", "환불 프로세스 자동화 스크립트 배포 완료로 처리 시간이 단축됨.", "G03"),
]


WEEKDAY_CHOICES = [0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 5]  # 평일 위주, 가끔 주말


def stage_for_week(week_idx: int) -> str:
    if week_idx < 4:
        return "stage1"
    if week_idx < 9:
        return "stage2"
    return "stage3"


def build_slack_logs():
    goals_by_member = {}
    for goal_id, member_id, _, _, _, _ in GOALS:
        goals_by_member.setdefault(member_id, []).append(goal_id)

    logs = []
    log_seq = 1
    total_days = (QUARTER_END - QUARTER_START).days
    n_weeks = total_days // 7 + 1

    for member_id, *_ in MEMBERS:
        member_goals = goals_by_member[member_id]
        for week_idx in range(n_weeks):
            week_start = QUARTER_START + timedelta(days=7 * week_idx)
            if week_start > QUARTER_END:
                break
            stage = stage_for_week(week_idx)
            n_logs_this_week = rng.choice([1, 1, 2])  # 대부분 1개, 가끔 2개
            for _ in range(n_logs_this_week):
                offset = rng.choice(WEEKDAY_CHOICES)
                log_date = min(week_start + timedelta(days=offset), QUARTER_END)
                goal_id = rng.choice(member_goals)
                narrative = GOAL_NARRATIVES[goal_id]
                text = rng.choice(narrative[stage])
                if rng.random() < 0.22:
                    text = text + " " + rng.choice(narrative["issue"])
                # channel_id/ts 는 목데이터이므로 빈 값 -- 실제 Slack 메시지가 아니라
                # permalink 를 만들 근거(원본 채널/타임스탬프)가 없다. ingest.py 로 실제
                # 수집된 로그만 이 값이 채워진다.
                logs.append((f"L{log_seq:04d}", member_id, log_date.isoformat(), text, goal_id, "", ""))
                log_seq += 1
    # 심어둔 정답 로그를 함께 넣는다(날짜순 정렬 대상에 포함되어 자연스럽게 섞인다)
    for member_id, log_date, text, goal_id in PLANTED_LOGS:
        logs.append((f"L{log_seq:04d}", member_id, log_date, text, goal_id, "", ""))
        log_seq += 1
    logs.sort(key=lambda r: (r[2], r[1]))
    # log_id 를 날짜순으로 재부여해 시간 흐름을 그대로 반영
    renumbered = []
    for i, row in enumerate(logs, start=1):
        renumbered.append((f"L{i:04d}",) + row[1:])
    return renumbered


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    write_csv(os.path.join(DATA_DIR, "members.csv"),
              ["member_id", "name", "team", "role"], MEMBERS)

    write_csv(os.path.join(DATA_DIR, "goals.csv"),
              ["goal_id", "member_id", "quarter", "title", "type", "total_stages"], GOALS)

    write_csv(os.path.join(DATA_DIR, "kpis.csv"),
              ["kpi_id", "name", "unit", "target", "current_value", "team"], KPIS)

    write_csv(os.path.join(DATA_DIR, "goal_kpi_link.csv"),
              ["goal_id", "kpi_id", "weight", "direction"], GOAL_KPI_LINKS)

    write_csv(os.path.join(DATA_DIR, "strategy_weights.csv"),
              ["quarter", "kpi_name", "priority_score"], STRATEGY_WEIGHTS)

    # 실제 수집된 로그(ingest.py로 들어온 것, channel_id/ts 보유)는 목데이터를
    # 재생성할 때도 보존한다 -- 가짜 링크를 만들지 않는 원칙과 대칭으로, 실제
    # permalink 근거가 있는 로그를 재생성으로 지우지 않기 위함.
    slack_path = os.path.join(DATA_DIR, "slack_logs.csv")
    preserved = []
    if os.path.exists(slack_path):
        with open(slack_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("channel_id", "").strip() or r.get("ts", "").strip():
                    preserved.append(r)

    slack_logs = build_slack_logs()
    # 보존된 실제 로그는 목데이터 뒤에 이어 붙이고 id를 연속 부여한다
    # (ingest.py가 항상 최대 id + 1 로 append 하는 규약과 일치).
    start = len(slack_logs) + 1
    for i, r in enumerate(preserved):
        slack_logs.append((f"L{start + i:04d}", r["member_id"], r["date"],
                           r["text"], r["linked_goal_id"], r["channel_id"], r["ts"]))

    write_csv(slack_path,
              ["log_id", "member_id", "date", "text", "linked_goal_id", "channel_id", "ts"], slack_logs)

    print(f"members: {len(MEMBERS)} rows")
    print(f"goals: {len(GOALS)} rows")
    print(f"kpis: {len(KPIS)} rows")
    print(f"goal_kpi_link: {len(GOAL_KPI_LINKS)} rows")
    print(f"strategy_weights: {len(STRATEGY_WEIGHTS)} rows")
    print(f"slack_logs: {len(slack_logs)} rows "
          f"(목데이터 {len(slack_logs) - len(preserved)} + 심어둔정답 {len(PLANTED_LOGS)} 포함, "
          f"실제수집 보존 {len(preserved)})")


if __name__ == "__main__":
    main()
