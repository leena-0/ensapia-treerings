# 코드 리뷰 — ensapia-treerings

> ENSAPIA: Slack 업무일지 기반 AI 목표·평가관리 시스템
> 본 문서는 repo의 각 코드 파일이 어떤 기능을 하는지 정리한 리뷰 노트다. (작성: 코드 정적 분석 기반)

---

## 1. 전체 구조 한눈에

```
seed/generate_mock_data.py     ← 목(mock) 데이터 생성
        │  (CSV 6종 출력)
        ▼
data/*.csv, data/*.json        ← 원천 데이터 + 캐시 저장소
        │
        ▼
reports/data_access.py         ← CSV 로드 + 인덱싱 (DataStore)
        │
        ├─▶ reports/priority.py   ← 우선순위 "숫자" 계산 (결정론적, LLM 미개입)
        ├─▶ reports/coaching_signals.py ← 코칭 신호(의존 병목) 탐지·선별 (결정론적)
        ├─▶ reports/progress.py   ← 목표 진행 "구획" 계산 (결정론적, LLM 미개입)
        ├─▶ reports/prompts.py    ← LLM 프롬프트 빌더 (서술만 요청)
        └─▶ reports/llm_client.py ← Gemini 호출 (+로컬 폴백, rate limit 보호)
                 │
                 ▼
reports/generate_reports.py    ← 오케스트레이터 (CLI). 리포트 3종 생성
                 │  (LLM 결과를 캐시에 저장)
                 ▼
reports/cache_store.py         ← LLM 응답 JSON 캐싱 (input_hash로 무효화)
                 │
                 ▼
slack_app/*                    ← 캐시를 읽어 Slack으로 발행/발송
   ├─ home_view.py             ← Block Kit 뷰 빌더
   ├─ publish_home.py          ← 홈 탭 대시보드 발행
   ├─ send_weekly_dm.py        ← 개인 주간 리포트 DM
   ├─ send_coaching_dm.py      ← 팀 코칭 카드 DM (리더 대상)
   ├─ send_evidence_package.py ← 평가 근거 PDF DM 첨부
   ├─ ingest.py                ← Slack 메시지 → slack_logs.csv 수집
   ├─ wish_match.py            ← "소원 매칭" (막힌 동료 ↔ 해결 경험자 연결)
   └─ member_map.py            ← Slack user_id ↔ member_id 매핑
        │
        ▼
      Slack (Web API)
```

**핵심 설계 원칙 (코드 전반에서 일관):**
- **숫자·순서·인용 검증은 코드가 확정**한다. LLM은 서술(정리/대조/요약)만 담당하고, 우선순위 점수·진행 구획·log_id 실존 여부는 결정론적 코드가 계산·검증한다.
- **캐시 우선.** LLM 호출은 무료 티어 rate limit 보호를 위해 최소 간격·재시도·JSON 캐싱으로 감싼다.
- **가짜 근거를 만들지 않는다.** permalink가 없는 목데이터는 링크를 생성하지 않고, 인용 log_id는 실제 존재하는 것만 남긴다.

---

## 2. 루트 / 설정 파일

### `env_loader.py`
- **역할**: 코드에 시크릿을 하드코딩하지 않기 위한 프로젝트 공용 `.env` 로더 (유일한 환경변수 진입점).
- **주요 함수/클래스**:
  - `_load_env_file(path)` — `.env`를 읽어 `KEY=VALUE` 파싱. 빈 줄/`#` 주석/`=` 없는 줄은 건너뛰고, 양끝 따옴표 제거 후 `os.environ.setdefault()`로 등록(기존 값 우선). 파일 없으면 조용히 반환.
  - `load_env()` — 프로젝트 루트의 `.env` → 상위 폴더 공용 `.env` 순으로 로드. 전역 `_loaded` 플래그로 1회만 실행.
- **의존성/입출력**: 표준 `os`만 사용. 입력: 루트/상위 `.env`. 출력: `os.environ` 주입. 파일 쓰기·외부 호출 없음.
- **특이사항**: `setdefault`라 이미 설정된 값은 안 덮어씀. 로컬 `.env`가 상위 공용보다 우선. `python-dotenv` 없이 자체 구현.

### `requirements.txt`
- `litellm` — 여러 LLM 제공자(Gemini, Ollama 등)를 단일 인터페이스로 호출하는 통합 클라이언트. (선언된 의존성은 이것 하나. 단, 실제 실행에는 `slack_sdk`, `fpdf2` 등도 필요 — 아래 "미비점" 참고)

