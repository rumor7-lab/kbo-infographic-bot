"""뉴스카드용 사진 공급 — 사용자가 직접 넣은 사진만 쓴다.

왜 자동 수집을 안 하는가
  1. 저작권. 보도사진은 언론사가 저작권을 가진 업무상저작물이고, 인스타그램은
     저작권 신고가 계정 정지로 이어질 수 있다. 자동으로 긁으면 하루 몇 장씩
     기계적으로 쌓이고 공개 저장소에 이력까지 남아 리스크가 선형으로 커진다.
  2. 품질. 이 포맷은 사진이 헤드라인의 감정선과 맞아야 후킹이 산다
     ("노려보는 장면", "고개 숙인 뒷모습"). 자동 선택은 이걸 못 한다.

운영 방식
  assets/newsphotos/ 에 파일을 넣어두면 파일명으로 매칭한다.
    <slug>.jpg              → 기본
    <slug>__center-30.jpg   → background-position 지정 (center 30%)
    <slug>__연합뉴스.jpg     → 사진 출처 표기
    <slug>__center-30__연합뉴스.jpg  → 둘 다

  slug 는 카드가 요청하는 키(보통 선수명 또는 주제 키워드)를 그대로 쓴다.
  예) assets/newsphotos/원태인.jpg
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHOTO_DIR = ROOT / "assets" / "newsphotos"

EXTS = (".jpg", ".jpeg", ".png", ".webp")

# 파일명 옵션 토큰: "center-30" → "center 30%"
_POS_RE = re.compile(r"^(top|center|bottom|left|right)-(\d{1,3})$")


@dataclass
class NewsPhoto:
    path: Path
    css_position: str | None = None
    credit: str = ""


def _norm(s: str) -> str:
    """한글 자모 분리(NFD) 차이로 매칭이 어긋나는 걸 막는다.

    macOS 에서 만든 파일명은 NFD 로 저장되는 경우가 있어 파이썬 문자열의 NFC 와
    바이트가 달라진다 — 눈에는 같아 보여도 == 비교가 실패한다.
    """
    return unicodedata.normalize("NFC", s).strip().lower().replace(" ", "")


def _parse_options(tokens: list[str]) -> tuple[str | None, str]:
    """파일명의 __ 뒤 토큰들에서 (css_position, credit) 추출."""
    pos: str | None = None
    credit_parts: list[str] = []
    for tok in tokens:
        m = _POS_RE.match(tok.strip().lower())
        if m:
            pos = f"{m.group(1)} {m.group(2)}%"
        elif tok.strip():
            credit_parts.append(tok.strip())
    return pos, " ".join(credit_parts)


def get_photo(slug: str, *, photo_dir: Path | None = None) -> NewsPhoto | None:
    """slug 에 해당하는 사진을 찾는다. 없으면 None (카드는 스킵된다)."""
    d = photo_dir or PHOTO_DIR
    if not d.exists():
        return None

    target = _norm(slug)
    for p in sorted(d.iterdir()):
        if p.suffix.lower() not in EXTS or not p.is_file():
            continue
        stem, _, rest = p.stem.partition("__")
        if _norm(stem) != target:
            continue
        pos, credit = _parse_options(rest.split("__")) if rest else (None, "")
        return NewsPhoto(path=p, css_position=pos, credit=credit)
    return None


def available_slugs(photo_dir: Path | None = None) -> list[str]:
    """현재 넣어둔 사진 목록 — 어떤 카드를 만들 수 있는지 확인용."""
    d = photo_dir or PHOTO_DIR
    if not d.exists():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in EXTS and p.is_file():
            out.append(p.stem.partition("__")[0])
    return out
