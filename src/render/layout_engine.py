"""레이아웃 엔진 — 데이터 형태를 보고 유형 A(표) / B(차트) / C(히어로)를 자동 선택.

설계 원칙
  1. 아웃라인(브랜드 프레임)은 유형과 무관하게 고정. base.html.j2 가 단독 소유.
  2. 유형은 '콘텐츠의 모양'이 결정한다. 카드 정의에서 강제 지정도 가능.
  3. 유형 C(히어로)는 선수 사진에 의존 → 사진 확보 실패 시 조용히 fallback.
     사진이 없다고 발행을 스킵하지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Layout = Literal["table", "chart", "hero", "grid", "newscard"]

# 차트로 그릴 수 있는 행 수 범위. 너무 적으면 빈약하고 너무 많으면 바가 뭉개진다.
CHART_MIN_ROWS = 3
CHART_MAX_ROWS = 12
# 표로 감당 가능한 최대 행 수 (1080x1350 기준)
TABLE_MAX_ROWS = 14

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


class LayoutError(RuntimeError):
    """레이아웃을 성립시킬 수 없음 — 해당 카드는 발행하지 않는다."""


@dataclass
class Subject:
    """유형 C 히어로 카드의 주인공(선수 1명 또는 팀 1개)."""

    name: str
    team: str | None = None
    photo: str | None = None          # 로컬 파일 경로 (file:// 로 변환됨)
    photo_pos: str | None = None      # background-position — 얼굴 위치 기반 (Photo.css_position)
    headline: str = ""                # 큰 글씨 한 방 ("연패 탈출")
    sub: str = ""                     # 설명 한 줄
    stats: list[dict[str, str]] = field(default_factory=list)  # [{label, value}]

    @property
    def has_photo(self) -> bool:
        return bool(self.photo)


@dataclass
class NewsCard:
    """유형 E(뉴스형) 카드의 문구 묶음.

    사진은 반드시 사람이 직접 고른 것만 쓴다(assets/newsphotos/). 헤드라인의
    감정선과 사진이 어긋나면 이 포맷은 후킹이 통째로 죽어서, 자동 수집한 사진을
    끼워넣느니 카드를 스킵하는 쪽이 낫다 — 그래서 photo 없으면 강등이 아니라 스킵.
    """

    line1: str                        # 헤드라인 1행 (핵심 주장/사실)
    line2: str = ""                   # 헤드라인 2행 (부연)
    hook: str = ""                    # 상단 노란 괄호 문구 (괄호는 템플릿이 붙임)
    # 하단 배지. 경쟁사가 '크보순삭' 하나로 밀어붙이듯 단일 태그를 반복 노출해
    # 각인시키는 전략이라 기본값을 고정해둔다.
    category: str = "지금 KBO"
    cat_color: str | None = None      # 배지 배경색 (미지정 시 브랜드 accent_cool)
    photo: str | None = None          # 로컬 파일 경로
    photo_pos: str | None = None      # background-position
    photo_credit: str = ""            # 사진 출처 표기

    @property
    def has_photo(self) -> bool:
        return bool(self.photo)


@dataclass
class Payload:
    """모든 카드가 이 형태로 정규화된 뒤 렌더러로 들어간다."""

    card_id: str
    title: str
    kicker: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    metric: str | None = None         # 차트로 그릴 컬럼명
    subject: Subject | None = None
    # 유형 D(그리드)용 — 선수 여러 명을 사진+스탯 카드로 나열할 때 씀.
    # subject(단수)와 별개다: 히어로는 '주인공 1명', 그리드는 '나열 비교'라
    # 의미가 달라 필드를 분리했다.
    subjects: list[Subject] = field(default_factory=list)
    # 유형 E(뉴스형)용 문구 묶음
    news: "NewsCard | None" = None
    as_of: str = ""
    provisional: bool = False
    footnote_extra: str = ""
    # 행 강조: {"삼성": "accent"} 형태. 1위 강조 등에 사용
    emphasis: dict[str, str] = field(default_factory=dict)

    # ── 형태 분석 ──────────────────────────────
    def numeric_columns(self) -> list[str]:
        """숫자로만 채워진 컬럼 목록. 팀명/선수명 같은 라벨 컬럼은 제외된다."""
        if not self.rows:
            return []
        out = []
        for col in self.columns:
            vals = [r.get(col) for r in self.rows]
            vals = [v for v in vals if v not in (None, "", "-")]
            if not vals:
                continue
            if all(_is_number(v) for v in vals):
                out.append(col)
        return out

    def label_column(self) -> str | None:
        """첫 번째 비숫자 컬럼 = 라벨(팀명/선수명)."""
        numeric = set(self.numeric_columns())
        for col in self.columns:
            if col not in numeric:
                return col
        return self.columns[0] if self.columns else None


def _is_number(v: Any) -> bool:
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        return bool(_NUM_RE.match(v.strip().replace(",", "")))
    return False


def select_layout(payload: Payload, card_cfg: dict[str, Any]) -> tuple[Layout, str]:
    """(선택된 레이아웃, 선택 이유) 반환. 이유는 로그·디버깅용."""

    declared = (card_cfg.get("layout") or "auto").lower()
    fallback = (card_cfg.get("fallback_layout") or "table").lower()

    # 1) 명시적 지정
    if declared in ("table", "chart", "hero", "grid", "newscard"):
        if declared == "hero" and not (payload.subject and payload.subject.has_photo):
            return _coerce(fallback, payload), (
                f"hero 지정이지만 사진 없음 → {fallback} 로 강등"
            )
        if declared == "grid" and not payload.subjects:
            return _coerce(fallback, payload), (
                f"grid 지정이지만 나열할 선수(subjects)가 없음 → {fallback} 로 강등"
            )
        # 뉴스카드는 사진이 곧 콘텐츠라 강등이 성립하지 않는다. 사진 없이 표로
        # 떨어뜨리면 헤드라인만 남은 이상한 카드가 나가므로 여기서 끊는다.
        if declared == "newscard":
            if payload.news is None:
                raise LayoutError("newscard 지정이지만 news 문구가 없음")
            if not payload.news.has_photo:
                raise LayoutError(
                    "newscard 에 쓸 사진이 없음 — assets/newsphotos/ 에 사진을 넣어주세요"
                )
        return declared, "카드 정의에서 명시 지정"  # type: ignore[return-value]

    # 2) auto — 주인공이 있고 사진까지 확보되면 히어로
    if payload.subject and payload.subject.has_photo:
        return "hero", "단일 주인공 + 사진 확보"

    # 3) 단일 지표 + 비교 가능한 행 수 → 차트
    metric = payload.metric
    numeric = payload.numeric_columns()
    if metric is None and len(numeric) == 1:
        metric = numeric[0]
    if metric and CHART_MIN_ROWS <= len(payload.rows) <= CHART_MAX_ROWS:
        # 지표가 3개 이상이면 표가 정보량 면에서 낫다
        if len(numeric) <= 2:
            payload.metric = metric
            return "chart", f"단일 지표({metric}) × {len(payload.rows)}행"

    # 4) 기본값 — 표
    return "table", f"다열 데이터({len(numeric)}개 지표) → 표"


def _coerce(layout: str, payload: Payload) -> Layout:
    if layout == "chart" and not (
        payload.metric or len(payload.numeric_columns()) == 1
    ):
        return "table"
    return layout if layout in ("table", "chart", "hero") else "table"  # type: ignore[return-value]


def truncate_rows(payload: Payload, layout: Layout) -> Payload:
    """캔버스를 넘기는 행은 잘라낸다. 잘린 사실은 각주에 남긴다."""
    limit = TABLE_MAX_ROWS if layout == "table" else CHART_MAX_ROWS
    if len(payload.rows) > limit:
        dropped = len(payload.rows) - limit
        payload.rows = payload.rows[:limit]
        note = f"상위 {limit}건만 표시(총 {limit + dropped}건)"
        payload.footnote_extra = (
            f"{payload.footnote_extra} · {note}".strip(" ·")
            if payload.footnote_extra
            else note
        )
    return payload