### `.env.example`
- `SLACK_BOT_TOKEN` — Slack 봇 토큰(`xoxb-...`). 메시지 수집·permalink·DM 발송용.
- `GEMINI_API_KEY` — Google Gemini API 키. LLM 호출 인증용.
- `GEMINI_MODEL` (선택) — 사용할 Gemini 모델 (기본 `gemini/gemini-flash-latest`).
- `ENABLE_LOCAL_FALLBACK` (선택) — 로컬 LLM 폴백 on/off (기본 `false`).
- `LOCAL_LLM_MODEL` (선택) — 폴백용 로컬 모델 (기본 `ollama/llama3.1`).
- `GEMINI_MIN_INTERVAL_SEC` (선택) — Gemini 호출 최소 간격(초), rate limit 조절 (기본 `4.5`).
- `SLACK_INGEST_CHANNEL_ID` (선택) — 로그 수집 대상 채널 ID.
- `PDF_FONT_PATH` (선택) — PDF용 한글 폰트 경로 (예: NotoSansKR-Regular.ttf).

---

## 3. `reports/` — 데이터·계산·LLM·리포트 생성

### `reports/__init__.py`
- **역할**: `reports`를 파이썬 패키지로 표시하는 빈 파일. 내용 없음.

### `reports/cache_store.py`
- **역할**: LLM 응답을 로컬 JSON으로 캐싱해 동일 입력 재호출을 막는 캐시 저장소 (무료 티어 보호).
- **주요 함수/클래스**:
  - `_slugify(text)` — 파일명 불가 문자를 `_`로 치환 (한글/영숫자/`_-`만 허용).
  - `cache_path(report_type, scope_id, period)` — `CACHE_DIR` 생성 후 세 키를 조합한 `.json` 경로 반환.
  - `compute_input_hash(payload)` — payload를 정렬 JSON으로 직렬화해 SHA-256 계산 (원본 변경 감지).
  - `load(path)` — 캐시 파일 읽어 dict 반환 (없으면 `None`).
  - `save(path, *, input_hash, model, content, generated_at, used_fallback=False)` — 레코드를 JSON 저장.
  - `is_cache_valid(cached, input_hash)` — 저장된 input_hash가 현재 값과 같은지 검사 (캐시 히트 판정).
  - `latest(report_type, scope_id)` — glob으로 해당 scope 캐시들을 사전순 정렬해 가장 최근 것 로드.
- **의존성/입출력**: 표준 `glob/hashlib/json/os/re`; `.data_access`의 `DATA_DIR`. `DATA_DIR/report_cache/*.json` 읽기/쓰기.
- **특이사항**: input_hash 기반 무효화(입력 동일=히트, 변경=재생성). ISO 날짜 파일명의 사전순=시간순을 이용해 최신 탐색.

### `reports/data_access.py`
- **역할**: CSV 목 데이터를 메모리로 로드하고 조회 인덱스를 만드는 데이터 접근 계층 (표준 `csv`만 사용).
- **주요 함수/클래스**:
  - `_read_csv(name)` — `DATA_DIR` 아래 CSV를 `DictReader`로 읽어 dict 리스트 반환.
  - `DataStore` — 6개 CSV(members/goals/kpis/goal_kpi_link/strategy_weights/slack_logs) 로드 + 인덱스(`members_by_id`, `goals_by_id`, `kpis_by_id`, `goals_by_member`, `links_by_goal`, `strategy_by_key`, `logs_by_member`, `members_by_team`) 구성. 로그는 멤버별 날짜순 정렬.
  - `member_goals(member_id)` — 멤버의 goal 목록.
  - `goal_links(goal_id)` — goal에 연결된 `(link, kpi)` 쌍 목록.
  - `logs_in_range(member_id, date_from, date_to)` — 날짜 범위 내 로그 필터.
- **의존성/입출력**: 표준 `csv/os/collections.defaultdict`. `<root>/data/*.csv` 6개 읽기. `DATA_DIR` 상수를 다른 모듈에 제공.
- **특이사항**: `strategy_by_key`는 `(quarter, kpi_name) → priority_score(float)` 매핑으로 우선순위 조회 최적화.

