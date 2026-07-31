"""선수 사진 자동 수집 + 품질 게이트.

뉴스 사진 자동 크롤링은 최신성은 최고지만 무인 운영에서 사고가 잦다.
그래서 '수집'보다 '거르기'에 코드를 더 썼다.

품질 게이트 5단계
  1. 해상도   — 1080 폭 캔버스에 쓸 수 있는 크기인가
  2. 얼굴 검출 — 사람이 1명만 크게 잡히는가 (단체샷·그래픽·로고 이미지 제거)
  3. 인물 위치 — 얼굴이 상단 1/2 안에 있는가 (히어로 레이아웃 전제)
  4. 화질     — 블러 스코어(라플라시안 분산)가 기준 이상인가
  5. 캐시 승인 — 통과한 사진은 선수별로 캐시. 매번 새로 뽑지 않는다(오인 위험 감소)

게이트를 모두 통과하지 못하면 None 을 반환한다. 이때 레이아웃 엔진이
유형 C → A/B 로 자동 강등하므로 발행 자체는 멈추지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "assets" / "photos"
MANIFEST = CACHE_DIR / "_manifest.json"
REVIEW_QUEUE = ROOT / "data" / "photo_review.json"

# 품질 기준
MIN_WIDTH = 900
MIN_HEIGHT = 600
MIN_FACE_RATIO = 0.045      # 얼굴 면적 / 전체 면적
MAX_FACES = 1
MIN_SHARPNESS = 60.0        # 라플라시안 분산
CACHE_TTL_DAYS = 21         # 이 기간 지나면 새 사진 시도
MAX_CANDIDATES = 8

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 야구 사진이 많은 도메인 우선. 여기 없는 도메인은 감점.
PREFERRED_HOSTS = (
    "img.sportsworldi.com", "image.newsis.com", "img.hankyung.com",
    "imgnews.pstatic.net", "photo.jtbc.co.kr", "image.chosun.com",
    "cdn.spotvnews.co.kr", "img.osen.co.kr", "www.spotvnews.co.kr",
)


@dataclass
class Photo:
    player: str
    path: str
    source_url: str
    width: int
    height: int
    faces: int
    sharpness: float
    face_center: tuple[float, float]   # (x, y) 0~1 비율
    fetched_at: str

    @property
    def css_position(self) -> str:
        """얼굴이 잘리지 않도록 background-position 값을 계산."""
        x, y = self.face_center
        # 얼굴을 상단 1/4 지점에 두는 것이 히어로 레이아웃에서 가장 안정적
        return f"{round(x * 100)}% {round(max(0.0, min(y * 100 - 8, 60)))}%"


_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
_last = 0.0


def _polite() -> None:
    global _last
    w = 1.5 - (time.time() - _last)
    if w > 0:
        time.sleep(w)
    _last = time.time()


# ── 캐시 ─────────────────────────────────────────
def _load_manifest() -> dict[str, Any]:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def _save_manifest(m: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def _cached(player: str) -> Photo | None:
    m = _load_manifest()
    rec = m.get(player)
    if not rec:
        return None
    p = Path(rec["path"])
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return None
    age = datetime.now(KST) - datetime.fromisoformat(rec["fetched_at"])
    if age > timedelta(days=CACHE_TTL_DAYS):
        return None
    rec = dict(rec)
    rec["path"] = str(p)
    rec["face_center"] = tuple(rec["face_center"])
    return Photo(**rec)


# ── 후보 검색 ────────────────────────────────────
def _search_candidates(player: str, team: str | None) -> list[str]:
    """뉴스 이미지 후보 URL 수집. 검색 경로는 언제든 바뀔 수 있으니 실패에 관대하게."""
    q = f"{team + ' ' if team else ''}{player} 야구"
    urls: list[str] = []

    endpoints = [
        f"https://search.naver.com/search.naver?where=image&query={quote(q)}",
        f"https://search.naver.com/search.naver?where=news&query={quote(q)}",
    ]
    for ep in endpoints:
        try:
            _polite()
            r = _session.get(ep, timeout=15)
            r.raise_for_status()
        except requests.RequestException:
            continue
        found = re.findall(r'https?://[^\s"\'<>]+?\.(?:jpg|jpeg|png)', r.text, re.I)
        urls.extend(found)
        if len(urls) >= MAX_CANDIDATES * 4:
            break

    # 정렬: 선호 도메인 우선, 썸네일 의심 URL 후순위
    def score(u: str) -> tuple[int, int]:
        host = urlparse(u).netloc
        pref = 0 if any(h in host for h in PREFERRED_HOSTS) else 1
        thumb = 1 if re.search(r"(thumb|small|_s\.|/60/|/100/|icon)", u, re.I) else 0
        return (pref, thumb)

    seen, ordered = set(), []
    for u in sorted(dict.fromkeys(urls), key=score):
        h = hashlib.md5(u.encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        ordered.append(u)
    return ordered[: MAX_CANDIDATES * 3]


# ── 품질 게이트 ──────────────────────────────────
def _inspect(data: bytes) -> dict[str, Any] | None:
    """OpenCV 로 해상도·얼굴·화질 검사. 통과 못하면 None."""
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise RuntimeError("opencv-python 이 필요합니다: pip install opencv-python") from e

    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    if w < MIN_WIDTH or h < MIN_HEIGHT:
        return None

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if sharpness < MIN_SHARPNESS:
        return None

    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    faces = cascade.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=6, minSize=(70, 70))
    if len(faces) == 0 or len(faces) > MAX_FACES:
        return None

    fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
    if (fw * fh) / (w * h) < MIN_FACE_RATIO:
        return None

    cy = (fy + fh / 2) / h
    if cy > 0.55:                      # 얼굴이 하단에 있으면 히어로 레이아웃에서 가려진다
        return None

    return {
        "width": w,
        "height": h,
        "faces": int(len(faces)),
        "sharpness": round(sharpness, 1),
        "face_center": ((fx + fw / 2) / w, cy),
    }


def _queue_review(photo: Photo) -> None:
    """새로 채택된 사진은 리뷰 큐에 남긴다. 무인 운영이라도 사후 확인은 필요하다."""
    REVIEW_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    q = json.loads(REVIEW_QUEUE.read_text(encoding="utf-8")) if REVIEW_QUEUE.exists() else []
    q.append({**asdict(photo), "reviewed": False})
    REVIEW_QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")


def get_photo(player: str, team: str | None = None) -> Photo | None:
    """선수 사진을 반환. 실패 시 None → 레이아웃 자동 강등."""
    if not player:
        return None

    hit = _cached(player)
    if hit:
        return hit

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for url in _search_candidates(player, team):
        try:
            _polite()
            r = _session.get(url, timeout=15)
            r.raise_for_status()
            data = r.content
        except requests.RequestException:
            continue
        if len(data) < 40_000:          # 40KB 미만은 썸네일
            continue

        info = _inspect(data)
        if info is None:
            continue

        safe = re.sub(r"[^\w가-힣]", "", player) or hashlib.md5(player.encode()).hexdigest()[:8]
        path = CACHE_DIR / f"{safe}.jpg"
        path.write_bytes(data)

        photo = Photo(
            player=player,
            path=str(path),
            source_url=url,
            fetched_at=datetime.now(KST).isoformat(),
            **info,
        )
        m = _load_manifest()
        m[player] = {**asdict(photo), "path": str(path.relative_to(ROOT))}
        _save_manifest(m)
        _queue_review(photo)
        return photo

    return None


def blocklist_add(player: str) -> None:
    """오인된 사진을 발견하면 캐시에서 제거. 다음 실행에서 다시 찾는다."""
    m = _load_manifest()
    rec = m.pop(player, None)
    if rec:
        p = ROOT / rec["path"]
        p.unlink(missing_ok=True)
        _save_manifest(m)
