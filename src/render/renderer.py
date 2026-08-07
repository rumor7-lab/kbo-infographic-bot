"""렌더러 — Payload → PNG(피드) / MP4(릴스).

릴스는 Playwright 비디오 녹화를 쓰지 않는다. Web Animations API 로 CSS 애니메이션의
currentTime 을 프레임 단위로 직접 세팅하고 스크린샷을 찍어 PNG 시퀀스를 만든 뒤
ffmpeg 로 인코딩한다. 녹화 방식보다 프레임 타이밍이 정확하고 화질 손실이 없다.
"""

from __future__ import annotations

import base64
import math
import mimetypes
import random
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .layout_engine import Layout, Payload, select_layout, truncate_rows

ROOT = Path(__file__).resolve().parents[2]
TPL_DIR = Path(__file__).parent / "templates"

TEMPLATE_OF: dict[Layout, str] = {
    "table": "table.html.j2",
    "chart": "chart.html.j2",
    "hero": "hero.html.j2",
    "grid": "grid.html.j2",
    "newscard": "newscard.html.j2",
}

# 배경 3종 중 렌더마다 하나를 랜덤으로 고른다(base.html.j2 의 .card.bg-N).
# 히어로(사진형)는 사진이 배경을 덮어써서 어떤 값이 골리든 시각적으로 무해하다.
BG_VARIANTS = ["bg-1", "bg-2"]


# ── 설정 로드 ────────────────────────────────────
def load_cfg() -> dict[str, Any]:
    with open(ROOT / "config" / "brand.yml", encoding="utf-8") as fp:
        brand = yaml.safe_load(fp)
    with open(ROOT / "config" / "cards.yml", encoding="utf-8") as fp:
        cards = yaml.safe_load(fp)
    return {"brand": brand, "cards": cards}


def _file_url(p: str | Path) -> str:
    """로컬 이미지를 base64 data URI로 인라인한다.

    원래는 file:// URI를 그대로 썼는데, Playwright가 page.set_content()로 넣은
    HTML은 origin이 없는 취급(about:blank)이라 크로미움이 file:// 리소스 로드를
    막아버려 로고/선수 사진이 전부 깨진 이미지로 나오는 문제가 있었다.
    data URI는 origin/파일접근 정책과 무관하게 항상 로드되므로 이 문제를 원천 차단한다.
    """
    path = Path(p).resolve()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _logo_url(team: str, cfg: dict) -> str | None:
    lg = cfg["brand"]["logos"]
    if not lg.get("enabled"):
        return None
    base = ROOT / lg["dir"] / team
    # 대부분 png 지만, 팀에 따라 위키미디어 원본이 jpg 인 경우가 있어(예: KIA) 확장자를 순회 확인.
    for ext in (".png", ".jpg", ".jpeg", ".svg"):
        path = base.with_suffix(ext)
        if path.exists():
            return _file_url(path)
    return None


# ── 적응형 사이징 ────────────────────────────────
# 각 유형의 '행 1개'가 아무리 좁아도 절대 이 밑으로는 안 내려가는 최소 높이.
# 타이틀 크기를 정하기 전에 먼저 이 최소 본문 공간부터 확보해야 한다 —
# 순서를 거꾸로 하면(타이틀 먼저) 10행짜리 카드에서 막대/셀이 헤더 쪽으로
# 밀려 올라가 겹치는 사고가 난다 (실제로 한 번 발생했던 문제).
_TABLE_MIN_ROW = 46      # cell_pad 최소치 기준 셀 높이
_CHART_MIN_ROW = 78      # bar_h(38) + rec 캡션(34) + gap(6) 최저치 합

_TITLE_SIZE_CANDIDATES = [126, 114, 102, 92, 84, 76, 70, 64, 58, 52]
_TITLE_MAX_LINES = 2
_TITLE_LINE_HEIGHT = 1.08
_TITLE_SAFETY_PAD = 28   # 폰트 메트릭 오차 대비 여유


def min_body_px(n_rows: int, layout: Layout) -> int:
    """이 카드가 최소한 필요로 하는 본문 높이. 타이틀은 이걸 침범할 수 없다."""
    if layout == "table":
        return _TABLE_MIN_ROW * max(n_rows + 1, 1)
    if layout == "chart":
        return _CHART_MIN_ROW * max(n_rows, 1)
    return 0  # 히어로는 사진 위 하단 고정 배치라 헤더 크기와 무관


