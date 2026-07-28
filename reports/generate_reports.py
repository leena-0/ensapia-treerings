#!/usr/bin/env python3
"""
리포트 3종(개인 주간 리포트 / 리더 팀 코칭 카드 / 평가 근거 패키지) 생성 오케스트레이터.

캐싱 우선: 입력 데이터(input_hash)가 바뀌지 않았으면 API를 다시 부르지 않고 캐시를 그대로 사용한다.
무료 티어 rate limit 보호: 호출 간 최소 간격(llm_client.py) + 429 재시도 백오프 + (옵션) 로컬 LLM 폴백.

사용 예:
  python3 -m reports.generate_reports --report personal --members M01,M03 --dry-run
  python3 -m reports.generate_reports --report all --members M01,M02
  python3 -m reports.generate_reports --report all --force   # 캐시 무시하고 전원 재생성
"""

import argparse
import json
import sys
from datetime import date, timedelta, datetime, timezone

from . import cache_store, prompts
from .coaching_signals import detect_signals
from .collaboration import member_collaboration_summary
from .data_access import DataStore
from .llm_client import generate_json
from .priority import rank_team_goals
from .progress import goal_stage, subgoal_stage

QUARTER = "2026-Q2"
QUARTER_START = date(2026, 4, 1)
QUARTER_END = date(2026, 6, 30)


def week_bounds(week_idx):
    start = QUARTER_START + timedelta(days=7 * week_idx)
    end = min(start + timedelta(days=6), QUARTER_END)
    return start.isoformat(), end.isoformat()