### `reports/priority.py`
- **역할**: KPI/목표 우선순위를 결정론적으로 계산 (`data_dictionary.md` 7절 공식의 실행 코드, LLM 미개입).
- **주요 함수/클래스**:
  - `normalized_gap(kpi_row, direction)` — 달성 갭을 direction(`+`/`-`)에 따라 0~1 정규화, 음수 클램프, target 0이면 0.
  - `kpi_priority(kpi_row, direction, quarter, store)` — 전략 점수 × normalized_gap → `(kpi_priority, gap, score)`. 전략 파라미터 없으면 `ValueError`.
  - `goal_priority(goal_id, quarter, store)` — 링크된 KPI들의 `weight × kpi_priority` 가중합 + 상세 내역. 링크 없는 정성 goal은 `(None, [])`.
  - `rank_team_goals(team, quarter, store)` — 팀 goal을 우선순위 내림차순 `ranked` + 정성 목표 `qualitative`로 분리.
- **의존성/입출력**: 순수 파이썬, `store` 인덱스 조회만. 파일 I/O·외부 호출 없음.
- **특이사항**: 우선순위 값을 LLM이 아닌 코드가 확정해 "이미 계산된 숫자"로 프롬프트에 전달. 정성 목표는 0점이 아니라 "순위 대상 아님"으로 구분.

### `reports/progress.py`
- **역할**: 목표 진행 단계("구획")를 slack_logs의 실제 작업 일수로 결정론적 계산 (`data_dictionary.md` 12절 공식).
- **주요 함수/클래스**:
  - `goal_stage(store, goal_id, today=None)` — 해당 goal 로그의 고유 날짜 수(worked_days)를 세어 `stage = min(total_stages, worked_days // 5)`, 마지막 작업일 대비 경과일로 freshness 문구 생성. `{worked_days, stage, total_stages, last_date, freshness}` 반환.
- **의존성/입출력**: 표준 `datetime.date`; `store`의 `goals_by_id`/`slack_logs` 조회. 파일 I/O·외부 호출 없음.
- **특이사항**: AI 추정 배제(5일=1구획). "완료" 판정은 사람 몫이라 진행률만 반환.

### `reports/prompts.py`
- **역할**: 리포트 3종 + 소원 매칭용 Gemini 프롬프트 빌더 (JSON 전용 출력, LLM은 서술만·숫자 재계산 금지).
- **주요 함수/클래스**:
  - `PROMPT_VERSION` — 프롬프트 템플릿 버전 상수(캐시 input_hash에 포함 → 템플릿 변경 시 캐시 무효화).
  - `_goal_block(goal, links_with_kpi)` — goal 하나를 KPI 연결 포함 텍스트 블록으로 포매팅.
  - `build_personal_weekly_prompt(...)` — 개인 주간 리포트 프롬프트 (highlights/goal_progress/issues/one_on_one_agenda 스키마).
  - `_ranked_goal_block(row, rank)` — 랭킹된 goal 한 줄을 KPI 근거와 함께 포매팅.
  - `build_coaching_cards_prompt(...)` — 팀 코칭 카드 프롬프트 (우선순위 산식 주석·few-shot·3~5개 선별 지침).
  - `build_wish_match_prompt(stuck_member, candidate_log, other_issue_logs)` — 막힌 멤버 문제를 해결 경험자 사례와 매칭.
  - `build_evidence_package_prompt(...)` — 분기 평가 근거 패키지 프롬프트 (goal_evidence/citations/quarter_review_draft 스키마).
- **의존성/입출력**: 순수 문자열 조립, `store`에서 멤버 정보 조회. 반환은 프롬프트 문자열.
- **특이사항**: 공통 `SYSTEM_PREAMBLE`로 "지어내기 금지·숫자 재계산 금지·정리/대조/계수/추출로 역할 제한" 강제. few-shot + 순수 JSON 강제. PROMPT_VERSION 수동 증가 규약.

### `reports/llm_client.py`
- **역할**: litellm 기반 LLM 클라이언트 — Gemini 1차 + (옵션) 로컬 Ollama 2차 폴백, rate limit 페이싱/재시도 포함.
- **주요 함수/클래스**:
  - `_ensure_env()` — `load_env()` 후 `GEMINI_API_KEY` 검증(없으면 `RuntimeError`), 1회만.
  - `_pace()` — 마지막 호출로부터 `GEMINI_MIN_INTERVAL_SEC`(기본 4.5초) 미만이면 sleep.
  - `_call(model, messages, temperature)` — `litellm.completion`을 `response_format={"type":"json_object"}`로 호출.
  - `generate_json(prompt, *, temperature=0.4)` — LLM 호출 후 `(text, model_used, used_fallback)` 반환. RateLimitError 시 지수 백오프 재시도(최대 4회, base 20초), 그 외 예외 시 폴백 판단. `ENABLE_LOCAL_FALLBACK=true`면 로컬 폴백, 아니면 `RuntimeError`.
