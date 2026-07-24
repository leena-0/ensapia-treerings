"""프로젝트 공용 .env 로더. 코드에 시크릿을 하드코딩하지 않기 위한 유일한 진입점."""

import os

_loaded = False


def _load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def load_env():
    """프로젝트 루트(.env)와 상위 폴더(공용 .env)를 순서대로 로드한다. 이미 로드했으면 재사용."""
    global _loaded
    if _loaded:
        return
    project_root = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(project_root)
    _load_env_file(os.path.join(project_root, ".env"))
    _load_env_file(os.path.join(parent_dir, ".env"))
    _loaded = True
