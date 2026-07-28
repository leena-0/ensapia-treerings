# -*- coding: utf-8 -*-
"""
리포트 3종의 Gemini 프롬프트 빌더.

공통 원칙:
- 출력은 순수 JSON만 (마크다운 코드블록/설명 문구 금지) -- response_mime_type=application/json 과 병행.
- LLM은 절대 새로운 숫자를 계산하지 않는다. KPI 현재/목표값, goal_priority_score 등은
  전부 파이썬에서 계산해 프롬프트에 이미 확정된 값으로 넘기고, LLM은 "서술"만 담당한다.
- 업무일지에 없는 사실을 지어내지 않는다 (근거 없는 성과/이슈 생성 금지).

PROMPT_VERSION 을 캐시 input_hash 에 포함시켜야 한다: 원본 데이터(목표/KPI/로그)가 그대로여도
프롬프트 템플릿 자체가 바뀌면 캐시가 무효화되어야 하기 때문이다. 이 프롬프트 문구를 수정할 때마다
아래 PROMPT_VERSION 을 올릴 것.
"""

PROMPT_VERSION = "2026-07-28.1"  # one_on_one_agenda 필드 제거 (스펙 미정의 + AI가 "의사결정 필요 안건"을 판단하는 것이 4-2절 원칙과 충돌해 삭제)

SYSTEM_PREAMBLE = """당신은 엔서피아(ENSAPIA)의 인사 평가를 지원하는 어시스턴트입니다.
엔서피아는 아바타/디지털 월드 서비스 기업으로 '리브리 아일랜드' 등을 운영합니다.
아래에 주어지는 목표(goal), KPI, Slack 업무일지 데이터만 근거로 삼아 리포트를 작성하세요.

반드시 지킬 것:
1. 주어진 데이터에 없는 수치나 사실을 지어내지 마세요.
2. KPI 현재값/목표값/우선순위 점수는 이미 계산되어 주어집니다. 재계산하거나 다른 값으로 바꾸지 마세요.
3. 출력은 지정된 JSON 스키마만 반환하세요. 코드블록(```), 설명 문구 없이 순수 JSON 객체만 출력하세요.
4. 정성 목표는 KPI가 없는 것이 정상입니다(예외로 허용됨) -- 업무일지 내용으로만 진척을 서술하세요.
5. 당신의 역할은 "정리, 대조, 계수, 추출"로 제한됩니다. 진척률이나 진행 상태가 "좋다/나쁘다/지연됐다/잘 되고 있다"는
   식의 판단은 절대 하지 마세요 (그런 판단은 시스템이 실제 로그 개수를 세어 별도로 계산합니다). 로그에 실제로
   쓰인 내용을 인용/정리하고, 주어진 숫자를 대조해서 보여주는 것까지만 하세요.
6. 정성 목표의 마일스톤 상태는 목표 소유자 본인이 직접 self-report 한 값이며 리더가 검증한 사실이 아닙니다.
   이를 언급할 때는 반드시 "본인 보고"라고 명시하고, "검증됨"/"확정됨"처럼 쓰거나 그 달성 여부를 재판단하지 마세요.
"""