def _char_width_units(ch: str) -> float:
    """검은고딕 기준 문자 1개의 대략적인 폭(= font-size 배수)."""
    if ch == " ":
        return 0.30
    if ch.isascii():
        return 0.66
    return 0.98  # 한글 — 검은고딕은 글자가 거의 정사각형


def _text_width(text: str, font_px: float) -> float:
    return sum(_char_width_units(ch) for ch in text) * font_px


def _wrap_lines(text: str, font_px: float, avail_px: float) -> list[str]:
    """공백 기준 줄바꿈 시뮬레이션. 실제 브라우저 렌더와 100% 일치하진 않지만
    헤더 높이를 미리 예약하기 위한 근사치로 충분하다."""
    words = text.split(" ")
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if not cur or _text_width(trial, font_px) <= avail_px:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _header_height_px(
    *, title_size: int, title_lines: int, has_kicker: bool, has_title_sub: bool
) -> int:
    """실제 CSS 수치에 맞춘 헤더(키커+타이틀+강조바) 높이 추정 + 안전 여유."""
    h = 56 + 20  # frame-head 상하 padding (56 위, 20 아래)
    if has_kicker:
        h += 67  # 배지형 키커 박스 + margin-bottom
    h += int(title_size * _TITLE_LINE_HEIGHT * title_lines)
    h += 9 + 24  # title-rule 높이 + margin-top
    if has_title_sub:
        h += 46 + 16
    return h + _TITLE_SAFETY_PAD


def fit_title(
    text: str,
    *,
    n_rows: int,
    layout: Layout,
    canvas_h: int,
    footer_h: int,
    has_kicker: bool,
    has_title_sub: bool,
    avail_px: int = 880,
) -> tuple[int, int, int]:
    """행 수가 요구하는 최소 본문 공간을 먼저 빼고, 남는 헤더 예산 안에서만
    가장 큰 폰트 크기를 고른다. 반환값: (font_px, line_count, header_px)"""
    reserved_body = min_body_px(n_rows, layout)
    max_header = canvas_h - footer_h - reserved_body

    def _try(max_lines: int) -> tuple[int, int, int] | None:
        for size in _TITLE_SIZE_CANDIDATES:
            lines = _wrap_lines(text, size, avail_px)
            if len(lines) > max_lines:
                continue
            header_px = _header_height_px(
                title_size=size, title_lines=len(lines),
                has_kicker=has_kicker, has_title_sub=has_title_sub,
            )
            if header_px <= max_header:
                return size, len(lines), header_px
        return None

    # 1순위: 한 줄로 끝나는 크기 중 가장 큰 것 — 웬만하면 줄바꿈이 안 생기게 한다.
    fit = _try(1)
    if fit:
        return fit
    # 2순위: 그래도 안 되면 여기까지만 허용.
    fit = _try(_TITLE_MAX_LINES)
    if fit:
        return fit

    # 후보 중 아무것도 예산 안에 못 들어가면(극단적으로 행이 많은 경우) 가장 작은
    # 크기를 쓰되, 헤더 높이는 실제 계산값을 그대로 반환해 _fit() 이 최대한 보정하게 한다.
    smallest = _TITLE_SIZE_CANDIDATES[-1]
    lines = _wrap_lines(text, smallest, avail_px)
    header_px = _header_height_px(
        title_size=smallest, title_lines=len(lines),
        has_kicker=has_kicker, has_title_sub=has_title_sub,
    )
    return smallest, len(lines), header_px


def _fit(n_rows: int, layout: Layout, height: int, header_px: int) -> dict[str, Any]:
    """행 수 + 실제 헤더 높이에 맞춰 셀 높이/바 굵기를 조절."""
    if layout == "table":
        avail = height - header_px - 84 - 60
        per = max(_TABLE_MIN_ROW, min(96, avail / max(n_rows + 1, 1)))
        return {"cell_pad": int((per - 44) / 2)}
    if layout == "chart":
        # 막대 아래 승무패 기록 캡션 한 줄(고정 ~34px)을 뺀 나머지로 막대 굵기를 계산.
        # 캡션을 막대 밖에 두는 이유: 하위 팀일수록 막대가 짧아져 안쪽에 넣으면
        # 텍스트가 막대 밖으로 새어 나가 흰 배경에 흰 글씨로 안 보이게 되는 문제가 있었음.
        rec_h = 34
        avail = height - header_px - 84 - 30
        per = max(_CHART_MIN_ROW, avail / max(n_rows, 1))
        bar_h = int(max(38, min(78, (per - rec_h) * 0.74)))
        gap = int(max(6, min(18, per - rec_h - bar_h)))
        return {"bar_h": bar_h, "bar_gap": gap}
    return {}


