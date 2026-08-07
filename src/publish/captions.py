"""캡션 생성. 해시태그는 '많이'가 아니라 '맞게'.

인스타 캡션 전략
  - 첫 줄이 전부다. 피드에서 잘리기 전에 결론이 나와야 한다.
  - 저장·댓글을 유도하는 한 줄을 중간에 넣는다 (알고리즘 신호)
  - 해시태그 8~12개. 고정 태그 + 카드별 태그 + 등장 구단 태그
"""

from __future__ import annotations

from typing import Any

BASE_TAGS = ["#KBO", "#프로야구", "#야구", "#KBO리그", "#야구스타그램"]

CARD_TAGS = {
    "daily_recap": ["#경기결과", "#야구하이라이트"],
    "team_standings": ["#순위표", "#팀순위"],
    "yesterday_heroes": ["#오늘의선수", "#수훈선수"],
    "today_results": ["#경기결과", "#야구중계"],
    "today_top_performer": ["#기록", "#야구기록"],
    "playoff_race": ["#가을야구", "#순위싸움", "#포스트시즌"],
    "hitter_leaders": ["#타격순위", "#타율"],
    "pitcher_leaders": ["#투수순위", "#평균자책점"],
    "frustration_index": ["#잔루", "#야구통계"],
    "weekend_preview": ["#주말야구", "#선발매치업"],
    "milestone_watch": ["#대기록", "#기록달성"],
    "weekly_digest": ["#주간결산"],
    "fa_list": ["#FA", "#스토브리그"],
    "salary_rank": ["#연봉", "#스토브리그"],
}

TEAM_TAGS = {
    "삼성": "#삼성라이온즈", "LG": "#LG트윈스", "KT": "#KT위즈", "KIA": "#KIA타이거즈",
    "두산": "#두산베어스", "한화": "#한화이글스", "NC": "#NC다이노스",
    "롯데": "#롯데자이언츠", "SSG": "#SSG랜더스", "키움": "#키움히어로즈",
}

HOOKS = {
    "team_standings": "우리 팀 순위 어디까지 갈 것 같아요?",
    "playoff_race": "5위 싸움 어떻게 보세요? 댓글로 예측 남겨주세요",
    "daily_recap": "어제 제일 인상적이었던 경기는?",
    "frustration_index": "이 지표 보고 한숨 나오는 팀 있나요?",
    "milestone_watch": "가장 먼저 달성할 선수는 누구일까요?",
}
DEFAULT_HOOK = "저장해두고 나중에 다시 보세요"


def build(
    card_id: str,
    title: str,
    rows: list[dict[str, Any]],
    *,
    as_of: str,
    provisional: bool = False,
    lead: str | None = None,
    extra_note: str = "",
) -> str:
    parts: list[str] = []

    # 1) 첫 줄 = 결론
    parts.append(lead or _auto_lead(card_id, title, rows))
    parts.append("")

    # 2) 상위 3건 텍스트 요약 (이미지를 못 보는 상황 대비 + 접근성)
    summary = _top3(rows)
    if summary:
        parts.append(summary)
        parts.append("")

    # 3) 유도 문구
    parts.append(HOOKS.get(card_id, DEFAULT_HOOK))
    parts.append("")

    # 4) 출처
    note = f"기준 {as_of} · 출처 KBO 공식기록"
    if provisional:
        note += " (잠정)"
    if extra_note:
        note += f" · {extra_note}"
    parts.append(note)
    parts.append("")

    # 5) 해시태그
    tags = list(BASE_TAGS) + CARD_TAGS.get(card_id, [])
    for t in _teams_in(rows)[:4]:
        if t in TEAM_TAGS:
            tags.append(TEAM_TAGS[t])
    parts.append(" ".join(dict.fromkeys(tags)))

    return "\n".join(parts)


def build_news(
    line1: str, line2: str, *, team: str | None, outlet_count: int,
    category: str = "지금 KBO",
) -> str:
    """뉴스형 카드(유형 E) 전용 캡션.

    table 계열 카드의 build() 는 rows 요약이 핵심이라 뉴스카드엔 안 맞는다.
    여기선 카드에 이미 쓴 헤드라인을 캡션 첫 줄에 그대로 반복해 피드 미리보기
    에서도 결론이 보이게 하고, '몇 개 매체가 다뤘는지'로 화제성을 한 번 더
    증명한다 — 조회수 대신 이걸 신뢰 신호로 쓰기로 한 설계와 같은 맥락이다.
    """
    headline = line1.strip()
    if line2.strip():
        headline += f" {line2.strip()}"

    parts = [headline, ""]
    parts.append(f"{category} · {outlet_count}개 매체가 다룬 소식")
    parts.append("")
    parts.append("저장해두고 다음 소식도 놓치지 마세요")
    parts.append("")

    tags = list(BASE_TAGS) + ["#지금KBO", "#야구뉴스", "#속보"]
    if team and team in TEAM_TAGS:
        tags.append(TEAM_TAGS[team])
    parts.append(" ".join(dict.fromkeys(tags)))

    return "\n".join(parts)


def _auto_lead(card_id: str, title: str, rows: list[dict]) -> str:
    if not rows:
        return title
    first = rows[0]
    label = first.get("팀명") or first.get("선수명") or first.get("경기") or ""
    if card_id == "team_standings" and label:
        return f"{title} — 1위 {label} ({first.get('승률', '')})"
    if card_id in ("hitter_leaders", "pitcher_leaders") and label:
        return f"{title} — 1위 {label}"
    return title


def _top3(rows: list[dict]) -> str:
    lines = []
    for i, r in enumerate(rows[:3], 1):
        label = r.get("팀명") or r.get("선수명") or r.get("경기")
        if not label:
            continue
        val = next(
            (r[k] for k in ("승률", "타율", "평균자책", "스코어", "기록") if r.get(k)), ""
        )
        lines.append(f"{i}. {label}{f' {val}' if val else ''}")
    return "\n".join(lines)


def _teams_in(rows: list[dict]) -> list[str]:
    out: list[str] = []
    for r in rows:
        t = r.get("_team") or r.get("팀명")
        if t and t not in out:
            out.append(t)
    return out