PERSONAL_FEWSHOT = """## Few-shot 예시 (형식 참고용, 실제 데이터 아님)

입력 예시:
- 목표: "신규 유저 온보딩 개선 실험" (정량)
- 연결 KPI: D7_Retention(weight 0.5, 현재 9.5%, 목표 12%, direction +), DAU(weight 0.5, 현재 86,400명, 목표 112,000명, direction +)
- 이번 주 업무일지:
  - log_id=L0035 [2026-04-20] "온보딩 개선 가설: 첫 세션 튜토리얼 단순화 → D7 리텐션 및 DAU 개선 기대. 기존 온보딩 퍼널 이탈 구간 분석 착수."
  - log_id=L0051 [2026-04-22] "단순화된 튜토리얼 A/B 테스트 적용 중. 1주차 데이터 기준 튜토리얼 완료율 상승 확인."

출력 예시 (판단/평가 문구 없이, 로그 내용을 그대로 정리·인용만 함):
{
  "goal_id": "G05",
  "title": "신규 유저 온보딩 개선 실험",
  "kpi_causal_summary": "온보딩 튜토리얼 단순화 → D7 리텐션(현재 9.5% → 목표 12%) 및 DAU(현재 86,400명 → 목표 112,000명) 개선 가설로 A/B 테스트 진행. 1주차 데이터 기준 튜토리얼 완료율 상승이 기록됨.",
  "citations": [
    {"log_id": "L0035", "date": "2026-04-20", "excerpt": "첫 세션 튜토리얼 단순화 → D7 리텐션 및 DAU 개선 기대"},
    {"log_id": "L0051", "date": "2026-04-22", "excerpt": "1주차 데이터 기준 튜토리얼 완료율 상승 확인"}
  ]
}

(주의: "성공적으로 개선했다", "순조롭게 진행 중이다", "지연되고 있다" 같은 판단 문구는 쓰지 않았음 -- 로그에
쓰인 사실과 주어진 KPI 숫자만 정리·대조했음)
"""


def _milestone_lines(milestones_with_evidence):
    lines = ["  마일스톤 (본인 보고, 검증 아님):"]
    for m, evidence in milestones_with_evidence:
        ev_ids = ", ".join(e["log_id"] for e in evidence) or "없음"
        reported = f" (본인 보고 {m['self_reported_at'][:10]})" if m["self_reported_at"] else ""
        lines.append(f"    - [{m['status']}] {m['title']}{reported} · 근거: {ev_ids}")
    return lines


def _goal_block(goal, links_with_kpi, milestones_with_evidence=None):
    lines = [f"- goal_id: {goal['goal_id']}", f"  title: {goal['title']}", f"  type: {goal['type']}"]
    if links_with_kpi:
        lines.append("  연결 KPI:")
        for link, kpi in links_with_kpi:
            lines.append(
                f"    - {kpi['name']} (weight {link['weight']}, direction {link['direction']}, "
                f"현재 {kpi['current_value']}{kpi['unit']} → 목표 {kpi['target']}{kpi['unit']})"
            )
    else:
        lines.append("  연결 KPI: 없음 (정성 목표)")
        if milestones_with_evidence:
            lines.extend(_milestone_lines(milestones_with_evidence))
    return "\n".join(lines)


def build_personal_weekly_prompt(store, member, week_start, week_end, goals_with_links, week_logs):
    goal_blocks = "\n".join(
        _goal_block(g, links, store.goal_milestones(g["goal_id"])) for g, links in goals_with_links
    )
    log_lines = "\n".join(
        f"  - log_id={log['log_id']} [{log['date']}] {log['text']}" for log in week_logs
    ) or "  (이번 주 업무일지 없음)"

    return f"""{SYSTEM_PREAMBLE}
{PERSONAL_FEWSHOT}

## 실제 입력 데이터

멤버: {member['name']} ({member['team']}, {member['role']})
주차: {week_start} ~ {week_end}

목표 목록:
{goal_blocks}

이번 주 업무일지:
{log_lines}

## 출력 형식 (JSON 객체만 출력)
{{
  "member_id": "{member['member_id']}",
  "week_start": "{week_start}",
  "week_end": "{week_end}",
  "highlights": [{{"text": "이번 주 로그에 실제로 쓰인 성과/작업 문장 정리", "log_ids": ["L0035"]}}],
  "goal_progress": [
    {{
      "goal_id": "...", "title": "...",
      "kpi_causal_summary": "정량 목표는 KPI 수치 대조 포함, 정성 목표는 로그 내용 정리 + 마일스톤 상태(본인 보고)를 자연스럽게 언급. 판단/평가 문구 금지",
      "citations": [{{"log_id": "...", "date": "...", "excerpt": "로그 원문에서 관련 부분 인용"}}]
    }}
  ],
  "issues": [{{"text": "업무일지에 실제로 쓰인 이슈/도움 요청 문장", "log_ids": ["L0042"]}}]
}}
(issues 에 해당 사항이 없으면 빈 배열 `[]`)

작성 지침:
- goal_progress 는 위 "목표 목록"의 모든 goal 을 빠짐없이 포함하세요 (이번 주 로그가 없는 goal 도 포함하되 citations 는 빈 배열).
- citations 의 log_id/date 는 반드시 위 "이번 주 업무일지"에 실제로 주어진 값만 쓰세요 (지어내지 마세요).
- highlights/issues 는 위 "이번 주 업무일지"에 실제로 언급된 내용에서만 도출하고, 각 항목에 근거 log_id를 포함하세요.
- "성공적으로", "순조롭게", "잘 진행 중", "지연되고 있다" 같은 진행 상태에 대한 평가/판단 표현은 쓰지 마세요. 로그 내용을 정리하고 주어진 KPI 숫자와 대조하는 것까지만 하세요.
- 다음 주 우선순위는 이 리포트에 포함하지 마세요 (시스템이 별도로 계산해 붙입니다).
"""


