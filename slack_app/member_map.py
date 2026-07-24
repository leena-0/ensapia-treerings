"""Slack user_id <-> member_id 매핑. 실제 워크스페이스 사용자와 목 데이터 멤버를 잇는 설정 파일."""

import json
import os

from reports.data_access import DATA_DIR

MEMBER_MAP_PATH = os.path.join(DATA_DIR, "slack_user_map.json")
DISPLAY_NAMES_PATH = os.path.join(DATA_DIR, "slack_display_names.json")


def load_member_map():
    if not os.path.exists(MEMBER_MAP_PATH):
        return {}
    with open(MEMBER_MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


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
    이름으로 보이도록 하는 용도 (예: 실제 계정 이나영 <-> 목데이터 M01 "정지원").
    """
    if not os.path.exists(DISPLAY_NAMES_PATH):
        return {}
    with open(DISPLAY_NAMES_PATH, encoding="utf-8") as f:
        return json.load(f)


def display_name_for(member):
    return load_display_names().get(member["member_id"], member["name"])
