# ENSAPIA 나이테 — Slack 업무일지 기반 AI 목표·성과관리 시스템

매일 쓰는 Slack 업무일지를 그대로 두고, AI가 여러 사람의 기록을 교차 대조해 **개인 주간 리포트 /
리더 코칭 카드 / 분기 평가 근거 패키지 / 협업 연결**을 자동으로 만들어내는 시스템. 구성원이 새로
작성하는 문서는 없다 — 쓰는 곳만 개인 전용 Slack 채널로 바뀔 뿐이다.

설계 원칙: **AI는 1차 검토자다.** 분류·대조·계수·추출은 AI/코드가 하고, 판단과 확정(완료 처리,
달성도, 코칭 카드 채택/기각)은 항상 사람이 한다. 자세한 배경과 설계 근거는 `data/data_dictionary.md`
(데이터 명세 + 설계 결정 기록)를 참고.

## 핵심 산출물

| 산출물 | 주기 | 구현 위치 |
|---|---|---|
| 개인 주간 리포트 | 주 1회 | `reports/generate_reports.py::run_personal` |
| 리더 코칭 카드 (+긴급 알림) | 격주 | `coaching/`(LangGraph 5종 탐지기) + `slack_app/send_team_coaching.py` |
| 분기 평가 근거 패키지 (PDF) | 분기 | `reports/generate_reports.py::run_evidence` + `reports/evidence_pdf.py` |
| 협업 연결 (`/지원요청`) | 상시 | `slack_app/wish_match.py` + `slack_app/socket_app.py` |
| 홈 탭 (오늘의 현황 · 도시 시각화) | 상시 | `slack_app/home_view.py` |

세 산출물(개인 리포트/코칭 카드/평가 근거)은 전부 **인용 검증을 코드가 강제**한다 — LLM이 인용한
`log_id`가 실제로 존재하고 원문과 일치하는지 코드가 대조하며, 근거 없는 문장은 폐기된다.

## 아키텍처

```
일일 업무일지 (Slack 개인 채널)
  │  AI: 분류 · 대조 · 계수 · 추출
  ↓
개인 주간 리포트  ←── 본인 확인 (정정 / 완료 신고 / 상태 선택)
  │
  ├──→ 코칭 카드 (격주)         입력: 2주치 원문 + 확인 결과
  └──→ 평가 근거 패키지 (분기)   입력: 12주치 확정 리포트 + 원문 인용

협업 연결(/지원요청)만 이 계층 밖에 있다 — 주기가 없고 본인이 필요할 때 등록한다.
```

- **저장소**: SQLite(`data/treerings.db`) — 원본 데이터(멤버/목표/KPI/업무일지)와 `report_cache`
  (LLM 생성 결과 캐시)를 함께 담는다.
- **LLM**: `litellm` 기반, `LLM_PROVIDER`로 프로바이더 전환(기본 Upstage Solar Pro, `gemini`로
  전환 가능). 코칭 카드 파이프라인은 `--no-llm`으로 LLM 없이도 100% 동작(코드 템플릿 서술).
- **코칭 카드**는 별도 엔진(`coaching/`, LangGraph): 탐지·근거검증·점수·선별은 전부 코드, LLM은
  마지막에 서술만 담당. 자세한 설계는 `coaching/README.md` 참고.
- **Slack 연동**은 Socket Mode(`slack_app/socket_app.py`) — 공인 HTTPS 엔드포인트 없이 슬래시
  커맨드/버튼 인터랙션을 받는다.

## 디렉토리 구조

```
reports/       개인 리포트 · 평가 근거 패키지 · 우선순위 계산 · LLM 클라이언트 · 회귀 테스트
coaching/      코칭 카드 엔진 (LangGraph, 5종 탐지기) — coaching/README.md 참고
slack_app/     Slack 봇 (Socket Mode 리스너, 홈탭, 발송 스크립트, 협업 연결)
data/          SQLite DB, 데이터 명세서(data_dictionary.md), Slack 매핑/캐시 JSON
seed/          목데이터 생성 스크립트 (SEED=42 고정, 재현 가능)
scripts/       1회성 마이그레이션/시드 스크립트
docs/          내부 검토 문서
assets/        홈탭 도시맵 등 이미지 리소스
```

## 시작하기

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt

cp .env.example .env   # SLACK_BOT_TOKEN, UPSTAGE_API_KEY(또는 GEMINI_API_KEY) 채우기
```

목데이터가 필요하면(최초 1회):
```bash
python -m seed.generate_mock_data
python -m scripts.migrate_csv_to_sqlite
```

Slack 봇 실행(Socket Mode — `/평가근거`, `/지원요청` 등 슬래시 커맨드·버튼 처리):
```bash
python -m slack_app.socket_app
```
Slack 앱 설정에서 `SLACK_APP_TOKEN`(Socket Mode, `connections:write`)과 Slash Commands
(`/평가근거`, `/지원요청` — Request URL은 아무 값이나 가능)를 등록해야 한다.

## 주요 명령어

```bash
# 리포트 생성 (캐시에 저장, --force로 강제 재생성)
python -m reports.generate_reports --report {personal,coaching,evidence,all} --members M01,M02

# Slack 발송
python -m slack_app.send_weekly_dm --member M01
python -m slack_app.send_evidence_package --member M01
python -m slack_app.send_team_coaching --team 사업부 --start 2026-05-13 --end 2026-05-26
python -m slack_app.publish_home --member M01

# 협업 연결 — 일지 기반 자동 스캔 + 답장 확인 (DM 기반)
python -m slack_app.wish_match --scan
python -m slack_app.wish_match --check-replies

# 회귀 테스트
python -m reports.selftest
python -m coaching.selftest
python -m slack_app.wish_selftest
```

## 참고 문서

- `data/data_dictionary.md` — 스키마 정의 + 모든 설계 결정의 근거/트레이드오프 기록
- `coaching/README.md` — 코칭 카드 엔진 상세 (파이프라인, 점수식, 알려진 한계)
- `.env.example` — 필요한 환경 변수 전체 목록과 설명
