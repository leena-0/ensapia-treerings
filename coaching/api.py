# -*- coding: utf-8 -*-
"""
FastAPI 엔드포인트 (구현명세_코칭카드 §9).

  POST /coaching-cards/generate         팀·기간 카드 생성(배치, 격주 실행)
  GET  /coaching-cards?manager_id=&period=   해당 매니저의 카드
  POST /coaching-cards/{card_id}/action      채택/기각 기록(채택률 수집)
  GET  /urgent-alerts?manager_id=            긴급 사안(즉시 발송분)

## 인증/인가 (fail-closed)
모든 엔드포인트는 요청자 신원을 **필수**로 요구한다(`get_current_user` 의존성). 신원이 없거나
권한이 없으면 거부한다(제약 6: 카드 대상자 본인은 자기 카드를 볼 수 없다).

⚠ 현재 신원 소스는 `X-User-Id` 헤더다. 이는 **데모용 스텁**이며, 실서비스에서는 반드시
검증된 세션/JWT(예: `Depends(verify_bearer_token)`)로 교체해야 한다. 클라이언트가 보낸 헤더를
그대로 신뢰하면 안 된다. create_app(get_current_user=...) 로 실제 인증 의존성을 주입할 수 있다.
"""
from __future__ import annotations

from datetime import date
from typing import Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from .graph import run_coaching
from .store import CardStore


class GenerateRequest(BaseModel):
    team_id: str
    period_start: date
    period_end: date


class ActionRequest(BaseModel):
    action: str                 # adopt / dismiss
    reason: Optional[str] = None


def _header_identity(x_user_id: Optional[str] = Header(default=None)) -> str:
    """데모용 신원 추출: X-User-Id 헤더 필수(없으면 401). 실서비스에선 토큰 검증으로 교체."""
    if not x_user_id:
        raise HTTPException(status_code=401, detail="인증 필요: 신원 미확인 (X-User-Id)")
    return x_user_id


def _default_authorize_generate(user_id: str, team_id: str) -> bool:
    """카드 생성 권한: 해당 팀의 관리 책임자(1차평가자)만 허용."""
    from reports.data_access import DataStore

    from .data_adapter import build_team_context
    store = DataStore()
    tc = build_team_context(store, team_id, date(2000, 1, 1), date(2000, 1, 1))
    return bool(tc.manager_id) and user_id == tc.manager_id


def create_app(runner: Callable = run_coaching, store: CardStore | None = None,
               get_current_user: Callable = _header_identity,
               authorize_generate: Callable = _default_authorize_generate) -> FastAPI:
    """runner/store/인증 의존성을 주입 가능하게 해 테스트·실배포에서 각각 대체할 수 있다."""
    app = FastAPI(title="ENSAPIA 코칭 카드 API")
    app.state.store = store or CardStore()
    app.state.runner = runner
    app.state.authorize_generate = authorize_generate

    def _period(ps: date, pe: date) -> str:
        return f"{ps}_{pe}"

    @app.post("/coaching-cards/generate")
    def generate(req: GenerateRequest, user: str = Depends(get_current_user)):
        if not app.state.authorize_generate(user, req.team_id):
            raise HTTPException(status_code=403, detail="이 팀의 카드를 생성할 권한이 없습니다.")
        result = app.state.runner(req.team_id, req.period_start, req.period_end)
        tc = result["team_context"]
        period = _period(req.period_start, req.period_end)
        app.state.store.save(req.team_id, period, tc.manager_id,
                             result["cards"], result["urgent_alerts"])
        return {
            "team_id": req.team_id, "period": period, "manager_id": tc.manager_id,
            "card_count": len(result["cards"]), "urgent_count": len(result["urgent_alerts"]),
        }

    @app.get("/coaching-cards")
    def list_cards(manager_id: str = Query(...), period: Optional[str] = None,
                   user: str = Depends(get_current_user)):
        # 제약 6: 수신 매니저 본인만 조회 가능 (fail-closed)
        if user != manager_id:
            raise HTTPException(status_code=403, detail="본인의 카드 목록만 조회할 수 있습니다.")
        return {"cards": app.state.store.cards_for_manager(manager_id, period, requester_id=user)}

    @app.post("/coaching-cards/{card_id}/action")
    def act(card_id: str, req: ActionRequest, user: str = Depends(get_current_user)):
        owner = app.state.store.owner_of(card_id)
        if owner is None:
            raise HTTPException(status_code=404, detail="카드를 찾을 수 없습니다.")
        if user != owner:
            raise HTTPException(status_code=403, detail="해당 카드의 수신 매니저만 조치할 수 있습니다.")
        if req.action not in ("adopt", "dismiss"):
            raise HTTPException(status_code=400, detail="action 은 adopt 또는 dismiss 여야 합니다.")
        app.state.store.record_action(card_id, req.action, req.reason)
        return {"ok": True, "card_id": card_id, "action": req.action}

    @app.get("/urgent-alerts")
    def urgent(manager_id: str = Query(...), user: str = Depends(get_current_user)):
        if user != manager_id:
            raise HTTPException(status_code=403, detail="본인의 긴급 알림만 조회할 수 있습니다.")
        return {"urgent_alerts": app.state.store.urgent_for_manager(manager_id)}

    return app


app = create_app()
