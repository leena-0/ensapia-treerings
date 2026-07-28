# -*- coding: utf-8 -*-
"""
코칭 카드 신호 탐지·선별 (결정론적, LLM 미개입).

프로젝트 원칙(개요 v10 §5-4, §7-1): "무엇이 신호인지 / 얼마나 중요한지(순서·점수)"는
코드가 확정하고, LLM은 선별된 신호를 '서술'만 한다. 인용 근거의 실존도 코드가 검증한다.

현재 구현: **의존 병목(dependency bottleneck)** 탐지.
  - 여러 사람이 같은 대상을 기다린다고 쓴 로그를 묶어 구조 병목으로 판정한다 (§5-4-1).
  - 선별 점수 = 영향 인원 × 근거 강도 × 지속 기간 (§5-4-4).
  - 확신도: 영향 인원 3명 이상 '높음', 2명 '중간'. 2명 미만은 병목이 아니므로 카드화하지 않는다 (§5-4-2).

확장 지점(데이터 확보 후 추가 예정): 미해결 요청 · 재작업 반복 · 부하 편중 · 미인지 성과.
탐지 대상 표는 개요 v10 §5-4-1 참조.
"""

import re
from datetime import date

# 막힘/대기 신호 마커 — 이 토큰이 있으면 '무언가를 기다리는 중'일 가능성이 높다.
WAIT_MARKERS = ("대기", "승인", "컨펌", "회신", "착수 불가", "확정 전", "보류", "지연", "기다")

# '같은 대상' 군집화에서 제외할 일반어/기능어 + 마커류.
# (의미 없는 공통 토큰으로 서로 다른 병목이 하나로 뭉치는 과결합을 막는다.)
_STOPWORDS = {
    "이벤트", "작업", "진행", "관련", "위해", "대한", "때문", "우려", "요청", "확인",
    "필요", "예정", "상황", "현재", "그리고", "하지만", "여름", "출시", "일정", "후속",
    "아직", "여전히", "다시", "어떤", "파트장께", "못함", "두고", "번째", "하고",
    # 존재/진행을 나타내는 일반 연결어 — 무엇을 기다리든 거의 항상 같이 나와서 그 자체로는
    # '대상'을 특정하지 못한다. 빠지면 서로 무관한 병목들이 이 단어 하나로 묶여버린다.
    "있음", "없음", "중이라", "중이며",
    # 실제 사업부 목데이터에서 발견: "이슈"(막연한 문제 지칭)/"발생"/"일부"는 서로 다른
    # 부서(추천 로직 서버 부하 vs PG사 정산 지연)의 완전히 무관한 지연 건을 이 단어들만으로
    # 묶어버렸다 — 대상이 아니라 사건을 서술하는 일반어라서 제외한다.
    "이슈", "발생", "일부",
    # 실제 사업부 목데이터 확장 후 재발견: "없어"/"나서"/"보류함"/"전이라"/"재요청"은
    # "~할 사람이 없어", "~가 안 나서", "~을 보류함"처럼 대기 상황을 서술할 때 거의 항상
    # 붙는 일반 어미/동사라, 완전히 무관한 사람 여러 명(PG사 계약 검토 대기·SDK 업그레이드
    # 대기·QA 권한 재요청 등)이 이 단어들만 공유한다는 이유로 하나의 거대 병목으로
    # 잘못 묶였다. 대상이 아니라 "대기 중임을 나타내는 문장 종결" 패턴이라 제외한다.
    "없어", "나서", "보류함", "전이라", "재요청",
    # "다음 단계 진행 가능"/"다음 스텝 진행 가능"처럼 "막힌 일이 풀리면 진행 가능하다"는
    # 결론 어구는 어떤 병목에도 거의 항상 붙어서 서로 다른 병목을 이 어구 하나로 묶는다.
    "가능", "정하고",
    # "요청"은 이미 불용어지만 "요청함"(활용형)은 정확 일치라서 안 걸린다. "다음"(단계/스텝)도
    # 어떤 병목 설명에도 흔히 붙는 일반어라 제외한다.
    "요청함", "다음",
    # 마커류(대기/승인 등)는 '대상'이 아니라 '상태'이므로 대상 토큰에서 제외
    "대기", "승인", "컨펌", "회신", "보류", "지연", "확정", "착수", "불가", "회신을",
}