COACHING_FORMULA_NOTE = """[우선순위 산식 - 하드코딩 아님, 운영자 입력 파라미터 기반. 카드 선별에 참고만 하고 카드 문구에 숫자를 그대로 나열하지 마세요]
normalized_gap = KPI 달성 갭을 0~1로 정규화 (direction '+': (target-current)/target, direction '-': (current-target)/target)
kpi_priority = strategy_weights.priority_score(운영자가 분기마다 입력) x normalized_gap
goal_priority = sum( goal_kpi_link.weight_i x kpi_priority_i )  -- goal이 연결한 모든 KPI에 대해 가중합
아래 ranked_goals 는 이미 이 산식으로 계산되어 내림차순 정렬된 결과입니다. 이 순서/숫자를 재계산하지 말고, "어떤 이슈가 카드로 뽑힐 만큼 중요한지" 판단하는 근거로만 쓰세요.
"""

CARD_FEWSHOT = """## Few-shot 예시 (형식 참고용, 실제 데이터 아님)

출력 예시:
{
  "cards": [
    {
      "card_id": "C1",
      "category": "개별 이슈",
      "member_ids": ["M03"],
      "goal_ids": ["G05"],
      "summary": "김도윤님이 튜토리얼 크래시 이슈로 온보딩 실험이 4일째 정체 중입니다. 앱개발팀과의 진행 상황을 1on1에서 확인해보세요.",
      "evidence": [{"log_id": "L0002", "date": "2026-04-01", "member_id": "M03"}]
    },
    {
      "card_id": "C2",
      "category": "구조 이슈",
      "member_ids": ["M01", "M04"],
      "goal_ids": ["G01", "G06"],
      "summary": "정지원님과 이서연님 모두 예산/리소스 승인 지연을 겪고 있습니다. 개인 일정 문제가 아니라 승인 프로세스의 구조적 병목일 수 있어 팀 차원 논의를 제안합니다.",
      "evidence": [{"log_id": "L0005", "date": "2026-04-02", "member_id": "M01"}, {"log_id": "L0031", "date": "2026-04-09", "member_id": "M04"}]
    }
  ]
}
"""


def _ranked_goal_block(row, rank):
    kpi_lines = "; ".join(
        f"{d['kpi_name']}(gap {d['normalized_gap']}, strategy {d['priority_score']}, weight {d['weight']})"
        for d in row["kpi_details"]
    )
    return (f"{rank}. goal_priority={row['goal_priority_score']} | {row['goal_id']} | "
            f"{row['member_id']} | {row['title']} | KPI 근거: {kpi_lines}")


