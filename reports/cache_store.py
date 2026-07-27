"""
리포트 캐시 저장소. LLM 응답을 data/treerings.db 의 report_cache 테이블에 캐싱해 동일 입력에
대한 재호출을 막는다. (무료 티어 rate limit/쿼터 보호가 목적 -- 매 요청마다 API를 부르지 않는다)

원래는 data/report_cache/*.json 파일이었으나(scripts/migrate_report_cache_to_sqlite.py 로 이전됨,
원본은 data/legacy_report_cache/ 에 스냅샷 보존), 관리자가 리포트 내용 자체를 SQL로 조회(예:
`SELECT scope_id, json_extract(content,'$.impact_summary') FROM report_cache WHERE report_type=...`)
할 수 있어야 한다는 요구 때문에 SQLite로 옮겼다.

캐시 키 = report_type + scope_id + period 의 조합(테이블 PK)이고, 같이 저장된 input_hash(그
리포트를 만든 원본 데이터의 해시)로 캐시 유효성을 판단한다.
다음 생성 시도 때 input_hash 가 그대로면 -> 캐시 히트로 API 호출을 생략.
input_hash 가 달라졌으면(새 업무일지 추가 등) -> 재생성.
"""

import hashlib
import json

from .data_access import get_connection


def compute_input_hash(payload):
    """payload: 리포트 생성에 쓰인 원본 데이터(딕셔너리/리스트 등 JSON 직렬화 가능한 것)."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_path(report_type, scope_id, period):
    """캐시 row 를 식별하는 키. 이름은 파일 경로 시절 그대로 유지해 호출부(generate_reports.py 등)를
    바꾸지 않아도 되게 했다 -- 지금은 (report_type, scope_id, period) 튜플일 뿐이다."""
    return (report_type, scope_id, period)


def _row_to_record(row):
    if row is None:
        return None
    return {
        "input_hash": row["input_hash"],
        "model": row["model"],
        "used_fallback": bool(row["used_fallback"]),
        "generated_at": row["generated_at"],
        "content": json.loads(row["content"]),
    }


def load(key):
    report_type, scope_id, period = key
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT input_hash, model, used_fallback, generated_at, content FROM report_cache "
            "WHERE report_type = ? AND scope_id = ? AND period = ?",
            (report_type, scope_id, period),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_record(row)


def save(key, *, input_hash, model, content, generated_at, used_fallback=False):
    report_type, scope_id, period = key
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO report_cache (report_type, scope_id, period, input_hash, model, "
                "used_fallback, generated_at, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(report_type, scope_id, period) DO UPDATE SET "
                "input_hash=excluded.input_hash, model=excluded.model, used_fallback=excluded.used_fallback, "
                "generated_at=excluded.generated_at, content=excluded.content",
                (report_type, scope_id, period, input_hash, model, int(used_fallback), generated_at,
                 json.dumps(content, ensure_ascii=False)),
            )
    finally:
        conn.close()
    return {
        "input_hash": input_hash, "model": model, "used_fallback": used_fallback,
        "generated_at": generated_at, "content": content,
    }


def is_cache_valid(cached, input_hash):
    return cached is not None and cached.get("input_hash") == input_hash


def latest(report_type, scope_id):
    """
    scope_id 에 대한 가장 최근 캐시를 읽는다 (period 문자열 내림차순 정렬 = 시간순 정렬, 파일명
    사전순 정렬이던 시절과 동일한 가정 -- period 가 날짜로 시작하는 문자열이라 성립).
    personal_weekly 처럼 period 가 매번 바뀌는 리포트에서 "최신 것"을 찾을 때 사용.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT input_hash, model, used_fallback, generated_at, content FROM report_cache "
            "WHERE report_type = ? AND scope_id = ? ORDER BY period DESC LIMIT 1",
            (report_type, scope_id),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_record(row)
