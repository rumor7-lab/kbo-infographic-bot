"""Instagram Graph API 발행 어댑터.

확인된 제약 (2026 기준)
  - 비즈니스/크리에이터 계정 + FB 페이지 연결 필수  → 이미 완료된 상태
  - 미디어는 '공개 접근 가능한 URL' 이어야 한다 (로컬 파일 직접 업로드 불가)
  - 캐러셀 2~10장, 캐러셀 전체가 1건으로 계산
  - 24시간 이동 기준 공식 100건. 하루 3건 발행에는 여유가 크다
  - 장기 토큰도 만료됨 → 주기적 갱신 필수. 파이프라인이 조용히 죽는 1순위 원인

발행 흐름
  1. 로컬 PNG/MP4 → 공개 URL 확보 (GitHub Release 또는 R2 업로드)
  2. 컨테이너 생성 (이미지/비디오/캐러셀 자녀)
  3. (비디오/릴스) 상태가 FINISHED 될 때까지 폴링
  4. media_publish
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import requests

API_VERSION = os.getenv("IG_API_VERSION", "v21.0")
GRAPH = f"https://graph.facebook.com/{API_VERSION}"

MediaKind = Literal["image", "reels"]


class PublishError(RuntimeError):
    pass


@dataclass
class Credentials:
    ig_user_id: str
    access_token: str

    @classmethod
    def from_env(cls) -> "Credentials":
        uid = os.getenv("IG_USER_ID")
        tok = os.getenv("IG_ACCESS_TOKEN")
        if not uid or not tok:
            raise PublishError("IG_USER_ID / IG_ACCESS_TOKEN 환경변수가 없습니다")
        return cls(uid, tok)


def _post(path: str, cred: Credentials, **params: Any) -> dict:
    params["access_token"] = cred.access_token
    r = requests.post(f"{GRAPH}/{path}", data=params, timeout=60)
    if r.status_code >= 400:
        raise PublishError(f"POST {path} 실패 {r.status_code}: {r.text[:400]}")
    return r.json()


def _get(path: str, cred: Credentials, **params: Any) -> dict:
    params["access_token"] = cred.access_token
    r = requests.get(f"{GRAPH}/{path}", params=params, timeout=60)
    if r.status_code >= 400:
        raise PublishError(f"GET {path} 실패 {r.status_code}: {r.text[:400]}")
    return r.json()


# ── 컨테이너 ─────────────────────────────────────
def create_image_container(
    cred: Credentials, image_url: str, *, caption: str | None = None, carousel_child: bool = False
) -> str:
    params: dict[str, Any] = {"image_url": image_url}
    if carousel_child:
        params["is_carousel_item"] = "true"
    elif caption:
        params["caption"] = caption
    return _post(f"{cred.ig_user_id}/media", cred, **params)["id"]


def create_reels_container(cred: Credentials, video_url: str, caption: str) -> str:
    return _post(
        f"{cred.ig_user_id}/media",
        cred,
        media_type="REELS",
        video_url=video_url,
        caption=caption,
        share_to_feed="true",
    )["id"]


def create_carousel_container(cred: Credentials, children: list[str], caption: str) -> str:
    if not (2 <= len(children) <= 10):
        raise PublishError(f"캐러셀은 2~10장이어야 합니다 (현재 {len(children)}장)")
    return _post(
        f"{cred.ig_user_id}/media",
        cred,
        media_type="CAROUSEL",
        children=",".join(children),
        caption=caption,
    )["id"]


def wait_ready(cred: Credentials, container_id: str, *, timeout: int = 300) -> None:
    """비디오는 서버 인코딩이 끝나야 발행 가능. 폴링 없이 publish 하면 실패한다."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _get(container_id, cred, fields="status_code,status")
        code = st.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise PublishError(f"컨테이너 처리 실패: {st}")
        time.sleep(6)
    raise PublishError(f"컨테이너 처리 타임아웃({timeout}s): {container_id}")


def publish(cred: Credentials, container_id: str) -> str:
    res = _post(f"{cred.ig_user_id}/media_publish", cred, creation_id=container_id)
    return res["id"]


def remaining_quota(cred: Credentials) -> int | None:
    """24시간 발행 잔여 수. 하루 3건 운영이면 여유롭지만 로그로 남겨둔다."""
    try:
        r = _get(f"{cred.ig_user_id}/content_publishing_limit", cred,
                 fields="quota_usage,config")
        data = r.get("data", [{}])[0]
        used = data.get("quota_usage", 0)
        cap = data.get("config", {}).get("quota_total", 100)
        return max(cap - used, 0)
    except PublishError:
        return None


# ── 상위 API ─────────────────────────────────────
def post_carousel(cred: Credentials, image_urls: list[str], caption: str) -> str:
    children = [create_image_container(cred, u, carousel_child=True) for u in image_urls]
    parent = create_carousel_container(cred, children, caption)
    wait_ready(cred, parent)
    return publish(cred, parent)


def post_single(cred: Credentials, image_url: str, caption: str) -> str:
    cid = create_image_container(cred, image_url, caption=caption)
    wait_ready(cred, cid)
    return publish(cred, cid)


def post_reel(cred: Credentials, video_url: str, caption: str) -> str:
    cid = create_reels_container(cred, video_url, caption)
    wait_ready(cred, cid, timeout=600)
    return publish(cred, cid)


# ── 토큰 갱신 ────────────────────────────────────
def refresh_long_lived_token(app_id: str, app_secret: str, token: str) -> dict:
    """장기 토큰 재발급. GitHub Actions 로 주 1회 돌리고 결과를 시크릿에 반영한다."""
    r = requests.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": token,
        },
        timeout=30,
    )
    if r.status_code >= 400:
        raise PublishError(f"토큰 갱신 실패: {r.text[:400]}")
    return r.json()


def token_days_left(cred: Credentials) -> int | None:
    try:
        r = requests.get(
            f"{GRAPH}/debug_token",
            params={"input_token": cred.access_token, "access_token": cred.access_token},
            timeout=30,
        ).json()
        exp = r.get("data", {}).get("expires_at")
        if not exp:
            return None
        return max(int((exp - time.time()) // 86400), 0)
    except Exception:
        return None