def build_coaching_cards_prompt(store, team, period, ranked_goals, qualitative_goals, member_logs):
    ranked_block = "\n".join(_ranked_goal_block(r, i + 1) for i, r in enumerate(ranked_goals)) or "(정량 목표 없음)"
    qual_block = "\n".join(f"- {q['goal_id']} | {q['member_id']} | {q['title']}" for q in qualitative_goals) or "(정성 목표 없음)"

    logs_blocks = []
    for member_id, logs in member_logs.items():
        member = store.members_by_id[member_id]
        lines = "\n".join(f"    - log_id={l['log_id']} [{l['date']}] {l['text']}" for l in logs) or "    (최근 로그 없음)"
        logs_blocks.append(f"- {member['name']}({member_id}):\n{lines}")
    logs_block = "\n".join(logs_blocks)

    return f"""{SYSTEM_PREAMBLE}
{COACHING_FORMULA_NOTE}
{CARD_FEWSHOT}

## 실제 입력 데이터

팀: {team}
기간: {period}

정량 목표 우선순위 랭킹 (참고용, 재계산 금지):
{ranked_block}

정성 목표 (우선순위 계산 대상 아님):
{qual_block}

멤버별 최근 업무일지 (여기서 이슈/병목을 찾으세요):
{logs_block}

## 출력 형식 (JSON 객체만 출력)
{{
  "team": "{team}",
  "period": "{period}",
  "cards": [
    {{
      "card_id": "C1",
      "category": "개별 이슈 또는 구조 이슈",
      "member_ids": ["관련 member_id 목록 (구조 이슈면 2명 이상)"],
      "goal_ids": ["관련 goal_id, 없으면 빈 배열"],
      "summary": "1~2문장. 누가/무엇이/왜 중요한지 자연스러운 문장으로. 리더가 바로 판단할 수 있게 구체적으로",
      "evidence": [{{"log_id": "...", "date": "...", "member_id": "..."}}]
    }}
  ],
  "qualitative_goals_checkin": [{{"goal_id": "...", "member_id": "...", "title": "...", "note": "정기 체크인 시 확인할 포인트"}}]
}}

작성 지침:
- cards 는 최대 3~5개만 선별하세요 (영향 범위와 근거 강도 기준). 팀 전체에서 감지되는 신호를 전부 나열하면 리더가 다시 우선순위를 판단해야 하니, 그 판단을 대신 해주는 것이 이 기능의 핵심입니다.
- "구조 이슈"는 반드시 2명 이상이 같은 종류의 병목(같은 팀/프로세스/승인 등)을 겪고 있다는 근거가 로그에 실제로 있을 때만 만드세요. 근거 없이 만들지 마세요.
- evidence 의 log_id/date/member_id 는 위에 주어진 실제 값만 사용하세요 (지어내지 마세요). evidence 없는 카드는 만들지 마세요.
- summary 에 goal_priority 같은 원시 점수를 그대로 나열하지 말고, 왜 중요한지 자연어로 녹여서 설명하세요.
- qualitative_goals_checkin 은 "정성 목표" 목록을 그대로 반영하세요.
"""