- **의존성/입출력**: `litellm`, 표준 `os/time`, `env_loader.load_env`. 외부 Gemini API + (옵션) Ollama 호출.
- **특이사항**: 모델을 버전 하드코딩 대신 `gemini/gemini-flash-latest` alias 사용. 무료 티어 보호(최소 간격 + 429 백오프). 폴백 사용 여부를 반환값으로 투명 노출.

### `reports/generate_reports.py`
- **역할**: 리포트 3종(개인 주간 / 팀 코칭 카드 / 평가 근거 패키지) 생성 오케스트레이터이자 CLI 진입점.
- **주요 함수/클래스**:
  - `week_bounds(week_idx)` — 분기 시작 기준 주차별 (시작, 종료) ISO 날짜 계산, QUARTER_END로 클램프.
  - `latest_week_bounds_for_member(store, member_id)` — 멤버 마지막 로그 기준 rolling 7일 창 (없으면 마지막 주 폴백).
  - `parse_json_safely(text)` — LLM 출력 JSON 파싱, ```json 코드블록 제거 재시도, 실패 시 `{"_raw_text", "_parse_error": True}`.
  - `_maybe_generate(...)` — 캐시 확인(히트 시 반환) → dry-run 시 프롬프트만 출력 → `generate_json` → 파싱 → postprocess → 캐시 저장. PROMPT_VERSION을 해시 payload에 포함.
  - `_postprocess_personal(...)` — LLM 출력에서 유효 log_id만 남기고, `goal_stage`로 진척 덮어쓰기, 미언급 goal로 `next_week_priorities`를 코드가 생성.
  - `run_personal(...)` — 멤버 개인 주간 리포트 payload 구성 후 `_maybe_generate`.
  - `_assemble_coaching_cards(...)` — 최종 코칭 카드를 코드가 확정. 구조 이슈(의존 병목)는 `coaching_signals`가 탐지·점수·인용 검증한 것을 권위로 삼고 LLM에서는 headline/prescription 서술만 결합, 개별 이슈는 evidence log_id 실존 검증 + 병목 인물 중복 제외 + KPI순 정렬. 구조 먼저 + 개별, 최대 5건(§5-4-4).
  - `run_coaching(...)` — 팀별 `rank_team_goals` + `detect_signals`(구조 병목 탐지) + 멤버별 최근 5개 로그로 코칭 카드 생성.
  - `run_evidence(...)` — 멤버 goal+링크+연결 로그로 평가 근거 패키지 생성.
  - `main()` — argparse(`--report/--members/--teams/--week/--force/--dry-run`), `DataStore` 로드, member_id 검증 후 실행.
- **의존성/입출력**: 표준 `argparse/json/sys/datetime`; 내부 `cache_store`, `prompts`, `DataStore`, `llm_client.generate_json`, `priority.rank_team_goals`, `progress.goal_stage`. LLM 호출 + 캐시 I/O. `python -m reports.generate_reports`로 실행.
- **특이사항**: 캐싱 우선 + 무료 티어 보호. "순서/숫자/계수·대조" 확정은 LLM이 아닌 코드(postprocess 콜백)가 담당하는 원칙이 일관 적용.

### `reports/coaching_signals.py` `[신설]`
- **역할**: 코칭 카드 신호를 **결정론적으로 탐지·선별**(LLM 미개입). 현재는 의존 병목(dependency bottleneck). 개요 v10 §5-4 반영.
- **주요 함수/클래스**:
  - `_object_tokens(waiting, nonwaiting)` — '막혔을 때만 등장하는' 대상 토큰 추출(대기 로그엔 2건+, 일반 로그엔 0건). 흔한 단어로 인한 과결합 방지.
  - `_cluster_by_objects(...)` — 대상 토큰 공유로 대기 로그를 connected-components 군집화.
  - `detect_dependency_bottlenecks(store, team)` — 2명 이상이 같은 대상을 기다리는 군집을 병목으로 판정. **score = 영향 인원 × 근거 강도 × 지속 기간**, 확신도(3명↑ 높음/2명 중간), 멤버별 대표 인용 근거 반환.
  - `detect_signals(store, team, top_n=5)` — score 내림차순 상위 N 선별(§5-4-4).
- **의존성/입출력**: 표준 `re/datetime/collections`; `store`(DataStore)만. 파일 I/O·외부 호출 없음 → 단위 테스트 용이.
- **특이사항**: 탐지·점수·인용 검증을 코드가 확정하고 LLM은 서술만(프로젝트 공통 원칙, §7-1). 확장 지점: 미해결 요청·재작업 반복·부하 편중·미인지 성과.

### `reports/evidence_pdf.py`
- **역할**: 평가 근거 패키지(evidence_package) 캐시 내용을 한글 지원 PDF로 변환.
- **주요 함수/클래스**:
  - `_resolve_font_path()` — 한글 폰트 경로 탐색(① `PDF_FONT_PATH` → ② 번들 `assets/fonts/NotoSansKR-Regular.ttf` → ③ macOS `AppleSDGothicNeo.ttc` 런타임 추출). 못 찾으면 `RuntimeError`.
  - `build_evidence_pdf(member, content, output_path)` — FPDF로 멤버 헤더·goal별 진척/임팩트/citations·분기 리뷰 초안을 렌더링해 저장, 경로 반환.
- **의존성/입출력**: `fpdf`(fpdf2), 폴백 시 `fontTools.ttLib.TTFont`; 표준 `os/tempfile`. 입력 member+content dict, 출력 PDF.
- **특이사항**: fpdf2 코어 폰트가 한글 미지원이라 유니코드 TTF 필수. macOS 시스템 폰트 폴백은 로컬 개발 전용(재배포 라이선스 없음). bold 전용 폰트 파일이 없어 동일 폰트로 대체.

---

## 4. `slack_app/` — Slack 수집·발행·발송

### `slack_app/__init__.py`
- **역할**: 패키지 마커 (빈 파일).

### `slack_app/home_view.py`
- **역할**: Slack Block Kit 빌더 — 홈 탭 정적 대시보드 + DM용 주간 리포트/코칭 카드 블록 생성.
- **주요 함수/클래스**:
  - `_section/_header/_context/_divider/_bullets(...)` — Block Kit 요소 헬퍼.
  - `build_goal_dashboard_blocks(member, store, quarter, display_name=None)` — 멤버 목표별 "구획 진행률"(`goal_stage`)을 렌더링하는 홈 본문.
  - `build_home_view(...)` — 대시보드 + 안내 문구로 `{"type":"home","blocks":...}` 뷰 반환.
  - `_log_ref(...)` / `_bullets_with_citations(...)` — log_id를 permalink 링크로 렌더링, 인용 불릿 생성.
  - `build_personal_blocks(member, cache_record, ...)` — 캐시된 개인 주간 리포트(성과/진척/이슈/우선순위/1on1)를 블록화, `_parse_error`/폴백 처리.
  - `_card_block(card)` / `build_coaching_blocks(team, cache_record)` — 코칭 카드(채택/필요없음 버튼, 정성 목표 체크인) 블록화.
- **의존성/입출력**: `reports.progress.goal_stage`. LLM 미호출. `store`로 goals/logs 조회.
- **특이사항**: 코칭 카드 버튼(`coaching_card_adopt/dismiss`)은 있으나 인터랙티비티 서버 미연동(주석 명시). 홈 대시보드는 결정론적 진행률만 표시.

### `slack_app/member_map.py`
- **역할**: Slack user_id ↔ member_id 매핑 및 표시 이름 오버라이드 로드/저장.
- **주요 함수/클래스**:
  - `load_member_map()` / `save_member_map(mapping)` — `slack_user_map.json` 읽기/쓰기.
  - `member_to_slack_id(member_id)` — 역방향 조회.
  - `load_display_names()` — `slack_display_names.json`(member_id → 표시 이름) 로드.
  - `display_name_for(member)` — 오버라이드 있으면 그 이름, 없으면 목데이터 이름.
- **의존성/입출력**: `json/os`, `reports.data_access.DATA_DIR`. `slack_user_map.json`, `slack_display_names.json` 읽기/쓰기.
- **특이사항**: 목데이터 이름은 평가/문서 일관성 위해 유지하고, Slack 화면에서만 실사용자 이름으로 보이게 하는 오버라이드.

### `slack_app/ingest.py`
- **역할**: Slack 채널 메시지를 폴링(`conversations.history`)해 새 업무일지를 `slack_logs.csv`에 추가.
- **주요 함수/클래스**:
  - `_load_json/_save_json(...)` — 폴링 상태 JSON 읽기/쓰기.
  - `_next_log_id(existing_logs)` — 기존 `L####` 최대값 +1.
  - `_pick_goal_id(member_id, tag_ids, store)` — `#G05` 태그 → 최근 로그 linked_goal_id → 첫 goal 순으로 목표 연결(소유 검증).
  - `ingest_channel(client, channel_id, *, dry_run=False)` — 마지막 ts 이후 메시지 필터(봇/시스템/확인답장 제외)해 새 로그 append, ts 저장.
  - `main()` — argparse(`--channel`, `--dry-run`), `SLACK_BOT_TOKEN`으로 WebClient 생성.
