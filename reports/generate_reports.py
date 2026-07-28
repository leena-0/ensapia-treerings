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
    """
    §5-1 원칙: "막힘"을 선택한 것만 코칭 카드로 전달된다 -- 확인 요청에서 아무도 막힘을 고르지
    않은 팀원은 애초에 이 팀의 member_logs 후보에서 빠진다(로그를 통째로 넘겨 LLM이 알아서
    병목을 찾게 하던 이전 방식은 이 게이트가 없었다). 막힘으로 표시된 건물에 실제로 연결된
    로그가 아직 없으면(체크인만 하고 근거 로그가 안 쌓인 경우) 최근 로그로 보수적으로 대체한다.
    """
    for team in teams:
        ranked, qualitative = rank_team_goals(team, QUARTER, store)
        member_logs = {}
        for member_id in store.members_by_team[team]:
            blocked_subgoal_ids = store.member_blocked_subgoal_ids(member_id)
            if not blocked_subgoal_ids:
                continue  # 본인이 막힘으로 표시한 게 없으면 카드 후보에서 제외

            blocked_log_ids = set()
            for sg_id in blocked_subgoal_ids:
                blocked_log_ids.update(store.evidence_log_ids_by_subgoal.get(sg_id, []))
            logs = [store.logs_by_id[lid] for lid in blocked_log_ids if lid in store.logs_by_id]
            member_logs[member_id] = logs or store.logs_by_member.get(member_id, [])[-5:]

        if not member_logs:
            print(f"[skip] {team}: 이번 기간 '막힘'으로 표시된 하위목표가 없어 코칭 카드 생성 대상 없음")
            continue

        prompt = prompts.build_coaching_cards_prompt(store, team, period_label, ranked, qualitative, member_logs)
        input_payload = {"ranked": ranked, "qualitative": qualitative, "logs": member_logs}
        _maybe_generate("coaching_card", team, period_label, prompt, input_payload, force=force, dry_run=dry_run,
                         postprocess=lambda content, ranked=ranked: _sort_cards_by_kpi_priority(content, ranked))


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
