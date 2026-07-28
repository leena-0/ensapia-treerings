"""
LLM 클라이언트 (litellm 기반, 1차 프로바이더 선택 + 로컬 LLM 2차 폴백).

- 1차 프로바이더는 LLM_PROVIDER 로 고른다 ("upstage"(기본) 또는 "gemini"):
  - upstage(기본): Solar Pro(한국어 특화, OpenAI 호환 API). litellm에 "upstage/" 전용
    프로바이더가 없어도 되게, model 문자열을 "openai/<모델명>"으로 두고 api_base/api_key 를
    직접 넘긴다(litellm의 OpenAI 호환 커스텀 엔드포인트 방식 -- 신규 프로바이더 추가 시에도
    이 패턴만 따르면 됨). Gemini 무료 티어가 일일 요청 20건 한도라 8사이클(24회 호출)짜리
    배치조차 못 끝내는 걸 실측 확인한 뒤 기본값을 이쪽으로 바꿨다.
  - gemini: `gemini/gemini-flash-latest` (Google이 제공하는 "latest" alias --
    특정 버전을 하드코딩하면 서비스 종료/신규 사용자 제공 중단으로 즉시 깨지는 것을 실측 확인했음).
    LLM_PROVIDER=gemini 로 명시하면 되돌릴 수 있다.
- 2차(옵션): 로컬 LLM(Ollama) 폴백. 이 프로젝트의 배포 대상은 (Cloud Run이 아니라) GCE VM이므로
  상시 기동 상태에서 같은 VM에 Ollama를 띄워, 1차 프로바이더 rate limit/장애 시 실제
  프로덕션 폴백으로 사용할 수 있다. 단, 로컬 모델 품질이 1차보다 낮을 수 있으므로
  기본값은 비활성화(ENABLE_LOCAL_FALLBACK=false)이며, 명시적으로 켜야 동작한다.
  폴백 사용 여부는 호출부(cache_store)에 model/used_fallback 으로 기록되어 투명하게 남는다.
- litellm 을 쓰는 이유: 여러 프로바이더를 동일한 completion() 인터페이스로 호출할 수 있어
  프로바이더 교체가 모델 문자열 하나 바꾸는 것으로 끝난다.

환경변수:
  LLM_PROVIDER            "upstage"(기본) 또는 "gemini"
  GEMINI_API_KEY          Gemini API 키 (LLM_PROVIDER=gemini일 때 필수)
  GEMINI_MODEL            Gemini 모델 문자열 (기본 "gemini/gemini-flash-latest")
  GEMINI_MIN_INTERVAL_SEC Gemini 호출 최소 간격(초, 기본 4.5 -- 무료 티어 분당 요청 한도 보호)
  UPSTAGE_API_KEY         Upstage API 키 (LLM_PROVIDER=upstage일 때 필수)
  UPSTAGE_MODEL           Solar 모델명 (기본 "solar-pro3". Solar Pro 2는 "solar-pro2")
  UPSTAGE_API_BASE        Upstage API 베이스 URL (기본 "https://api.upstage.ai/v1")
  UPSTAGE_MIN_INTERVAL_SEC Upstage 호출 최소 간격(초, 기본 0 -- 크레딧 기반이라 별도 페이싱 불필요)
  ENABLE_LOCAL_FALLBACK   "true"일 때만 로컬 LLM 폴백 시도 (기본 false)
  LOCAL_LLM_MODEL         2차 모델 문자열 (기본 "ollama/llama3.1")
  OLLAMA_API_BASE         Ollama 서버 주소 (기본 http://localhost:11434, GCE VM 등 원격 지정 가능)
"""

import os
import time

import litellm

from env_loader import load_env

DEFAULT_PRIMARY_MODEL = "gemini/gemini-flash-latest"
DEFAULT_UPSTAGE_MODEL = "solar-pro3"
DEFAULT_UPSTAGE_API_BASE = "https://api.upstage.ai/v1"
DEFAULT_FALLBACK_MODEL = "ollama/llama3.1"
DEFAULT_MIN_INTERVAL_SEC = 4.5
MAX_RETRIES = 4
BACKOFF_BASE_SEC = 20