def _bar_pcts(rows: list[dict], metric: str) -> None:
    """바 길이 정규화. 최소값도 최소 34%는 차지하게 해서 라벨이 안 잘리게 한다.
    "97.3%" 처럼 % 단위가 붙은 표시값도 폭 계산 시엔 숫자만 뽑아 쓴다
    (표시 문자열은 그대로 두고 파싱할 때만 벗겨낸다)."""
    vals = []
    for r in rows:
        try:
            raw = str(r.get(metric, 0)).replace(",", "").replace("%", "").strip()
            vals.append(float(raw))
        except ValueError:
            vals.append(0.0)
    hi, lo = max(vals) if vals else 1.0, min(vals) if vals else 0.0
    span = hi - lo if hi != lo else (hi or 1.0)
    for r, v in zip(rows, vals):
        norm = (v - lo) / span if span else 1.0
        r["_bar_pct"] = round(34 + norm * 66, 2)


def _diff_class(v: Any) -> str:
    s = str(v).strip()
    if s.startswith("+") and s not in ("+0", "+0.0"):
        return "up"
    if s.startswith("-"):
        return "down"
    return ""


def _headline_size(text: str) -> int:
    # 검은고딕(Black Han Sans)은 글자 폭이 프리텐다드보다 넓어 같은 글자 수 기준으로
    # 사이즈를 한 단계씩 낮춰 캔버스 넘침을 방지한다.
    n = len(text)
    if n <= 4:
        return 176
    if n <= 6:
        return 138
    if n <= 10:
        return 108
    return 84


def _news_line_size(text: str, *, base: int = 78) -> int:
    """뉴스카드 헤드라인 자동 축소.

    가용 폭 968px(1080 - 좌우 56px) 기준. 한글 볼드는 대략 글자당 폰트크기의
    0.95배 폭을 먹으므로 그 선에서 2줄을 넘기지 않게 단계적으로 줄인다.
    <em> 강조 태그는 폭에 영향이 없으니 길이 계산에서 뺀다.

    기존 62px 기준이 실측(카드 실제 발행분)에서 너무 작다는 피드백을 받아
    전체적으로 한 단계씩 키웠다 — 짧은 헤드라인일수록 여백을 더 적극적으로
    큰 글씨에 써도 되므로 기준값(base)만 올리고 단계 폭 비율은 유지한다.
    """
    n = len(re.sub(r"</?em>", "", text or ""))
    for limit, size in ((10, base), (14, 68), (18, 60), (24, 52), (30, 45)):
        if n <= limit:
            return min(base, size)
    return min(base, 40)


def _split_emphasis(title: str) -> tuple[str, str]:
    """타이틀 마지막 단어를 포인트 컬러로 분리 — '팀 순위' → '팀 ' + '순위'.
    벤치마크들이 전부 타이틀 안에서 색 강조를 주는 방식을 그대로 채택."""
    parts = title.rsplit(" ", 1)
    if len(parts) == 2 and parts[1]:
        return parts[0] + " ", parts[1]
    return "", title