- **의존성/입출력**: `argparse/csv/json/os/re/sys/datetime`, `slack_sdk.WebClient`, `env_loader`, `DataStore`, `member_map`. `slack_logs.csv`·`slack_ingest_state.json` 읽기/쓰기. Slack API: `conversations_history`.
- **특이사항**: 실시간 Events API 대신 폴링. 소원 매칭 "네/아니오" 답장(`_CONFIRM_REPLY_RE`)을 업무일지로 오수집 안 하게 필터. 실제 메시지는 permalink용 channel_id/ts 보존.

### `slack_app/publish_home.py`
- **역할**: App Home 탭에 "목표 현황" 정적 대시보드 발행 CLI.
- **주요 함수/클래스**:
  - `publish_for_member(client, store, slack_user_id, member_id)` — `build_home_view` → `views_publish`.
  - `main()` — argparse(`--member`, `--all`), member_map 순회.
- **의존성/입출력**: `argparse/os/sys`, `slack_sdk.WebClient`, `env_loader`, `DataStore`, `home_view.build_home_view`, `member_map`. Slack API: `views_publish`.
- **특이사항**: LLM 미호출, CSV 즉석 읽어 항상 최신 표시. `QUARTER = "2026-Q2"` 하드코딩.

### `slack_app/publish_mentoring_preview.py`
- **역할**: 멘토링 데모용 "매일 현황" 홈 탭 임시 정적 미리보기 발행(문구 하드코딩, 실제 로직 미구현).
- **주요 함수/클래스**:
  - `_section/_header/_context/_divider(...)` — 로컬 Block Kit 헬퍼.
  - `build_preview_blocks(display_name)` — 목업 문구가 하드코딩된 홈 뷰 블록.
  - `main()` — argparse(`--member` 필수), `views_publish`.
