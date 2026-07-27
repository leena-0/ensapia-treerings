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
from .data_access import DataStore
from .llm_client import generate_json
from .priority import rank_team_goals
from .progress import goal_stage

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


def _postprocess_personal(content, store, goals, week_logs):
    """
    LLM의 역할을 '정리/대조/계수/추출'로 제한한다: 진척 상태·다음 주 우선순위처럼 사실을 세거나
    비교해서 나오는 값은 LLM이 판단하게 두지 않고 여기서 코드로 계산해 덮어쓴다.
    """
    valid_log_ids = {log["log_id"] for log in week_logs}

    def _clean_citations(items, key="citations"):
        for item in items:
            item[key] = [c for c in item.get(key, []) if c.get("log_id") in valid_log_ids]
        return items

    _clean_citations(content.get("goal_progress", []))
    # highlights/issues/one_on_one_agenda 는 log_ids(문자열 리스트)라 별도 검증
    for key in ("highlights", "issues", "one_on_one_agenda"):
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

    # 다음 주 우선순위(대조): 이번 주 로그가 하나도 없었던 목표를 리마인드. LLM 추정이 아니라
    # "이번 주 언급 있었는지 없었는지"를 코드가 대조해서 만든 목록.
    mentioned_goal_ids = {log["linked_goal_id"] for log in week_logs}
    content["next_week_priorities"] = [
        {"goal_id": g["goal_id"], "title": g["title"], "reminder": "이번 주 업무일지에 언급 없음 - 다음 주 확인 필요"}
        for g in goals if g["goal_id"] not in mentioned_goal_ids
    ]
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
            postprocess=lambda content, store=store, goals=goals, week_logs=week_logs:
                _postprocess_personal(content, store, goals, week_logs),
        )


def _sort_cards_by_kpi_priority(content, ranked):
    """
    카드 순서를 LLM이 정하게 두지 않고 코드가 확정한다: 카드가 언급한 goal_ids 중
    goal_priority_score(파이썬이 계산한 값)가 가장 높은 것을 그 카드의 순위로 쓴다.
    goal_ids 가 없거나 ranked_goals 에 없는 카드(정성 목표 등)는 맨 뒤로 보낸다.
    """
    priority_by_goal = {r["goal_id"]: r["goal_priority_score"] for r in ranked}

    def card_score(card):
        scores = [priority_by_goal[g] for g in card.get("goal_ids") or [] if g in priority_by_goal]
        return max(scores) if scores else -1

    cards = content.get("cards", [])
    content["cards"] = sorted(cards, key=card_score, reverse=True)
    return content


def run_coaching(store, teams, period_label, *, force, dry_run):
    for team in teams:
        ranked, qualitative = rank_team_goals(team, QUARTER, store)
        member_logs = {}
        for member_id in store.members_by_team[team]:
            logs = store.logs_by_member.get(member_id, [])
            member_logs[member_id] = logs[-5:]  # 개별/구조 이슈 교차 탐지를 위해 최근 5건 원문 그대로 제공

        prompt = prompts.build_coaching_cards_prompt(store, team, period_label, ranked, qualitative, member_logs)
        input_payload = {"ranked": ranked, "qualitative": qualitative, "logs": member_logs}
        _maybe_generate("coaching_card", team, period_label, prompt, input_payload, force=force, dry_run=dry_run,
                         postprocess=lambda content, ranked=ranked: _sort_cards_by_kpi_priority(content, ranked))


def _postprocess_evidence(content, store, goals_with_logs):
    """
    personal_weekly 의 _postprocess_personal 과 동일한 원칙: citation은 실제 log_id로 필터링하고,
    마일스톤(본인 보고) 상태는 LLM 서술이 아니라 코드가 확정해 주입한다.
    (evidence_package 는 기존에 postprocess가 전혀 없어 citation 필터링도 안 되고 있었던 기존 공백이었음
    -- 이번에 마일스톤을 추가하는 김에 같이 메운다.)
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
            postprocess=lambda content, store=store, goals_with_logs=goals_with_logs:
                _postprocess_evidence(content, store, goals_with_logs),
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