# 토큰 끝에 붙는 대표적인 조사 (길이 내림차순으로 벗겨낸다)
_JOSA = ("으로써", "으로서", "이라고", "으로", "로서", "로써", "에게", "에서", "까지",
         "부터", "이나", "처럼", "보다", "한테", "이며", "하고", "이고", "와의", "과의",
         "에는", "에도", "은", "는", "이", "가", "을", "를", "의", "에", "도", "만",
         "과", "와", "로", "랑")

_TOKEN_SPLIT = re.compile(r"[\s,./·:;()\[\]\"'`~!?※…\-]+")


def _norm_token(tok: str) -> str:
    """구두점 제거 + 대표 조사 제거로 토큰을 정규화한다 (예: '구성안을' -> '구성안')."""
    tok = re.sub(r"[^\w가-힣]", "", tok)
    for j in sorted(_JOSA, key=len, reverse=True):
        if len(tok) > len(j) + 1 and tok.endswith(j):
            return tok[: -len(j)]
    return tok


# '기다리다/늦어지다/밀리다'류는 어미가 계속 바뀌어(기다리는/기다리고, 늦어지고/늦어지는,
# 밀리는/밀릴) _STOPWORDS 의 정확 일치로 못 걸러진다. '무언가에 막혀 지연되고 있다'는 상태를
# 나타내는 동사일 뿐 대상을 특정하지 않으므로, 다른 병목인데도 이 동사 하나로 묶이는 것을
# 막기 위해 접두어로 제외한다(다른 마커는 명사라 어미 변화가 없어 정확 일치로 충분함).
_MARKER_PREFIXES = ("기다", "늦어지", "밀리")


def _content_tokens(text: str) -> set:
    """대상(object) 군집화에 쓸 의미 토큰 집합. 2글자 이상, 불용어/숫자/마커 동사 제외."""
    out = set()
    for raw in _TOKEN_SPLIT.split(text):
        t = _norm_token(raw)
        if (len(t) >= 2 and t not in _STOPWORDS and not t.isdigit()
                and not t.startswith(_MARKER_PREFIXES)):
            out.add(t)
    return out


def _is_waiting_log(text: str) -> bool:
    return any(m in text for m in WAIT_MARKERS)