- **의존성/입출력**: `argparse/os/sys`, `slack_sdk.WebClient`, `env_loader`, `DataStore`, `member_map`. Slack API: `views_publish`.
- **특이사항**: 데모 전용 — `publish_home --member M01` 재실행 시 실제 대시보드로 복원. 도시/세계 시각화는 텍스트 placeholder만.

### `slack_app/send_weekly_dm.py`
- **역할**: 개인 주간 리포트를 DM 메시지로 발송하는 CLI.
- **주요 함수/클래스**:
  - `_collect_log_ids(content)` — highlights/goal_progress/issues/1on1에서 인용 log_id 수집.
  - `_resolve_permalinks(client, store, log_ids)` — 실제 메시지(channel_id/ts 보유)만 `chat_getPermalink` 조회(목데이터는 건너뜀).
  - `send_for_member(...)` — `personal_weekly` 캐시 → permalink 해석 → `build_personal_blocks` → `chat_postMessage`.
  - `main()` — argparse(`--member`, `--all`).
- **의존성/입출력**: `slack_sdk.WebClient`, `env_loader`, `cache_store`, `DataStore`, `home_view.build_personal_blocks`, `member_map`. Slack API: `chat_getPermalink`, `chat_postMessage`.
- **특이사항**: LLM 미호출(캐시만 발송). permalink 실패는 발송을 막지 않고 경고만. cron 미연동(수동, 금요일 발송 의도). 가짜 링크 생성 안 함.