def latest_week_bounds_for_member(store, member_id):
    """
    멤버별로 실제 마지막 업무일지 날짜 기준 rolling 5일 창을 계산한다.
    (전체 로그의 최신 날짜를 모든 멤버에 공통 적용하면, 한 멤버만 분기 밖 날짜로 새 로그가
    들어왔을 때 다른 멤버들의 "이번 주"가 빈 주로 계산되는 문제가 있어 멤버별로 분리함.)
    QUARTER_END 로 클램프하지 않는다 -- 실시간 수집된 로그는 목 데이터 분기 범위를 벗어날 수 있다.
    """
    logs = store.logs_by_member.get(member_id, [])
    if not logs:
        return week_bounds(((QUARTER_END - QUARTER_START).days) // 7)
    last_date = date.fromisoformat(logs[-1]["date"])
    start = last_date - timedelta(days=6)
    return start.isoformat(), last_date.isoformat()


def parse_json_safely(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.lower().startswith("json"):
                stripped = stripped[4:]
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
        return {"_raw_text": text, "_parse_error": True}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _maybe_generate(report_type, scope_id, period, prompt, input_payload, *, force, dry_run, postprocess=None):
    path = cache_store.cache_path(report_type, scope_id, period)
    hashed_payload = {"prompt_version": prompts.PROMPT_VERSION, "data": input_payload}
    input_hash = cache_store.compute_input_hash(hashed_payload)
    cached = cache_store.load(path)

    if not force and cache_store.is_cache_valid(cached, input_hash):
        print(f"[cache hit] {report_type} / {scope_id} / {period}")
        return cached["content"]

    if dry_run:
        print(f"\n===== DRY RUN: {report_type} / {scope_id} / {period} =====")
        print(prompt)
        return None

    print(f"[generating] {report_type} / {scope_id} / {period} ...")
    text, model_name, used_fallback = generate_json(prompt)
    content = parse_json_safely(text)
    if postprocess and not content.get("_parse_error"):
        content = postprocess(content)  # 순서/숫자 관련 후처리는 LLM이 아니라 코드가 확정 (프로젝트 공통 원칙)
    cache_store.save(path, input_hash=input_hash, model=model_name, used_fallback=used_fallback,
                      content=content, generated_at=_now_iso())
    return content


def _subgoal_next_week_priorities(store, goals, week_logs):
    """
    확인 요청(§5-1, "이 시스템의 관문") 대상 목록: 이번 주 로그가 하나도 안 걸린 하위목표(건물).
    goal 단위가 아니라 sub_goal 단위인 이유 -- §5-1의 "목표별 상태"가 애초에 "하위목표별
    완료/진행/미언급"으로 정의되어 있다. 아직 한 번도 손대지 않은(worked_days==0) 건물은
    "정체"가 아니라 그냥 "미착수"이므로 확인 대상에서 제외한다 (§4-3 "진행 정체" 판정 원칙과
    동일 -- 시작도 안 한 것에 막힘/보류를 묻는 건 무의미).
    """
    week_log_ids = {log["log_id"] for log in week_logs}
    out = []
    for g in goals:
        for sg in store.sub_goals(g["goal_id"]):
            if sg["status"] == "확정완료":
                continue
            evidence_ids = set(store.evidence_log_ids_by_subgoal.get(sg["sub_goal_id"], []))
            if evidence_ids & week_log_ids:
                continue  # 이번 주 언급됨
            p = subgoal_stage(store, sg["sub_goal_id"])
            if p["worked_days"] == 0:
                continue  # 아직 미착수 -- 확인 대상 아님
            existing = store.latest_checkin(sg["sub_goal_id"])
            out.append({
                "goal_id": g["goal_id"], "goal_title": g["title"],
                "sub_goal_id": sg["sub_goal_id"], "title": sg["title"],
                "reminder": "이번 주 업무일지에 언급 없음 - 상태를 선택해주세요",
                "current_status": existing["status"] if existing else None,
            })
    return out


def _postprocess_personal(content, store, goals, week_logs, week_start, week_end):
    """
    LLM의 역할을 '정리/대조/계수/추출'로 제한한다: 진척 상태·다음 주 우선순위처럼 사실을 세거나
    비교해서 나오는 값은 LLM이 판단하게 두지 않고 여기서 코드로 계산해 덮어쓴다.
    """
    # week_start/week_end 는 확인 요청 select 블록의 block_id(주차 식별)에 그대로 쓰이므로,
    # LLM이 프롬프트에 준 값을 그대로 안 돌려줬을 가능성에 대비해 코드가 확정값으로 덮어쓴다.
    content["week_start"], content["week_end"] = week_start, week_end

    valid_log_ids = {log["log_id"] for log in week_logs}

    def _clean_citations(items, key="citations"):
        for item in items:
            item[key] = [c for c in item.get(key, []) if c.get("log_id") in valid_log_ids]
        return items

    _clean_citations(content.get("goal_progress", []))
    # highlights/issues 는 log_ids(문자열 리스트)라 별도 검증
    for key in ("highlights", "issues"):
        for item in content.get(key, []):
            item["log_ids"] = [lid for lid in item.get("log_ids", []) if lid in valid_log_ids]

    # 목표별 진척(계수): worked_days/stage 는 LLM이 아니라 progress.py 가 로그 개수를 세어 계산
    stage_by_goal = {g["goal_id"]: goal_stage(store, g["goal_id"]) for g in goals}
    for gp in content.get("goal_progress", []):
        gp["progress"] = stage_by_goal.get(gp.get("goal_id"))

    # 정성 목표 마일스톤(본인 보고): LLM 서술은 못 믿으니 구조화된 상태/근거는 코드가 확정해 덮어씀
    milestones_by_goal = {g["goal_id"]: store.goal_milestones(g["goal_id"]) for g in goals}
    for gp in content.get("goal_progress", []):
        milestones = milestones_by_goal.get(gp.get("goal_id"))
        if milestones:
            gp["milestones"] = [
                {
                    "milestone_id": m["milestone_id"], "title": m["title"], "status": m["status"],
                    "self_reported_at": m["self_reported_at"], "reported_by": m["reported_by"],
                    "evidence_log_ids": [e["log_id"] for e in ev],
                }
                for m, ev in milestones
            ]

    # 확인 요청 = 다음 주 우선순위(대조): 이번 주 로그가 없었던 하위목표(건물)를 리마인드하고,
    # 본인이 Slack에서 진행중/대기/보류/막힘을 직접 고르게 한다(홈탭 크레인 표시 오버라이드 +
    # 코칭 카드 게이트로 이어짐). LLM 추정이 아니라 코드가 대조해서 만든 목록.
    content["next_week_priorities"] = _subgoal_next_week_priorities(store, goals, week_logs)
    return content


def run_personal(store, member_ids, week_start, week_end, *, force, dry_run):
    for member_id in member_ids:
        member = store.members_by_id[member_id]
        goals = store.member_goals(member_id)
        goals_with_links = [(g, store.goal_links(g["goal_id"])) for g in goals]
        week_logs = store.logs_in_range(member_id, week_start, week_end)

        prompt = prompts.build_personal_weekly_prompt(store, member, week_start, week_end, goals_with_links, week_logs)
        input_payload = {
            "member": member,
            "goals": [
                {
                    "goal": g,
                    "links": [{"weight": l["weight"], "direction": l["direction"], "kpi": k} for l, k in links],
                    "milestones": [{"milestone": m, "evidence": ev} for m, ev in store.goal_milestones(g["goal_id"])],
                }
                for g, links in goals_with_links
            ],
            "week_logs": week_logs,
        }
        _maybe_generate(
            "personal_weekly", member_id, f"{week_start}_{week_end}", prompt, input_payload,
            force=force, dry_run=dry_run,
            postprocess=lambda content, store=store, goals=goals, week_logs=week_logs,
                                week_start=week_start, week_end=week_end:
                _postprocess_personal(content, store, goals, week_logs, week_start, week_end),
        )


def _assemble_coaching_cards(content, detected, ranked, store):
    """
    최종 카드 목록을 '코드가 확정'한다 (개요 v10 §5-4, 프로젝트 공통 원칙).

    - 구조 이슈(의존 병목)는 coaching_signals 가 탐지·점수·인용 검증까지 마친 것을 권위로 삼고,
      LLM 산출물에서는 headline/prescription '서술'만 가져온다. 인원/근거/점수/확신도는 코드값 사용.
    - 개별 이슈는 LLM 산출물을 쓰되 evidence log_id 실존을 코드가 검증하고(없으면 폐기),
      병목에 이미 얽힌 인물과 중복되는 카드는 제외한다.
    - 선별: 구조 이슈(점수 = 영향 인원 × 근거 강도 × 지속 기간 내림차순) 먼저, 이어서 개별 이슈
      (KPI 우선순위 내림차순). 합쳐서 최대 5건 (§5-4-4 '개수 제한이 기능이다').
    """
    valid_log_ids = {l["log_id"] for logs in store.logs_by_member.values() for l in logs}
    priority_by_goal = {r["goal_id"]: r["goal_priority_score"] for r in ranked}

    # 1) 구조 이슈: 코드 신호(권위) + LLM 서술 결합
    narr_by_id = {c.get("signal_id"): c for c in content.get("structural_cards", [])}
    structural = []
    for s in detected:
        c = narr_by_id.get(s["signal_id"], {})
        parts = [x.strip() for x in (c.get("headline"), c.get("prescription")) if x and x.strip()]
        summary = " ".join(parts) or (
            f"{s['impact_count']}명이 같은 대상('{', '.join(s['shared_terms'][:2])}')을 "
            f"{s['duration_days']}일째 기다리는 의존 병목입니다. 리더가 승인을 밀어주면 해소됩니다."
        )
        structural.append({
            "card_id": s["signal_id"],
            "category": "구조 이슈",
            "member_ids": s["member_ids"],
            "goal_ids": [],
            "summary": summary,
            "evidence": [{"log_id": e["log_id"], "date": e["date"], "member_id": e["member_id"]}
                         for e in s["evidence"] if e["log_id"] in valid_log_ids],
            "confidence": s["confidence"],
            "signal_type": s["type"],
            "score": s["score"],
        })
    structural.sort(key=lambda c: c["score"], reverse=True)

    # 2) 개별 이슈: LLM 산출 + evidence 실존 검증 + 병목 인물 중복 제외 + KPI 순 정렬
    bottleneck_members = {m for s in detected for m in s["member_ids"]}
    individual = []
    for c in content.get("individual_cards", []):
        ev = [e for e in c.get("evidence", []) if e.get("log_id") in valid_log_ids]
        if not ev:
            continue  # 근거 없는 카드는 만들지 않는다 (§5-4-4)
        members = c.get("member_ids", [])
        if members and set(members) <= bottleneck_members:
            continue  # 이미 병목 카드로 다뤄진 인물만 담은 개별 카드는 중복이므로 제외
        individual.append({
            "card_id": c.get("card_id", ""),
            "category": "개별 이슈",
            "member_ids": members,
            "goal_ids": c.get("goal_ids", []),
            "summary": c.get("summary", ""),
            "evidence": ev,
        })

    def kpi_score(card):
        scores = [priority_by_goal[g] for g in card.get("goal_ids") or [] if g in priority_by_goal]
        return max(scores) if scores else -1
    individual.sort(key=kpi_score, reverse=True)

    # 3) 구조 먼저 + 개별, 최대 5건
    content["cards"] = (structural + individual)[:5]
    content.setdefault("qualitative_goals_checkin", [])
    return content


def run_coaching(store, teams, period_label, *, force, dry_run):
    """
    merge 메모(2026-07-28): 원래 이 함수엔 "확인 요청에서 본인이 막힘으로 표시한 멤버만
    member_logs 후보에 넣는다"는 게이트가 있었다(§5-1 "막힘만 코칭 카드로 전달" 원칙을,
    코드 탐지기가 없던 시절 자기신고로 대체 구현한 것). origin/main이 그 사이 진짜 코드
    탐지기(coaching_signals.detect_signals, 아래)를 만들었는데, 이건 자기신고 없이도
    팀 전체 로그를 교차 읽어(§3-1 "교차 읽기") 여러 사람이 같은 대상을 기다리는 걸 구조적으로
    찾아낸다 -- 오히려 자기신고 게이트를 씌우면 "본인은 막힘이라 인지 못했지만 실제로는
    구조적 병목인" 케이스를 놓치게 되어 그 원칙에 역행한다. 그래서 게이트는 제거하고 main의
    원래 방식(팀원 전원의 최근 로그를 개별 이슈 LLM 마이닝용으로 무조건 제공)을 유지했다.
    `subgoal_weekly_checkin`(확인 요청) 데이터는 대신 `coaching/data_adapter.py`를 통해
    `coaching/` 파이프라인(LangGraph, detect_blocked_escalation)의 자기신고 입력으로 연결된다.
    """
    for team in teams:
        ranked, qualitative = rank_team_goals(team, QUARTER, store)
        member_logs = {}
        for member_id in store.members_by_team[team]:
            logs = store.logs_by_member.get(member_id, [])
            member_logs[member_id] = logs[-5:]  # 개별 이슈 탐지를 위해 최근 5건 원문 그대로 제공

        # 구조 병목은 LLM이 아니라 코드가 탐지·선별·인용 검증한다 (전체 로그를 가로질러 읽음)
        detected = detect_signals(store, team)
        for i, s in enumerate(detected, 1):
            s["signal_id"] = f"S{i}"

        prompt = prompts.build_coaching_cards_prompt(
            store, team, period_label, ranked, qualitative, member_logs, detected)
        input_payload = {"ranked": ranked, "qualitative": qualitative,
                         "logs": member_logs, "signals": detected}
        _maybe_generate("coaching_card", team, period_label, prompt, input_payload, force=force, dry_run=dry_run,
                         postprocess=lambda content, detected=detected, ranked=ranked:
                             _assemble_coaching_cards(content, detected, ranked, store))


def _subgoal_summary_for_goal(store, goal_id):
    """평가 근거 패키지의 "목표별 결과: 완료·미완 항목"(§5-4)용 사람이 읽을 상태 라벨.
    확정완료/본인완료(sub_goals.status)는 그대로 쓰고, 나머지는 subgoal_stage()가 실제
    로그 일수를 세어 계산한 값(진행중/미착수)으로 -- 여기서도 AI는 관여하지 않는다."""
    summary = []
    for sg in store.sub_goals(goal_id):
        if sg["status"] == "확정완료":
            label = "완료(확정)"
        elif sg["status"] == "본인완료":
            label = "완료(본인 보고, 리더 확인 대기)"
        else:
            p = subgoal_stage(store, sg["sub_goal_id"])
            label = "진행중" if p["stage"] > 0 else "미착수"
        p = subgoal_stage(store, sg["sub_goal_id"])
        summary.append({
            "sub_goal_id": sg["sub_goal_id"], "title": sg["title"], "status": label,
            "worked_days": p["worked_days"], "confirmed_at": sg.get("confirmed_at"),
        })
    return summary


def _postprocess_evidence(content, store, goals_with_logs, member_id, quarter_start, quarter_end):
    """
    personal_weekly 의 _postprocess_personal 과 동일한 원칙: citation은 실제 log_id로 필터링하고,
    마일스톤(본인 보고) 상태는 LLM 서술이 아니라 코드가 확정해 주입한다.
    (evidence_package 는 기존에 postprocess가 전혀 없어 citation 필터링도 안 되고 있었던 기존 공백이었음
    -- 이번에 마일스톤을 추가하는 김에 같이 메운다.)

    §5-4 "목표별 결과(완료·미완 항목과 근거)"/"협업 기록"을 코드가 계산해 덮어쓴다 -- 둘 다 사실을
    세거나 대조해서 나오는 값이라 LLM 서술에 맡기지 않는다는 프로젝트 공통 원칙과 같다.
    """
    valid_log_ids = {log["log_id"] for _, _, logs in goals_with_logs for log in logs}
    milestones_by_goal = {g["goal_id"]: store.goal_milestones(g["goal_id"]) for g, _, _ in goals_with_logs}

    for ge in content.get("goal_evidence", []):
        ge["citations"] = [c for c in ge.get("citations", []) if c.get("log_id") in valid_log_ids]
        milestones = milestones_by_goal.get(ge.get("goal_id"))
        if milestones:
            ge["milestones"] = [
                {
                    "milestone_id": m["milestone_id"], "title": m["title"], "status": m["status"],
                    "self_reported_at": m["self_reported_at"], "reported_by": m["reported_by"],
                    "evidence_log_ids": [e["log_id"] for e in ev],
                }
                for m, ev in milestones
            ]
        sub_goals = _subgoal_summary_for_goal(store, ge.get("goal_id"))
        if sub_goals:
            ge["sub_goals"] = sub_goals

    content["collaboration"] = member_collaboration_summary(member_id, store, quarter_start, quarter_end)
    return content


def run_evidence(store, member_ids, *, force, dry_run):
    for member_id in member_ids:
        member = store.members_by_id[member_id]
        goals = store.member_goals(member_id)
        goals_with_logs = []
        for g in goals:
            links = store.goal_links(g["goal_id"])
            logs = [l for l in store.logs_by_member.get(member_id, []) if l["linked_goal_id"] == g["goal_id"]]
            goals_with_logs.append((g, links, logs))

        prompt = prompts.build_evidence_package_prompt(store, member, QUARTER, goals_with_logs)
        input_payload = {
            "member": member,
            "goals": [
                {
                    "goal": g,
                    "links": [{"weight": l["weight"], "direction": l["direction"], "kpi": k} for l, k in links],
                    "logs": logs,
                    "milestones": [{"milestone": m, "evidence": ev} for m, ev in store.goal_milestones(g["goal_id"])],
                }
                for g, links, logs in goals_with_logs
            ],
        }
        _maybe_generate(
            "evidence_package", member_id, QUARTER, prompt, input_payload, force=force, dry_run=dry_run,
            postprocess=lambda content, store=store, goals_with_logs=goals_with_logs, member_id=member_id:
                _postprocess_evidence(content, store, goals_with_logs, member_id, QUARTER_START, QUARTER_END),
        )


def main():
    parser = argparse.ArgumentParser(description="ENSAPIA 리포트 3종 생성기")
    parser.add_argument("--report", choices=["personal", "coaching", "evidence", "all"], default="all")
    parser.add_argument("--members", help="쉼표로 구분된 member_id 목록 (예: M01,M02). 미지정 시 전원")
    parser.add_argument("--teams", help="쉼표로 구분된 team 목록. 미지정 시 전체 팀")
    parser.add_argument("--week", choices=["latest", "all"], default="latest", help="개인 주간 리포트 대상 주차")
    parser.add_argument("--force", action="store_true", help="캐시 무시하고 강제 재생성")
    parser.add_argument("--dry-run", action="store_true", help="실제 API 호출 없이 프롬프트만 출력")
    args = parser.parse_args()

    store = DataStore()

    member_ids = args.members.split(",") if args.members else [m["member_id"] for m in store.members]
    teams = args.teams.split(",") if args.teams else list(store.members_by_team.keys())

    unknown_members = [m for m in member_ids if m not in store.members_by_id]
    if unknown_members:
        sys.exit(f"알 수 없는 member_id: {unknown_members}")

    if args.report in ("personal", "all"):
        if args.week == "latest":
            for member_id in member_ids:
                week_start, week_end = latest_week_bounds_for_member(store, member_id)
                run_personal(store, [member_id], week_start, week_end, force=args.force, dry_run=args.dry_run)
        else:
            n_weeks = (QUARTER_END - QUARTER_START).days // 7 + 1
            for week_idx in range(n_weeks):
                week_start, week_end = week_bounds(week_idx)
                run_personal(store, member_ids, week_start, week_end, force=args.force, dry_run=args.dry_run)

    if args.report in ("coaching", "all"):
        run_coaching(store, teams, QUARTER, force=args.force, dry_run=args.dry_run)

    if args.report in ("evidence", "all"):
        run_evidence(store, member_ids, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
