"""Slack user_id <-> member_id 매핑. 실제 워크스페이스 사용자와 목 데이터 멤버를 잇는 설정 파일.

개인 매핑(본인의 실제 Slack user_id/실명)은 이 파일들에 커밋하지 않는다 — 팀 공유 저장소에
개인정보가 그대로 올라가는 걸 피하기 위해, .env 의 SLACK_MY_USER_ID/SLACK_MY_MEMBER_ID/
SLACK_MY_DISPLAY_NAME 로 로컬에만 오버라이드한다(.env 는 gitignore됨). 커밋되는 JSON 파일은
팀 전체가 공유해도 되는 매핑(있다면)만 담는다.
"""

import json
import os

from env_loader import load_env
from reports.data_access import DATA_DIR

MEMBER_MAP_PATH = os.path.join(DATA_DIR, "slack_user_map.json")
DISPLAY_NAMES_PATH = os.path.join(DATA_DIR, "slack_display_names.json")


def load_member_map():
    load_env()
    mapping = {}
    if os.path.exists(MEMBER_MAP_PATH):
        with open(MEMBER_MAP_PATH, encoding="utf-8") as f:
            mapping = json.load(f)
    my_slack_id = os.environ.get("SLACK_MY_USER_ID", "").strip()
    my_member_id = os.environ.get("SLACK_MY_MEMBER_ID", "").strip()
    if my_slack_id and my_member_id:
        # 같은 member_id를 가리키던 기존 항목(커밋된 JSON의 값)은 지우고 오버라이드로 대체한다.
        # 안 지우면 두 키가 같은 member_id를 가리키게 되어, dict 순회 순서상 원본이 먼저
        # 매치돼 오버라이드가 조용히 무시된다.
        mapping = {sid: mid for sid, mid in mapping.items() if mid != my_member_id}
        mapping[my_slack_id] = my_member_id
    return mapping


def save_member_map(mapping):
    with open(MEMBER_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


def member_to_slack_id(member_id):
    mapping = load_member_map()
    for slack_id, mid in mapping.items():
        if mid == member_id:
            return slack_id
    return None


def load_display_names():
    """
    member_id -> Slack 표시용 이름 오버라이드. 목 데이터(members.csv)의 이름은 평가/문서
    일관성을 위해 그대로 두고, 실제 테스트 계정에 매핑했을 때 Slack 화면에서만 실사용자
    이름으로 보이도록 하는 용도. 본인 실명은 .env의 SLACK_MY_DISPLAY_NAME 로 오버라이드한다.
    """
    load_env()
    names = {}
    if os.path.exists(DISPLAY_NAMES_PATH):
        with open(DISPLAY_NAMES_PATH, encoding="utf-8") as f:
            names = json.load(f)
    my_member_id = os.environ.get("SLACK_MY_MEMBER_ID", "").strip()
    my_display_name = os.environ.get("SLACK_MY_DISPLAY_NAME", "").strip()
    if my_member_id and my_display_name:
        names = dict(names)
        names[my_member_id] = my_display_name
    return names


def display_name_for(member):
    return load_display_names().get(member["member_id"], member["name"])
