"""
평가 근거 패키지(report_cache 의 evidence_package 캐시)를 PDF로 변환한다.

한글 렌더링을 위해 유니코드 폰트가 필요하다 (fpdf2 코어 폰트는 한글 미지원). 폰트 탐색 우선순위:
  1. PDF_FONT_PATH 환경변수 (배포 환경에서 지정 -- GCE/Linux 에서는 Noto Sans KR 등 오픈 라이선스
     폰트를 설치하고 이 경로를 지정할 것)
  2. 프로젝트 assets/fonts/*.ttf (번들된 오픈 폰트가 있으면 사용)
  3. (macOS 로컬 개발 전용 폴백) 시스템에 이미 설치된 AppleSDGothicNeo.ttc 에서 런타임에 단일
     폰트를 추출해 임시 캐시. Apple 폰트는 재배포 라이선스가 없으므로 **배포 환경에서는 사용하지 않는다**
     (이 폴백은 로컬 macOS 환경에 이미 설치되어 있는 시스템 폰트를 그 자리에서 읽어 쓰는 것일 뿐,
     폰트 파일을 프로젝트에 복사/배포하지 않는다).
"""

import os
import tempfile

from fpdf import FPDF

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_FONT = os.path.join(PROJECT_ROOT, "assets", "fonts", "NotoSansKR-Regular.ttf")
_MACOS_SYSTEM_FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"

_font_cache_path = None


def _resolve_font_path():
    global _font_cache_path

    env_path = os.environ.get("PDF_FONT_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    if os.path.exists(ASSETS_FONT):
        return ASSETS_FONT
    if _font_cache_path and os.path.exists(_font_cache_path):
        return _font_cache_path
    if os.path.exists(_MACOS_SYSTEM_FONT):
        from fontTools.ttLib import TTFont
        font = TTFont(_MACOS_SYSTEM_FONT, fontNumber=3)  # Regular 근접 weight, 로컬 개발 전용
        tmp_path = os.path.join(tempfile.gettempdir(), "ensapia_kr_font_dev_only.ttf")
        font.save(tmp_path)
        _font_cache_path = tmp_path
        return tmp_path

    raise RuntimeError(
        "한글 PDF 렌더링용 폰트를 찾을 수 없습니다. PDF_FONT_PATH 환경변수로 TTF 경로를 지정하거나 "
        "assets/fonts/NotoSansKR-Regular.ttf 를 추가하세요."
    )


def build_evidence_pdf(member, content, output_path):
    font_path = _resolve_font_path()

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.add_font("KR", "", font_path)
    pdf.add_font("KR", "B", font_path)  # bold 전용 파일이 없어 동일 폰트로 대체

    w = pdf.epw  # 유효 페이지 너비 -- w=0(자동) 대신 명시해 커서 위치 문제를 피한다

    def line(text, size=10, bold=False, color=(0, 0, 0), height=6):
        pdf.set_x(pdf.l_margin)
        pdf.set_font("KR", "B" if bold else "", size)
        pdf.set_text_color(*color)
        pdf.multi_cell(w, height, text, new_x="LMARGIN", new_y="NEXT")

    line(f"{member['name']}님 평가 근거 패키지", size=18, bold=True, height=10)
    line(f"{member['team']} · {member['role']} · {content.get('quarter', '')}", size=11, color=(90, 90, 90))
    pdf.ln(4)

    for ge in content.get("goal_evidence", []):
        line(f"[{ge.get('goal_id', '')}] {ge.get('title', '')}", size=13, bold=True, height=8)
        line(f"진척도 평가: {ge.get('progress_assessment', '')}")
        line(f"임팩트: {ge.get('impact_summary', '')}")
        for c in ge.get("citations", []):
            line(f"  - [{c.get('log_id', '')} / {c.get('date', '')}] {c.get('excerpt', '')}",
                 size=9, color=(90, 90, 90), height=5)
        milestones = ge.get("milestones") or []
        if milestones:
            # 이모지(✅/🚧/⬜)는 fpdf2 + Noto Sans KR/AppleSDGothic 폰트에서 정상 렌더링 검증이 안 되어
            # 텍스트 라벨([완료] 등)로 표기한다 (Slack 쪽은 클라이언트가 렌더링하므로 이모지 사용 가능).
            line("마일스톤 (본인 보고 -- 리더 검토 필요, 자동 검증 아님):", size=10, bold=True, height=5)
            for m in milestones:
                reported = f" · 본인 보고 {m['self_reported_at'][:10]}" if m.get("self_reported_at") else ""
                evidence = ", ".join(m.get("evidence_log_ids") or []) or "없음"
                line(f"  [{m.get('status', '')}] {m.get('title', '')}{reported} · 근거: {evidence}",
                     size=9, color=(90, 90, 90), height=5)

        sub_goals = ge.get("sub_goals") or []
        if sub_goals:
            # §5-4 "목표별 결과: 완료·미완 항목과 근거" -- subgoal_stage()가 실제 로그 일수를 세어
            # 계산한 상태이지 LLM 서술이 아니다 (_subgoal_summary_for_goal, generate_reports.py).
            line("하위 목표(건물) 현황:", size=10, bold=True, height=5)
            for sg in sub_goals:
                confirmed = f" · 확정 {sg['confirmed_at'][:10]}" if sg.get("confirmed_at") else ""
                line(f"  [{sg['status']}] {sg['title']} (누적 {sg['worked_days']}일){confirmed}",
                     size=9, color=(90, 90, 90), height=5)
        pdf.ln(3)

    collaboration = content.get("collaboration") or {}
    if collaboration.get("helped") or collaboration.get("received"):
        # §5-4 "협업 기록(함께 일한 관계/도운 것/도움받은 것)" -- wish_match.py(confirmed)에서
        # 집계한 사실이며, 원문 발췌는 담지 않고 관계·주제·월 단위 시점만 노출한다(§5-3 원칙과 동일).
        line("협업 기록", size=13, bold=True, height=8)
        line(f"같은 팀 협업 {collaboration.get('same_team_count', 0)}건 · "
             f"다른 팀 협업 {collaboration.get('other_team_count', 0)}건", size=10, color=(90, 90, 90))
        for h in collaboration.get("helped", []):
            line(f"  🌱 도운 것 -- {h['name']}({h['team']}) · {h['month']}경 · {h['topic']}",
                 size=9, color=(90, 90, 90), height=5)
        for r in collaboration.get("received", []):
            line(f"  🙏 도움받은 것 -- {r['name']}({r['team']}) · {r['month']}경 · {r['topic']}",
                 size=9, color=(90, 90, 90), height=5)
        pdf.ln(3)

    line("분기 성과 리뷰 초안", size=13, bold=True, height=8)
    line(content.get("quarter_review_draft", ""))

    pdf.output(output_path)
    return output_path
