#!/usr/bin/env python3
"""
data/report_cache/*.json (리포트 3종 캐시) -> data/treerings.db 의 report_cache 테이블로 일회성 이전.

기존 treerings.db(scripts/migrate_csv_to_sqlite.py로 생성됨)에 report_cache 테이블만 추가하고,
파일명(`{report_type}__{scope_id}__{period}.json`)에서 report_type/scope_id/period를 그대로
파싱해 그 파일의 레코드(input_hash/model/used_fallback/generated_at/content)를 삽입한다.
원본 JSON은 data/legacy_report_cache/ 로 이동해 스냅샷으로 보존한다(삭제하지 않음).

이 이전 이후로는 reports/cache_store.py 가 파일이 아니라 이 테이블을 읽고 쓴다.

사용 예:
  python3 -m scripts.migrate_report_cache_to_sqlite
"""

import glob
import json
import os
import shutil

from reports.data_access import DATA_DIR, get_connection

CACHE_DIR = os.path.join(DATA_DIR, "report_cache")
LEGACY_CACHE_DIR = os.path.join(DATA_DIR, "legacy_report_cache")

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_cache (
    report_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    period TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    used_fallback INTEGER NOT NULL DEFAULT 0,
    generated_at TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (report_type, scope_id, period)
);
"""


def _parse_filename(path):
    name = os.path.splitext(os.path.basename(path))[0]
    parts = name.split("__")
    if len(parts) != 3:
        raise ValueError(f"파일명 형식이 예상과 다릅니다({{report_type}}__{{scope_id}}__{{period}}.json): {path!r}")
    return tuple(parts)  # report_type, scope_id, period


def migrate():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)

        paths = sorted(glob.glob(os.path.join(CACHE_DIR, "*.json")))
        if not paths:
            print("이전할 report_cache JSON 파일이 없습니다.")
            return

        for path in paths:
            report_type, scope_id, period = _parse_filename(path)
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
            conn.execute(
                "INSERT INTO report_cache (report_type, scope_id, period, input_hash, model, "
                "used_fallback, generated_at, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(report_type, scope_id, period) DO UPDATE SET "
                "input_hash=excluded.input_hash, model=excluded.model, used_fallback=excluded.used_fallback, "
                "generated_at=excluded.generated_at, content=excluded.content",
                (report_type, scope_id, period, record["input_hash"], record["model"],
                 int(record.get("used_fallback", False)), record["generated_at"],
                 json.dumps(record["content"], ensure_ascii=False)),
            )
            print(f"  [migrated] {report_type} / {scope_id} / {period}")
        conn.commit()
    finally:
        conn.close()

    count = get_connection().execute("SELECT COUNT(*) FROM report_cache").fetchone()[0]
    print(f"report_cache: {count} rows")

    os.makedirs(LEGACY_CACHE_DIR, exist_ok=True)
    for path in paths:
        shutil.move(path, os.path.join(LEGACY_CACHE_DIR, os.path.basename(path)))
    print(f"원본 JSON -> {LEGACY_CACHE_DIR}/ 로 이동 완료 (스냅샷 보존, 런타임 코드는 더 이상 참조하지 않음)")


if __name__ == "__main__":
    migrate()