### `slack_app/send_coaching_dm.py`
- **역할**: 팀 코칭 카드를 리더(1차/2차 평가자)에게 DM 발송하는 CLI.
- **주요 함수/클래스**:
  - `send_for_member(...)` — 리더 검증 후 `cache_store.latest("coaching_card", team)` → `build_coaching_blocks` → `chat_postMessage`.
  - `main()` — argparse(`--member`, `--all`).
- **의존성/입출력**: `slack_sdk.WebClient`, `env_loader`, `cache_store`, `DataStore`, `home_view.build_coaching_blocks`, `member_map`. Slack API: `chat_postMessage`.
- **특이사항**: LLM 미호출(사전 생성 캐시만). 리더 아니면 skip. 먼저 `reports.generate_reports --report coaching` 필요.

### `slack_app/send_evidence_package.py`
- **역할**: 평가 근거 패키지를 PDF로 렌더링해 Slack DM에 첨부 발송하는 CLI.
- **주요 함수/클래스**:
  - `send_for_member(...)` — `evidence_package` 캐시 → `build_evidence_pdf` → `conversations_open`(DM 채널 확보) → `files_upload_v2` 업로드.
  - `main()` — argparse(`--member`, `--all`).
- **의존성/입출력**: `slack_sdk.WebClient`, `env_loader`, `cache_store`, `DataStore`, `evidence_pdf.build_evidence_pdf`, `generate_reports.QUARTER`, `member_map`; 표준 `tempfile`. Slack API: `conversations_open`, `files_upload_v2`.
- **특이사항**: `files_upload_v2`가 user_id가 아닌 실제 DM 채널(D...) id를 요구해 `conversations_open` 선행. `_parse_error` 캐시는 skip. PDF에 표시 이름 오버라이드 반영.

### `slack_app/wish_match.py`
- **역할**: 소원 매칭 — 업무일지에서 "막힌 상황"을 감지해 과거 유사 문제 해결자를 LLM으로 찾아 DM으로 연결.
- **주요 함수/클래스**:
  - `_load_json/_save_json/_now_iso(...)` — 상태·시각 헬퍼.
  - `_has_issue_keyword(text)` / `_candidate_logs(...)` / `_other_issue_logs(...)` — 이슈 키워드 기반 후보/타 멤버 로그 추출.
  - `_open_dm(client, slack_user_id)` — `conversations_open`으로 DM 채널 id.
  - `scan(client, store)` — 후보 로그를 `build_wish_match_prompt` + `generate_json`으로 판단, AI가 지목한 helper log_id/member_id를 코드로 검증 후 당사자에게 확인 DM, `wish_pending.json` 기록.
  - `check_replies(client, store)` — 대기 DM 채널을 폴링해 "네/아니오"(`CONFIRM_YES_RE`/`CONFIRM_NO_RE`) 답장 확인, 확정 시 helper에게 협조 요청 DM.
  - `main()` — argparse(`--scan`, `--check-replies`).
- **의존성/입출력**: `slack_sdk.WebClient`, `env_loader`, `prompts`, `DataStore`, `llm_client.generate_json`, `member_map`; 표준 `json/os/re/datetime`. `wish_pending.json`·`slack_ingest_state.json` 읽기/쓰기. Slack API: `conversations_open`, `chat_postMessage`, `conversations_history`.
- **특이사항**: LLM은 "매칭 그럴듯함"만 판단, log_id/member_id 실존은 코드가 검증(인용 검증에 AI 미사용). 전 과정 DM 기반이라 타 팀원 미노출.

---

## 5. `seed/` 와 데이터 파일

### `seed/generate_mock_data.py`
- **역할**: ENSAPIA용 목(mock) 데이터를 `data/`에 CSV로 생성하는 시드 스크립트.
- **주요 함수/클래스**:
  - `stage_for_week(week_idx)` — 주차를 3단계 내러티브로 매핑(0~3주 stage1, 4~8주 stage2, 9주+ stage3).
  - `build_slack_logs()` — 멤버×주차 순회로 목표별 단계 서술 + 22% 확률 이슈 멘션을 섞어 로그 생성. 날짜·멤버 정렬 후 `log_id`(`L0001`…)를 날짜순 재부여. `channel_id`/`ts`는 빈 값(실제 permalink 근거 없음).
  - `write_csv(path, header, rows)` — UTF-8 CSV 기록.
  - `main()` — `data/` 생성 후 6개 CSV를 쓰고 각 행 수 출력.