def build_wish_match_prompt(stuck_member, candidate_log, other_issue_logs):
    others_block = "\n".join(
        f"- log_id={l['log_id']} | {l['member_name']}({l['member_id']}) | [{l['date']}] {l['text']}"
        for l in other_issue_logs
    ) or "(참고할 다른 멤버의 이슈 로그 없음)"

    return f"""{SYSTEM_PREAMBLE}

## 과제
아래 "확인 대상 로그"가 실제로 막힌 상황(도움이 필요한 상황)을 설명하는지 판단하고,
그렇다면 "다른 멤버들의 이슈 로그" 중에서 비슷한 문제를 **이미 해결한** 사례가 있는지 찾으세요.
(단순히 같은 문제를 겪고 있다고 말한 것은 해결 사례가 아닙니다. 실제로 풀었다는 뉘앙스가 있어야 합니다.)

## 확인 대상 로그
{stuck_member['name']}({stuck_member['member_id']}) | [{candidate_log['date']}] {candidate_log['text']}

## 다른 멤버들의 이슈 로그 (여기서만 골라야 함, 목록에 없는 log_id/member_id 를 지어내지 마세요)
{others_block}

## 출력 형식 (JSON 객체만 출력)
{{
  "is_stuck": true 또는 false,
  "problem_summary": "확인 대상 로그가 막힌 상황이라면, 그 문제를 1문장으로 요약. 아니면 빈 문자열",
  "match_found": true 또는 false,
  "helper_member_id": "매칭된 멤버의 member_id, 없으면 빈 문자열",
  "helper_log_id": "매칭된 로그의 log_id (위 목록에 실제 존재하는 것만), 없으면 빈 문자열",
  "helper_topic": "매칭된 로그가 다룬 주제를 2~5단어의 짧은 구로 일반화 (예: '모델 경량화', 'PG사 정산 연동'). 원문 문장을 그대로 인용하지 마세요. 없으면 빈 문자열",
  "reason": "왜 이 로그가 비슷한 문제를 해결한 사례라고 판단했는지, 없으면 빈 문자열"
}}

작성 지침:
- is_stuck=false 이면 나머지 필드는 전부 빈 값으로 두세요.
- helper_log_id 는 반드시 위 "다른 멤버들의 이슈 로그" 목록에 실제로 있는 log_id만 쓰세요.
- helper_topic 은 요청자에게 그대로 노출되는 값입니다 -- 해결 방법의 상세나 원문 문장이 아니라
  "무슨 주제였는지"만 짧게 일반화하세요 (예: 원문이 "메모리 캐싱 로직 수정으로 크래시 해결"이면
  helper_topic 은 "크래시 이슈 해결" 정도로만 — 구체적 수정 방법을 담지 마세요).
- 확신이 없으면 match_found=false 로 두세요 (억지로 매칭하지 마세요).
"""


def build_evidence_package_prompt(store, member, quarter, goals_with_logs):
    blocks = []
    for goal, links, logs in goals_with_logs:
        kpi_info = ""
        if links:
            kpi_info = " / ".join(
                f"{kpi['name']}: 현재 {kpi['current_value']}{kpi['unit']} → 목표 {kpi['target']}{kpi['unit']} (direction {link['direction']}, weight {link['weight']})"
                for link, kpi in links
            )
        log_lines = "\n".join(f"    - log_id={log['log_id']} [{log['date']}] {log['text']}" for log in logs) or "    (분기 중 업무일지 없음)"
        block = (
            f"- goal_id: {goal['goal_id']} | title: {goal['title']} | type: {goal['type']}\n"
            f"  KPI: {kpi_info or '없음 (정성 목표)'}\n"
        )
        if not links:
            milestones = store.goal_milestones(goal["goal_id"])
            if milestones:
                block += "\n".join(_milestone_lines(milestones)) + "\n"
        block += f"  분기 누적 업무일지:\n{log_lines}"
        blocks.append(block)
    goals_block = "\n".join(blocks)

    return f"""{SYSTEM_PREAMBLE}

## 실제 입력 데이터

멤버: {member['name']} ({member['team']}, {member['role']})
분기: {quarter}

목표별 분기 누적 데이터:
{goals_block}

## 출력 형식 (JSON 객체만 출력)
{{
  "member_id": "{member['member_id']}",
  "quarter": "{quarter}",
  "goal_evidence": [
    {{
      "goal_id": "...",
      "title": "...",
      "progress_assessment": "정량 목표는 KPI 갭 기준, 정성 목표는 업무일지 기준 실질 진척도 평가",
      "impact_summary": "이 목표가 팀/회사에 준 임팩트 요약",
      "citations": [{{"log_id": "...", "date": "...", "excerpt": "업무일지 원문 인용(그대로 또는 축약)"}}]
    }}
  ],
  "quarter_review_draft": "분기 성과 리뷰 초안 (전체 목표를 아우르는 3~5문장)"
}}

작성 지침:
- citations 의 log_id/date 는 반드시 위에 주어진 실제 값만 사용하세요 (지어내지 마세요).
- 각 goal 당 citations 는 최소 1개 이상 포함하세요 (업무일지가 있는 경우).
- progress_assessment 는 정량 목표의 경우 반드시 KPI 현재/목표값을 인용하고, 정성 목표의 경우 마일스톤
  상태(본인 보고)를 인용하되 검증된 것처럼 쓰지 마세요.
"""