_last_call_ts = 0.0
_env_checked = False


def _provider():
    # 기본 프로바이더는 upstage(Solar Pro 3) — Gemini 무료 티어의 일일 20건 한도로 대량 배치가
    # 끝까지 못 도는 걸 실측 확인한 뒤 고정. gemini로 되돌리려면 LLM_PROVIDER=gemini 로 지정.
    return os.environ.get("LLM_PROVIDER", "upstage").strip().lower()


def _ensure_env():
    global _env_checked
    if _env_checked:
        return
    load_env()
    _env_checked = True
    provider = _provider()
    if provider == "upstage":
        if not os.environ.get("UPSTAGE_API_KEY"):
            raise RuntimeError(
                "UPSTAGE_API_KEY 환경변수가 없습니다. 프로젝트 루트 또는 상위 폴더의 .env 에 "
                "UPSTAGE_API_KEY=... 를 설정하세요."
            )
    elif not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError(
            "GEMINI_API_KEY 환경변수가 없습니다. 프로젝트 루트 또는 상위 폴더의 .env 에 "
            "GEMINI_API_KEY=... 를 설정하세요."
        )


def _primary_config():
    """1차 프로바이더의 (model, extra_kwargs, min_interval) 을 LLM_PROVIDER 기준으로 만든다."""
    if _provider() == "upstage":
        model = f"openai/{os.environ.get('UPSTAGE_MODEL', DEFAULT_UPSTAGE_MODEL)}"
        kwargs = {
            "api_base": os.environ.get("UPSTAGE_API_BASE", DEFAULT_UPSTAGE_API_BASE),
            "api_key": os.environ["UPSTAGE_API_KEY"],
        }
        min_interval = float(os.environ.get("UPSTAGE_MIN_INTERVAL_SEC", 0))
        return model, kwargs, min_interval
    model = os.environ.get("GEMINI_MODEL", DEFAULT_PRIMARY_MODEL)
    min_interval = float(os.environ.get("GEMINI_MIN_INTERVAL_SEC", DEFAULT_MIN_INTERVAL_SEC))
    return model, {}, min_interval


def _pace(min_interval):
    global _last_call_ts
    elapsed = time.time() - _last_call_ts
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_call_ts = time.time()


def _call(model, messages, temperature, **extra):
    return litellm.completion(
        model=model,
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},
        **extra,
    )


def generate_json(prompt, *, temperature=0.4):
    """
    프롬프트를 LLM에 보내고 (text, model_used, used_fallback) 을 반환한다.
    text 는 JSON 문자열이어야 한다(response_format=json_object 로 강제).
    """
    _ensure_env()
    primary, primary_kwargs, min_interval = _primary_config()
    messages = [{"role": "user", "content": prompt}]

    last_err = None
    for attempt in range(MAX_RETRIES):
        _pace(min_interval)
        try:
            resp = _call(primary, messages, temperature, **primary_kwargs)
            return resp.choices[0].message.content, primary, False
        except litellm.RateLimitError as e:
            last_err = e
            if attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE_SEC * (2 ** attempt)
                print(f"  [rate limit] {wait}초 대기 후 재시도 ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
        except Exception as e:  # noqa: BLE001 - 키 오류 등 재시도 대상이 아닌 오류는 즉시 폴백 판단으로
            last_err = e
            break

    if os.environ.get("ENABLE_LOCAL_FALLBACK", "false").lower() == "true":
        fallback = os.environ.get("LOCAL_LLM_MODEL", DEFAULT_FALLBACK_MODEL)
        print(f"  [fallback] {primary} 호출 실패({last_err!r}) -> 로컬 LLM({fallback})으로 폴백")
        resp = _call(fallback, messages, temperature)
        return resp.choices[0].message.content, fallback, True

    raise RuntimeError(
        f"{primary} 호출 실패, 로컬 폴백 비활성화 상태입니다(ENABLE_LOCAL_FALLBACK=true 로 켤 수 있음): {last_err!r}"
    ) from last_err