- **의존성/입출력**: 표준 `csv/os/random/datetime`. 입력 없음(상수 데이터). 출력: `data/` 아래 6개 CSV.
- **특이사항**: `SEED=42` 고정으로 재현성 보장. 파생 지표(DAU/매출/LTV 등)는 독립 랜덤이 아니라 기준 지표(MAU/고착도/ARPU/이탈률/K-factor)에서 계산(예: `DAU = round(MAU × 고착도)`). `goal_kpi_link` weight는 목표별 합=1.0. `data_dictionary.md`의 산식·전략 가중치 참조.

### 데이터 파일 개요 (`data/`)
`main()`이 생성하는 CSV 6종:
- `members.csv` — `member_id, name, team, role`. 멤버 15명(M01~M15).
- `goals.csv` — `goal_id, member_id, quarter, title, type, total_stages`. 목표 19개(G01~G19).
- `kpis.csv` — `kpi_id, name, unit, target, current_value, team`. KPI 9개(K01~K09).
- `goal_kpi_link.csv` — `goal_id, kpi_id, weight, direction`. 목표-KPI 다대다 연결 18개.
- `strategy_weights.csv` — `quarter, kpi_name, priority_score`. 분기별 KPI 우선순위 9행.
- `slack_logs.csv` — `log_id, member_id, date, text, linked_goal_id, channel_id, ts`. `build_slack_logs()`가 동적 생성(행 수 난수, `channel_id`/`ts` 빈 값).

이 외에 런타임 상태/캐시 파일:
- `data/report_cache/*.json` — LLM 리포트 캐시 (cache_store).
- `data/slack_user_map.json`, `data/slack_display_names.json` — 사용자 매핑/표시 이름.
- `data/slack_ingest_state.json` — 채널별 마지막 폴링 ts.
- `data/wish_pending.json` — 소원 매칭 대기 상태.
- `data/data_dictionary.md` — 데이터 스키마·산식·전략 가중치 정의 문서(코드가 참조).

---

## 6. 실행 흐름 (CLI 요약)

```bash
# 1) 목 데이터 생성
python -m seed.generate_mock_data

# 2) 리포트 생성 (LLM 호출 → 캐시 저장)
python -m reports.generate_reports --report personal --members M01 M03
python -m reports.generate_reports --report coaching --teams 사업부
python -m reports.generate_reports --report evidence --members M01
#   --dry-run: 프롬프트만 출력(LLM 미호출), --force: 캐시 무시 재생성

# 3) Slack 발행/발송 (캐시를 읽어 전송)
python -m slack_app.publish_home --all
python -m slack_app.send_weekly_dm --all
python -m slack_app.send_coaching_dm --all
python -m slack_app.send_evidence_package --member M01

# 부가: 실제 Slack 로그 수집 / 소원 매칭
python -m slack_app.ingest --channel <CHANNEL_ID>
python -m slack_app.wish_match --scan
python -m slack_app.wish_match --check-replies
```

---

## 7. 관찰된 특징 · 참고

- **역할 분리가 명확**: `reports/`(생성) ↔ `slack_app/`(전송)이 캐시(JSON)를 경계로 느슨하게 결합. 발송 계층은 대부분 LLM을 호출하지 않고 캐시만 읽는다.
- **결정론 우선**: 우선순위(`priority.py`)·진행 구획(`progress.py`)은 순수 계산이라 테스트·재현이 쉽고, LLM 출력은 후처리(`_postprocess_*`)에서 코드가 교정한다.
- **무료 티어/rate limit 방어**가 곳곳에 설계됨(호출 간격, 지수 백오프, 캐시, dry-run).
- **참고할 미비점**:
  - `requirements.txt`에는 `litellm`만 선언돼 있으나 실제 실행에는 `slack_sdk`(전체 slack_app), `fpdf2`(evidence_pdf), 폴백 시 `fonttools`가 필요하다 → 의존성 목록 보강 권장.
  - 코칭 카드의 채택/필요없음 버튼은 인터랙티비티(요청 처리) 서버가 없어 현재 동작하지 않는다(주석에 명시됨).
  - 발송 스크립트들이 cron/스케줄러에 연결돼 있지 않아 현재는 수동 실행이다.
  - `publish_mentoring_preview.py`는 데모용 하드코딩으로, 실제 "매일 현황" 추적 로직은 미구현.
