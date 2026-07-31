"""발행 전 검증 — 틀린 카드 1장이 계정 신뢰를 날린다는 전제로 설계.

검증 실패 시 기본 정책은 '발행 스킵 + 알림'. 절대 대충 발행하지 않는다.
업로드 사례 참고: '최고 구속 TOP 10' 카드에서 동일 선수명이 9번 반복된 케이스는
원 사이트 기준으로는 정상(투구 단위 집계)이지만 인포그래픽으로는 고장처럼 보인다.
→ duplicate_names 룰이 정확히 이 사고를 막는다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_NUM = re.compile(r"^-?[\d,]+(\.\d+)?$")


@dataclass
class Report:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "검증 통과"
        parts = []
        if self.errors:
            parts.append("ERROR: " + " / ".join(self.errors))
        if self.warnings:
            parts.append("WARN: " + " / ".join(self.warnings))
        return " | ".join(parts)


def _f(v: Any) -> float | None:
    s = str(v).strip().replace(",", "")
    if not _NUM.match(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def validate(
    payload_rows: list[dict[str, Any]],
    columns: list[str],
    cfg: dict[str, Any],
    *,
    card_id: str,
    label_col: str | None = None,
    prev_rows: list[dict[str, Any]] | None = None,
) -> Report:
    rep = Report()
    v = cfg["cards"]["validation"]

    # 1) 빈 데이터
    if not payload_rows:
        rep.fail("데이터 행이 0건")
        return rep

    # 2) 동일 라벨 반복 (표시 로직 버그 탐지)
    if label_col:
        names = [str(r.get(label_col, "")) for r in payload_rows if r.get(label_col)]
        counts = Counter(names)
        worst, n = counts.most_common(1)[0] if counts else ("", 0)
        if n > v["max_duplicate_names"]:
            rep.fail(f"'{worst}' 가 {n}회 반복 — 표시 로직 확인 필요")

    # 3) 팀 카드는 10개 팀 검사
    teams = {r.get("_team") for r in payload_rows if r.get("_team")}
    if card_id in ("team_standings", "frustration_index") and len(teams) != v["require_team_count"]:
        rep.fail(f"팀 수 {len(teams)}개 (기대 {v['require_team_count']}개)")

    # 4) 승률 범위
    lo, hi = v["win_rate_range"]
    for r in payload_rows:
        wr = _f(r.get("승률"))
        if wr is not None and not (lo <= wr <= hi):
            rep.fail(f"승률 이상치: {r.get('팀명', '?')} = {wr}")

    # 5) 승+무+패 == 경기수
    for r in payload_rows:
        g, w, d, l = (_f(r.get(k)) for k in ("경기", "승", "무", "패"))
        if None not in (g, w, d, l) and abs((w + d + l) - g) > 0.5:  # type: ignore[operator]
            rep.fail(f"{r.get('팀명','?')} 승/무/패 합({w}+{d}+{l})이 경기수({g})와 불일치")

    # 6) null / 빈 셀 비율
    total = len(payload_rows) * max(len(columns), 1)
    blanks = sum(
        1 for r in payload_rows for c in columns if str(r.get(c, "")).strip() in ("", "-")
    )
    if total and blanks / total > 0.25:
        rep.warn(f"빈 셀 비율 {blanks / total:.0%}")

    # 7) 전일 스냅샷 대비 급변 (누적 스탯이 줄어들면 파싱 오류)
    if prev_rows and label_col:
        prev = {str(r.get(label_col)): r for r in prev_rows}
        for r in payload_rows:
            key = str(r.get(label_col))
            if key not in prev:
                continue
            for col in ("경기", "승", "안타", "홈런", "타점"):
                cur, old = _f(r.get(col)), _f(prev[key].get(col))
                if cur is not None and old is not None and cur < old - 0.5:
                    rep.fail(f"{key} '{col}' 이 감소({old}→{cur}) — 누적 스탯 역행")

    return rep


def validate_subject(subject: Any, *, card_id: str) -> Report:
    """히어로 카드(유형 C)는 행이 없으므로 주인공 자체를 검증한다."""
    rep = Report()
    if subject is None:
        rep.fail("히어로 카드인데 주인공 정보가 없음")
        return rep
    if not getattr(subject, "name", "").strip():
        rep.fail("주인공 이름이 비어 있음")
    if not getattr(subject, "headline", "").strip():
        rep.fail("헤드라인이 비어 있음")
    # 한글 이름 2~4자가 정상. 벗어나면 파싱 오류 가능성
    name = getattr(subject, "name", "")
    if name and not re.fullmatch(r"[가-힣]{2,5}|[A-Za-z .\-]{2,20}", name):
        rep.warn(f"선수명 형식이 이상함: {name!r}")
    if not getattr(subject, "photo", None):
        rep.warn("사진 미확보 — 레이아웃 자동 강등됨")
    for s in getattr(subject, "stats", []) or []:
        if str(s.get("value", "")).strip() in ("", "-"):
            rep.warn(f"스탯 '{s.get('label')}' 값이 비어 있음")
    return rep


def gate(rep: Report, cfg: dict[str, Any]) -> bool:
    """True 면 발행 진행. skip_on_failure 정책 적용 지점."""
    if rep.ok:
        return True
    return not cfg["cards"]["validation"].get("skip_on_failure", True)
