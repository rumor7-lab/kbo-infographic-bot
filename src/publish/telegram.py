"""텔레그램 발행 승인 어댑터.

카드를 렌더한 뒤 바로 인스타그램에 올리지 않고, 텔레그램 채팅으로 이미지+승인/거부
버튼을 보낸다. 사람이 버튼을 누르면(승인) 그때 실제 인스타그램 발행이 일어난다.

폴링 방식을 쓴다 (웹훅 서버 없음) — GitHub Actions 크론으로 몇 분 간격으로
getUpdates 를 불러서 콜백을 처리한다. 별도 상시 서버가 필요 없다는 게 이 방식의
핵심 장점이다(이 프로젝트는 이미 서버 없이 GitHub Actions + 정적 호스팅 조합으로
돌아가고 있어서, 여기에도 같은 원칙을 그대로 적용했다).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


@dataclass
class Credentials:
    bot_token: str
    chat_id: str

    @classmethod
    def from_env(cls) -> "Credentials":
        token = os.getenv("TG_BOT_TOKEN")
        chat_id = os.getenv("TG_CHAT_ID")
        if not token or not chat_id:
            raise TelegramError("TG_BOT_TOKEN / TG_CHAT_ID 환경변수가 없습니다")
        return cls(token, chat_id)


def _call(
    cred: Credentials, method: str, *, files: dict[str, Any] | None = None, **params: Any
) -> dict:
    r = requests.post(
        f"{API}/bot{cred.bot_token}/{method}", data=params, files=files, timeout=60
    )
    body = r.json()
    if not body.get("ok"):
        raise TelegramError(f"{method} 실패: {body}")
    return body["result"]


def _approval_keyboard(token: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ 승인", "callback_data": f"approve:{token}"},
                {"text": "❌ 거부", "callback_data": f"reject:{token}"},
            ]
        ]
    }


def send_photo_for_approval(
    cred: Credentials, image_url: str, caption: str, token: str, *, local_path: Path | None = None
) -> int:
    """이미지 카드를 승인 버튼과 함께 보낸다. 반환값은 message_id (나중에 수정용).

    local_path 가 있으면 텔레그램에 파일을 직접 업로드한다(멀티파트). GitHub Release
    다운로드 URL 은 리다이렉트/Content-Type(application/octet-stream) 특성 때문에
    텔레그램 서버가 원격 fetch 에 실패하는 경우가 있어("failed to get HTTP URL
    content"), 승인 미리보기 전송만큼은 URL 대신 로컬 바이트를 직접 올리는 쪽이 훨씬
    안정적이다. (인스타그램 발행용 공개 URL 은 별도로 이미 확보해둔 값을 그대로 쓴다.)
    """
    params = dict(
        chat_id=cred.chat_id, caption=caption,
        reply_markup=_reply_markup_json(_approval_keyboard(token)),
    )
    if local_path is not None:
        with open(local_path, "rb") as f:
            res = _call(cred, "sendPhoto", files={"photo": (local_path.name, f, "image/png")}, **params)
    else:
        res = _call(cred, "sendPhoto", photo=image_url, **params)
    return res["message_id"]


def send_video_for_approval(
    cred: Credentials, video_url: str, caption: str, token: str, *, local_path: Path | None = None
) -> int:
    params = dict(
        chat_id=cred.chat_id, caption=caption,
        reply_markup=_reply_markup_json(_approval_keyboard(token)),
    )
    if local_path is not None:
        with open(local_path, "rb") as f:
            res = _call(cred, "sendVideo", files={"video": (local_path.name, f, "video/mp4")}, **params)
    else:
        res = _call(cred, "sendVideo", video=video_url, **params)
    return res["message_id"]


def send_message(
    cred: Credentials, text: str, *, reply_to_message_id: int | None = None
) -> int:
    params: dict[str, Any] = {"chat_id": cred.chat_id, "text": text}
    if reply_to_message_id is not None:
        params["reply_to_message_id"] = reply_to_message_id
    res = _call(cred, "sendMessage", **params)
    return res["message_id"]


def edit_caption(cred: Credentials, message_id: int, new_caption: str) -> None:
    """결정 이후 메시지를 갱신하고 버튼을 없앤다(reply_markup 을 빈 값으로 덮어씀)."""
    try:
        _call(
            cred, "editMessageCaption",
            chat_id=cred.chat_id, message_id=message_id, caption=new_caption,
            reply_markup=_reply_markup_json({"inline_keyboard": []}),
        )
    except TelegramError:
        pass  # 메시지가 이미 지워졌거나 캡션이 동일해서 나는 에러는 무시해도 안전


def answer_callback(cred: Credentials, callback_query_id: str, text: str) -> None:
    try:
        _call(cred, "answerCallbackQuery", callback_query_id=callback_query_id, text=text)
    except TelegramError as e:
        print(f"DEBUG answer_callback 실패(무시됨): {e}")


def get_file_path(cred: Credentials, file_id: str) -> str:
    """파일 메타 조회 — 다운로드용 file_path 를 반환한다(최대 20MB 파일만 가능)."""
    res = _call(cred, "getFile", file_id=file_id)
    return res["file_path"]


def download_file(cred: Credentials, file_path: str, dest: Path) -> Path:
    """getFile 로 받은 file_path 를 실제 바이트로 내려받는다.

    봇 API 파일 다운로드는 bot{token}/ 경로가 아니라 별도의 file/bot{token}/
    엔드포인트를 쓴다 — 다른 메서드들과 URL 형태가 달라 헷갈리기 쉽다.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(f"{API}/file/bot{cred.bot_token}/{file_path}", timeout=60)
    if r.status_code != 200:
        raise TelegramError(f"파일 다운로드 실패 {r.status_code}: {r.text[:200]}")
    dest.write_bytes(r.content)
    return dest


def get_updates(cred: Credentials, offset: int) -> list[dict]:
    res = requests.post(
        f"{API}/bot{cred.bot_token}/getUpdates",
        data={"offset": offset, "timeout": 0},
        timeout=30,
    ).json()
    if not res.get("ok"):
        raise TelegramError(f"getUpdates 실패: {res}")
    return res["result"]


def _reply_markup_json(markup: dict) -> str:
    import json

    return json.dumps(markup, ensure_ascii=False)