# ── 컨텍스트 조립 ────────────────────────────────
def build_context(
    payload: Payload,
    card_cfg: dict[str, Any],
    cfg: dict[str, Any],
    *,
    layout: Layout,
    motion: bool,
    bg_variant: str | None = None,
) -> dict[str, Any]:
    b = cfg["brand"]
    canvas = b["canvas"]["reels" if motion else "feed"]
    W, H = canvas["width"], canvas["height"]

    rows = payload.rows
    label_col = payload.label_column()

    # 팀 메타 부착
    for r in rows:
        team = r.get("_team") or (r.get(label_col) if label_col else None)
        if team in b["teams"]:
            r["_team"] = team
            r["_logo"] = _logo_url(team, cfg)

    # 차트인데 metric 이 비어 있으면(레이아웃 강제 지정 등) 숫자 컬럼에서 추론
    if layout == "chart" and not payload.metric:
        nums = payload.numeric_columns()
        payload.metric = nums[0] if nums else None
    if layout == "chart":
        if not payload.metric:
            raise ValueError(f"{payload.card_id}: chart 레이아웃인데 숫자 지표가 없습니다")
        _bar_pcts(rows, payload.metric)

    footer = b["frame"]["footer_template"]  # 이제 출처 고정 문구뿐 (기준일자는 제목 옆)
    extra = payload.footnote_extra or card_cfg.get("footnote_extra", "")
    if extra:
        footer = f"{footer} · {extra}"

    subject = payload.subject
    if subject and subject.photo:
        subject.photo = _file_url(subject.photo)

    # 그리드(유형 D) — 나열되는 선수 각각의 사진도 동일하게 data URI로 인라인
    for gs in payload.subjects:
        if gs.photo:
            gs.photo = _file_url(gs.photo)

    # 뉴스카드(유형 E) — 사람이 직접 넣은 사진을 인라인
    news = payload.news
    if news and news.photo:
        news.photo = _file_url(news.photo)

    title_main, title_emphasis = _split_emphasis(payload.title)
    kicker_text = payload.kicker or card_cfg.get("kicker", "")
    title_sub_text = card_cfg.get("title_sub", "")

    # 행 수가 요구하는 최소 본문 공간을 먼저 확보한 뒤, 남는 예산 안에서만 타이틀을 키운다.
    # (10행짜리 카드에서 타이틀만 먼저 키우면 행이 헤더 쪽으로 밀려 겹치는 문제가 있었음)
    title_size, title_lines, header_px = fit_title(
        payload.title,
        n_rows=len(rows),
        layout=layout,
        canvas_h=H,
        footer_h=b["frame"]["footer_height"],
        has_kicker=bool(kicker_text),
        has_title_sub=bool(title_sub_text),
    )

    ctx: dict[str, Any] = {
        "brand": b["brand"],
        "c": b["colors"],
        "t": b["typography"],
        "f": b["frame"],
        "teams": b["teams"],
        "logos_enabled": b["logos"]["enabled"],
        "logo_size": b["logos"]["size"],
        "W": W,
        "H": H,
        "motion": motion,
        "title": payload.title,
        "title_main": title_main,
        "title_emphasis": title_emphasis,
        "title_size": title_size,
        "title_sub": title_sub_text,
        "kicker": kicker_text,
        "as_of": payload.as_of,
        "bg_variant": bg_variant or random.choice(BG_VARIANTS),
        "columns": payload.columns,
        "rows": rows,
        "label_col": label_col,
        "metric": payload.metric,
        "subject": subject,
        "subjects": payload.subjects,
        # 4명 이하면 큼직하게 2열, 그 이상이면 4열로 촘촘하게 (그리드 유형 전용)
        "grid_cols": "cols-4" if len(payload.subjects) > 4 else "cols-2",
        "provisional": payload.provisional or card_cfg.get("provisional", False),
        "footer_text": footer,
        "rank_highlight": True,
        "diff_cols": {"승패차", "게임차", "차이", "증감"},
        "diff_class": _diff_class,
        "team_color": (
            b["teams"].get(subject.team, {}).get("primary") if subject else None
        ),
        "photo_pos": (subject.photo_pos if subject and subject.photo_pos else "center 18%"),
        "headline_size": _headline_size(subject.headline if subject else ""),
    }

    # ── 뉴스카드(유형 E) 전용 컨텍스트 ──────────
    if news:
        line1_size = _news_line_size(news.line1)
        ctx.update({
            "photo": news.photo,
            # 보도사진은 인물이 중앙~상단에 오는 경우가 많고, 하단은 헤드라인이
            # 덮으므로 기본값을 살짝 위로 잡는다.
            "photo_pos": news.photo_pos or "center 22%",
            "photo_credit": news.photo_credit,
            "hook": news.hook,
            "hook_size": _news_line_size(news.hook, base=38),
            "category": news.category,
            "cat_color": news.cat_color,
            "line1": news.line1,
            "line2": news.line2,
            "line1_size": line1_size,
            # 2행은 부연이라 1행보다 한 톤 작게 — 다만 1행이 이미 많이 줄었으면
            # 같이 줄지 않도록 자체 길이 기준도 함께 적용한다.
            "line2_size": min(int(line1_size * 0.92), _news_line_size(news.line2, base=66)),
        })
    ctx.update(_fit(len(rows), layout, H, header_px))
    return ctx


