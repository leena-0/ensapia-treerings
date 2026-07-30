# -*- coding: utf-8 -*-
"""
코칭 카드 기능 데이터 스키마 (구현명세_코칭카드 §3).

pydantic 모델로 입력(WorkLog/WeeklyStatusSelection/TeamContext)과
출력(CoachingCard/Evidence/UrgentAlert)을 정의한다.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 입력
# ---------------------------------------------------------------------------
class WorkLog(BaseModel):
    message_id: str            # 로그 고유 식별자 (Slack ts 또는 목데이터 log_id)
    user_id: str
    user_name: str
    permalink: str = ""        # 원문 링크 (없으면 빈 문자열)
    timestamp: datetime
    text: str


class WeeklyStatusSelection(BaseModel):
    """주간 리포트에서 본인이 고른 상태 (명세 제약 1의 입력원).

    AI가 정체를 자동 판정하지 않고, 본인이 `blocked`로 표시한 것만 코칭 후보가 된다.
    """
    user_id: str
    subgoal_id: str
    subgoal_title: str
    status: str                # in_progress / waiting / on_hold / blocked
    week_of: date
    note: Optional[str] = None


class TeamContext(BaseModel):
    team_id: str
    members: list[dict]        # {user_id, user_name, role}
    manager_id: str            # 카드 수신자(1차평가자 = 관리 책임자)
    senior_manager_id: str = ""  # 2차평가자. 없으면 빈 문자열(팀에 2차평가자가 없는 경우)
    period_start: date
    period_end: date           # 격주이므로 14일 가정


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
class Evidence(BaseModel):
    message_id: str
    user_name: str
    permalink: str = ""
    excerpt: str               # 원문 발췌 (15단어 이내 권장)
    timestamp: datetime


# 탐지 유형 (명세 §4)
CARD_TYPES = (
    "dependency_bottleneck",   # 여러 사람이 같은 대상을 기다림 (최고 가치)
    "unresolved_request",      # 도움/결정 요청 후 후속 없음
    "rework_loop",             # 수정/롤백/재작업 누적
    "load_imbalance",          # 한 사람 항목 수가 팀 평균 대비 과다
    "unrecognized_work",       # 눈에 띄는 성과인데 언급 없음
)


class CoachingCard(BaseModel):
    card_id: str
    card_type: str
    priority_score: float = 0.0   # 선별 점수 (§5)
    confidence: str = "medium"    # high / medium  (low는 생성 안 함)
    headline: str = ""            # 한 줄 요약
    body: str = ""                # 확신도에 따라 조언문 또는 질문형
    subjects: list[str] = Field(default_factory=list)   # 관련 구성원 user_id (수신자에게만 노출)
    evidence: list[Evidence] = Field(default_factory=list)
    affected_count: int = 0
    duration_days: int = 0


class UrgentAlert(BaseModel):
    """긴급 사안 — 격주를 기다리지 않고 즉시 DM (명세 §7-5)."""
    alert_id: str
    card_type: str = "dependency_bottleneck"
    reason: str = ""
    headline: str = ""
    subjects: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    detected_at: datetime
