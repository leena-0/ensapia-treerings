"""
리포트 캐시 저장소. LLM 응답을 로컬 JSON 파일로 캐싱해 동일 입력에 대한 재호출을 막는다.
(무료 티어 rate limit/쿼터 보호가 목적 -- 매 요청마다 API를 부르지 않는다)

캐시 키 = report_type + scope_id + period 의 조합으로 파일 경로를 정하고,
파일 내부에 input_hash(그 리포트를 만든 원본 데이터의 해시)를 같이 저장한다.
다음 생성 시도 때 input_hash 가 그대로면 -> 캐시 히트로 API 호출을 생략.
input_hash 가 달라졌으면(새 업무일지 추가 등) -> 재생성.
"""

import glob
import hashlib
import json
import os
import re

from .data_access import DATA_DIR

CACHE_DIR = os.path.join(DATA_DIR, "report_cache")


def _slugify(text):
    return re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", text)


def cache_path(report_type, scope_id, period):
    os.makedirs(CACHE_DIR, exist_ok=True)
    filename = f"{_slugify(report_type)}__{_slugify(scope_id)}__{_slugify(period)}.json"
    return os.path.join(CACHE_DIR, filename)


def compute_input_hash(payload):
    """payload: 리포트 생성에 쓰인 원본 데이터(딕셔너리/리스트 등 JSON 직렬화 가능한 것)."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, *, input_hash, model, content, generated_at, used_fallback=False):
    record = {
        "input_hash": input_hash,
        "model": model,
        "used_fallback": used_fallback,
        "generated_at": generated_at,
        "content": content,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def is_cache_valid(cached, input_hash):
    return cached is not None and cached.get("input_hash") == input_hash


def latest(report_type, scope_id):
    """
    scope_id 에 대한 가장 최근 캐시를 읽는다 (파일명에 ISO 날짜/기간이 들어있어 사전순 정렬 =
    시간순 정렬). personal_weekly 처럼 period 가 매번 바뀌는 리포트에서 "최신 것"을 찾을 때 사용.
    """
    pattern = os.path.join(CACHE_DIR, f"{_slugify(report_type)}__{_slugify(scope_id)}__*.json")
    matches = sorted(glob.glob(pattern))
    if not matches:
        return None
    return load(matches[-1])
