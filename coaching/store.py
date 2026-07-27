# -*- coding: utf-8 -*-
"""
카드 저장소 (인메모리).

명세는 PostgreSQL을 가정하나 데모 단계에서는 인메모리로 둔다. 저장 계층만 교체하면 된다.
- 상위 3~5건은 매니저에게 전달, 나머지 후보는 파이프라인에서 이미 폐기(§7 제약 7).
- 채택/기각 액션을 기록해 채택률 지표로 쓴다(§5-4-4).
- 권한(제약 6): 카드 조회는 수신 매니저만. 카드에 언급된 대상자 본인은 볼 수 없다.
"""
from __future__ import annotations

from datetime import datetime, timezone


class CardStore:
    def __init__(self):
        self._batches: dict[tuple, dict] = {}     # (team, period) -> batch
        self._card_owner: dict[str, str] = {}     # card_id -> manager_id
        self._card_index: dict[str, object] = {}  # card_id -> CoachingCard
        self._actions: list[dict] = []

    def save(self, team, period, manager_id, cards, urgent):
        # 배치 내 card_id 가 팀/기간을 넘어 충돌하지 않도록 접두어 부여
        for c in cards:
            c.card_id = f"{team}_{period}_{c.card_id}"
            self._card_owner[c.card_id] = manager_id
            self._card_index[c.card_id] = c
        self._batches[(team, period)] = {"manager_id": manager_id, "cards": cards, "urgent": urgent}

    def cards_for_manager(self, manager_id, period=None, requester_id=None):
        out = []
        for (team, per), b in self._batches.items():
            if b["manager_id"] != manager_id:
                continue
            if period and per != period:
                continue
            for c in b["cards"]:
                if requester_id and requester_id in c.subjects:
                    continue  # 제약 6: 대상자 본인 노출 금지
                out.append(c)
        return out

    def urgent_for_manager(self, manager_id):
        out = []
        for b in self._batches.values():
            if b["manager_id"] == manager_id:
                out.extend(b["urgent"])
        return out

    def owner_of(self, card_id):
        return self._card_owner.get(card_id)

    def record_action(self, card_id, action, reason=None):
        self._actions.append({
            "card_id": card_id, "action": action, "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        })

    def actions(self):
        return list(self._actions)