def _excerpt(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[:n] + "…"


def _object_tokens(waiting_logs, nonwaiting_logs, min_waiting_df=2):
    """
    '병목의 대상'이 되는 토큰을 뽑는다.

    핵심 아이디어: 병목의 대상(예: '리워드 구성안', '가챠')은 **막혔을 때만** 언급되고
    평상시 진척 로그에는 나오지 않는다. 따라서 '대기 로그에는 2건 이상 등장하되
    일반(비대기) 로그에는 전혀 안 나오는' 토큰만 대상 토큰으로 본다.
    → '리텐션/로직/개선' 같은 흔한 단어로 서로 다른 병목이 뭉치는 과결합을 막는다.
    """
    from collections import Counter
    nonwait = set()
    for l in nonwaiting_logs:
        nonwait |= _content_tokens(l["text"])
    wc = Counter()
    for l in waiting_logs:
        wc.update(_content_tokens(l["text"]))
    return {t for t, c in wc.items() if c >= min_waiting_df and t not in nonwait}


def _cluster_by_objects(waiting_logs, object_tokens):
    """
    대기 로그를 '공유 대상 토큰'으로만 연결해 군집(connected components)을 만든다.
    대상 토큰을 하나도 안 가진 대기 로그(예: 개인 사정으로 인한 대기)는 단독으로 남아
    이후 '2명 이상' 조건에서 자연히 제외된다.
    """
    tokens = [(_content_tokens(l["text"]) & object_tokens, l) for l in waiting_logs]
    n = len(tokens)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        if not tokens[i][0]:
            continue
        for j in range(i + 1, n):
            if tokens[i][0] & tokens[j][0]:  # 대상 토큰 1개 이상 공유
                union(i, j)

    groups = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(tokens[idx][1])
    # 대상 토큰이 전혀 없는 단독 로그는 병목 후보에서 제외
    return [g for g in groups.values() if len(g) >= 2]


def _confidence(impact_count: int) -> str:
    # §5-4-2: 확신도가 문형을 결정. 낮음(2명 미만)은 애초에 병목이 아니라 호출 전 제외.
    return "높음" if impact_count >= 3 else "중간"


def detect_bottlenecks(records):
    """
    범용 코어: 로그 레코드 목록에서 의존 병목을 탐지한다 (데이터 소스 독립).

    records: dict 리스트, 각 항목은 {"id", "user_id", "date"(ISO str), "text"}.
    반환 각 신호(dict):
      type, member_ids, log_ids, evidence[{id,user_id,date,excerpt}], shared_terms,
      impact_count(영향 인원), evidence_strength(대기 로그 수), duration_days(지속 기간),
      score(= 영향 인원 × 근거 강도 × 지속 기간), confidence
    """
    from collections import Counter

    waiting, nonwaiting = [], []
    for r in records:
        (waiting if _is_waiting_log(r["text"]) else nonwaiting).append(r)
    object_tokens = _object_tokens(waiting, nonwaiting)

    signals = []
    for cluster in _cluster_by_objects(waiting, object_tokens):
        members = sorted({r["user_id"] for r in cluster})
        if len(members) < 2:
            continue  # 2명 미만은 개인 일정일 뿐, 구조 병목 아님 (§5-4-1)

        dates = sorted(date.fromisoformat(r["date"]) for r in cluster)
        duration_days = (dates[-1] - dates[0]).days + 1

        tok_counter = Counter()
        for r in cluster:
            tok_counter.update(_content_tokens(r["text"]) & object_tokens)
        shared_terms = [t for t, _ in tok_counter.most_common(4)]

        # 인용 근거: 멤버별 '가장 이른' 대기 로그 1건씩(모든 당사자가 드러나게), 날짜순
        by_user_earliest = {}
        for r in sorted(cluster, key=lambda x: x["date"]):
            by_user_earliest.setdefault(r["user_id"], r)
        evidence = [
            {"id": r["id"], "user_id": r["user_id"], "date": r["date"], "excerpt": _excerpt(r["text"])}
            for r in sorted(by_user_earliest.values(), key=lambda x: x["date"])
        ]

        impact_count = len(members)
        evidence_strength = len(cluster)
        signals.append({
            "type": "dependency_bottleneck",
            "member_ids": members,
            "log_ids": sorted(r["id"] for r in cluster),
            "evidence": evidence,
            "shared_terms": shared_terms,
            "impact_count": impact_count,
            "evidence_strength": evidence_strength,
            "duration_days": duration_days,
            "score": impact_count * evidence_strength * duration_days,
            "confidence": _confidence(impact_count),
        })
    return signals


def detect_dependency_bottlenecks(store, team):
    """DataStore(CSV) 어댑터: store의 팀 로그를 코어로 넘기고, 근거 키를 reports 형식으로 변환."""
    records = [
        {"id": l["log_id"], "user_id": l["member_id"], "date": l["date"], "text": l["text"]}
        for member_id in store.members_by_team.get(team, [])
        for l in store.logs_by_member.get(member_id, [])
    ]
    signals = detect_bottlenecks(records)
    for s in signals:  # evidence 키를 기존 reports 소비자에 맞게 rename
        s["evidence"] = [
            {"log_id": e["id"], "member_id": e["user_id"], "date": e["date"], "excerpt": e["excerpt"]}
            for e in s["evidence"]
        ]
    return signals


def detect_signals(store, team, *, top_n=5):
    """
    팀의 코칭 신호를 탐지·선별한다. 현재는 의존 병목만.
    선별: score(영향 인원 × 근거 강도 × 지속 기간) 내림차순 상위 top_n (§5-4-4).
    """
    signals = detect_dependency_bottlenecks(store, team)
    signals.sort(key=lambda s: s["score"], reverse=True)
    return signals[:top_n]


if __name__ == "__main__":
    # 눈으로 확인용: 팀별 탐지 결과 출력
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    from .data_access import DataStore
    store = DataStore()
    for team in store.members_by_team:
        sigs = detect_signals(store, team)
        print(f"\n=== 팀: {team} — 탐지 신호 {len(sigs)}건 ===")
        for s in sigs:
            print(f"  [{s['type']}] 확신도={s['confidence']} score={s['score']} "
                  f"(영향 {s['impact_count']}명 × 근거 {s['evidence_strength']} × 지속 {s['duration_days']}일)")
            print(f"    대상 토큰: {s['shared_terms']}")
            print(f"    멤버: {s['member_ids']}")
            for e in s["evidence"]:
                print(f"    - {e['log_id']} [{e['date']}] {e['member_id']}: {e['excerpt']}")