def render_html(
    payload: Payload, card_cfg: dict, cfg: dict, *, motion: bool = False,
    bg_variant: str | None = None,
):
    layout, reason = select_layout(payload, card_cfg)
    payload = truncate_rows(payload, layout)

    env = Environment(
        loader=FileSystemLoader(str(TPL_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    ctx = build_context(
        payload, card_cfg, cfg, layout=layout, motion=motion, bg_variant=bg_variant
    )
    html = env.get_template(TEMPLATE_OF[layout]).render(**ctx)
    return html, layout, reason


# ── PNG ──────────────────────────────────────────
def shoot_png(html: str, out: Path, width: int, height: int) -> Path:
    from playwright.sync_api import sync_playwright

    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # set_content() 로 넣은 HTML은 origin이 없어(about:blank 취급) 기본적으로
        # file:// 이미지(로고, 선수 사진) 로드가 크로미움 보안정책에 막힌다.
        # --allow-file-access-from-files 로 풀어준다.
        browser = p.chromium.launch(
            args=["--force-color-profile=srgb", "--allow-file-access-from-files"]
        )
        page = browser.new_page(
            viewport={"width": width, "height": height}, device_scale_factor=1
        )
        page.set_content(html, wait_until="load")
        page.wait_for_timeout(700)  # 웹폰트 + 이미지 로드 대기
        page.screenshot(path=str(out), type="png")
        browser.close()
    return out


# ── MP4 (릴스) ───────────────────────────────────
def shoot_reel(
    html: str, out: Path, width: int, height: int, *, fps: int, duration: float
) -> Path:
    """CSS 애니메이션을 프레임 단위로 seek 하며 PNG 시퀀스 → ffmpeg 인코딩."""
    from playwright.sync_api import sync_playwright

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg 가 PATH 에 없습니다. scripts/setup.md 참고")

    out.parent.mkdir(parents=True, exist_ok=True)
    n_frames = int(math.ceil(fps * duration))
    tmp = Path(tempfile.mkdtemp(prefix="reel_"))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--force-color-profile=srgb", "--allow-file-access-from-files"]
            )
            page = browser.new_page(
                viewport={"width": width, "height": height}, device_scale_factor=1
            )
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(800)
            # 자동 재생 정지 — 이후 currentTime 을 우리가 직접 제어
            page.add_style_tag(
                content="*,*::before,*::after{animation-play-state:paused !important}"
            )
            for i in range(n_frames):
                t_ms = (i / fps) * 1000
                page.evaluate(
                    "(t) => document.getAnimations().forEach(a => { a.currentTime = t; })",
                    t_ms,
                )
                page.screenshot(path=str(tmp / f"f{i:05d}.png"), type="png")
            browser.close()

        subprocess.run(
            [
                "ffmpeg", "-y", "-framerate", str(fps),
                "-i", str(tmp / "f%05d.png"),
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-profile:v", "high", "-level", "4.1",
                "-crf", "18", "-movflags", "+faststart",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def render_card(
    payload: Payload, card_cfg: dict, cfg: dict, out_dir: Path
) -> dict[str, Any]:
    """피드 PNG 생성. card_cfg 또는 슬롯 설정에서 릴스가 켜져 있으면 MP4도 생성."""
    b = cfg["brand"]
    feed = b["canvas"]["feed"]
    reels = b["canvas"]["reels"]

    # 피드 이미지와 릴스가 서로 다른 배경을 랜덤으로 골라버리면 같은 카드인데
    # 버전마다 배경이 달라 어색하다. 한 번만 뽑아서 두 렌더 모두에 같이 쓴다.
    bg_variant = random.choice(BG_VARIANTS)

    html, layout, reason = render_html(
        payload, card_cfg, cfg, motion=False, bg_variant=bg_variant
    )
    png = shoot_png(html, out_dir / f"{payload.card_id}.png", feed["width"], feed["height"])

    result = {"card_id": payload.card_id, "layout": layout, "reason": reason, "png": png}

    if card_cfg.get("_reels"):
        mhtml, _, _ = render_html(
            payload, card_cfg, cfg, motion=True, bg_variant=bg_variant
        )
        result["mp4"] = shoot_reel(
            mhtml,
            out_dir / f"{payload.card_id}.mp4",
            reels["width"],
            reels["height"],
            fps=reels["fps"],
            duration=reels["duration_sec"],
        )
    return result
