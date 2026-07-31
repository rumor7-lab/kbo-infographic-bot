"""로컬 파일 → 공개 URL.

Instagram Graph API 는 '공개 접근 가능한 URL' 만 받는다. 로컬 PNG 를 직접 못 올린다.
별도 스토리지 없이 GitHub Release 에이셋을 공개 CDN 처럼 쓰는 방식을 기본으로 한다.

backend
  github  : GitHub Release 에 업로드 (추가 비용 0, 토큰만 필요) ← 기본
  r2      : Cloudflare R2 / S3 호환 (트래픽 늘어나면 전환)
  local   : 개발용. 업로드 없이 file:// 경로 반환 (발행은 못 함)
"""

from __future__ import annotations

import mimetypes
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")


class HostingError(RuntimeError):
    pass


def upload(path: Path, *, backend: str | None = None) -> str:
    backend = backend or os.getenv("MEDIA_BACKEND", "github")
    if backend == "github":
        return _github_release(path)
    if backend == "r2":
        return _r2(path)
    if backend == "local":
        return path.resolve().as_uri()
    raise HostingError(f"알 수 없는 backend: {backend}")


# ── GitHub Release ───────────────────────────────
def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_release(path: Path) -> str:
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")  # "owner/repo"
    if not token or not repo:
        raise HostingError("GH_TOKEN / GITHUB_REPOSITORY 환경변수가 필요합니다")

    # 일자별 릴리스 하나에 그날 자산을 모아둔다
    tag = f"media-{datetime.now(KST).strftime('%Y%m%d')}"
    api = f"https://api.github.com/repos/{repo}"
    h = _gh_headers(token)

    r = requests.get(f"{api}/releases/tags/{tag}", headers=h, timeout=30)
    if r.status_code == 404:
        r = requests.post(
            f"{api}/releases",
            headers=h,
            json={"tag_name": tag, "name": tag, "body": "auto-generated media", "prerelease": True},
            timeout=30,
        )
    if r.status_code >= 400:
        raise HostingError(f"릴리스 준비 실패 {r.status_code}: {r.text[:300]}")
    rel = r.json()

    # 같은 이름 자산이 있으면 지우고 새로 올린다 (재실행 대응)
    name = f"{datetime.now(KST).strftime('%H%M%S')}_{path.name}"
    for a in rel.get("assets", []):
        if a["name"] == name:
            requests.delete(f"{api}/releases/assets/{a['id']}", headers=h, timeout=30)

    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    up = rel["upload_url"].split("{")[0]
    r = requests.post(
        f"{up}?name={name}",
        headers={**h, "Content-Type": ctype},
        data=path.read_bytes(),
        timeout=180,
    )
    if r.status_code >= 400:
        raise HostingError(f"자산 업로드 실패 {r.status_code}: {r.text[:300]}")
    return r.json()["browser_download_url"]


# ── Cloudflare R2 / S3 ───────────────────────────
def _r2(path: Path) -> str:
    try:
        import boto3
    except ImportError as e:
        raise HostingError("boto3 가 필요합니다: pip install boto3") from e

    bucket = os.environ["R2_BUCKET"]
    public_base = os.environ["R2_PUBLIC_BASE"].rstrip("/")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    key = f"{datetime.now(KST).strftime('%Y/%m/%d')}/{path.name}"
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    client.put_object(
        Bucket=bucket, Key=key, Body=path.read_bytes(), ContentType=ctype
    )
    return f"{public_base}/{key}"
