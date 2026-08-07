"""사진 답장 → 뉴스카드 렌더 → 승인 큐. scripts/news_card.py, scripts/poll_telegram_approvals.py 공용.

왜 별도 모듈로 뺐나
  처음엔 scripts/news_card.py 가 자기 offset 파일로 텔레그램 getUpdates 를
  직접 폴링했다. 그런데 텔레그램의 getUpdates offset 은 '호출한 스크립트별'이
  아니라 봇 전체 기준으로 전역이다 — offset 을 지정해 호출하는 순간 그보다
  작은 update_id 는 서버에서 지워져서 그 이후로는 *누구에게도* 다시 안 온다.
  이미 승인 처리용으로 scripts/poll_telegram_approvals.py 가 GitHub Actions
  5분 크론에서 자기 offset(data/telegram_offset.json)으로 getUpdates 를 계속
  부르고 있었기 때문에, 로컬 news_card.py 가 뭘 보기도 전에 크론이 먼저
  업데이트를 '소비'해버리는 경쟁이 실측에서 그대로 발생했다(사진 답장을
  보냈는데 로컬 스크립트엔 0건으로 나옴).

  봇 하나에 getUpdates 소비자는 하나여야 한다. 그래서 실제 운영 경로는
  poll_telegram_approvals.py 하나로 합치고, 이 모듈은 그 안에서 호출하는
  '사진 답장 처리' 로직만 담는다(git 커밋/오프셋 관리는 호출부 책임 —
  이미 poll_telegram_approvals.py 가 한 번에 처리한다). scripts/news_card.py
  는 로컬에서 단독으로 돌려보는 수동 테스트용으로만 남겨둔다(운영 중엔
  GitHub Actions 와 offset 을 두고 경쟁하므로 --watch 로 상시 실행하면 안 됨).
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from src.collect.news_trend import KST
from src.publish import captions, hosting
from src.publish import telegram as tg
from src.render.layout_engine import NewsCard, Payload
from src.render.renderer import load_cfg, render_card

ROOT = Path(__file__).resolve().parents[1]
TOPIC_DIR = ROOT / "data" / "topics"
PENDING = ROOT / "data" / "pending"
# 텔레그램에서 받은 사진 임시 저장소. assets/newsphotos/* 는 이미 .gitignore
# 대상이라(공개 저장소에 보도사진을 커밋하면 안 됨) 여기에 같이 둔다.
PHOTO_INBOX = ROOT / "assets" / "newsphotos" / "_inbox"

_HOOK_RE = re.compile(r"^\[(.+?)\]\s*(.*)$")
_CREDIT_RE = re.compile(r"^(?:©|출처\s*[:：]\s*)(.+)$")


def load_topic(message_id: int) -> dict | None:
    p = TOPIC_DIR / f"{message_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def mark_topic(message_id: int, **fields) -> None:
    p = TOPIC_DIR / f"{message_id}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d.update(fields)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def prune_topics(*, days: int = 3) -> None:
    """며칠 지난 주제 파일은 지운다 — 그만큼 지난 사진 답장은 어차피 의미가 없다."""
    if not TOPIC_DIR.exists():
        return
    cutoff = datetime.now(KST) - timedelta(days=days)
    for p in TOPIC_DIR.glob("*.json"):
        try:
            created = datetime.fromisoformat(json.loads(p.read_text(encoding="utf-8"))["created_at"])
        except Exception:  # noqa: BLE001
            continue
        if created < cutoff:
            p.unlink(missing_ok=True)


def parse_caption(caption: str) -> dict[str, str]:
    """캡션에서 훅/헤드라인 2행/사진출처를 분리한다.

    형식:
      [단독] 김서현 볼넷 25개 남발       ← 1행. 대괄호는 선택(상단 노란 훅 문구로 분리됨)
      제구 완전히 무너졌다              ← 2행. 선택
      ©연합뉴스                         ← 사진 출처. 선택('©' 또는 '출처:' 로 시작)
    """
    lines = [ln.strip() for ln in caption.splitlines() if ln.strip()]

    credit = ""
    text_lines = []
    for ln in lines:
        m = _CREDIT_RE.match(ln)
        if m:
            credit = m.group(1).strip()
            continue
        text_lines.append(ln)

    hook = ""
    if text_lines:
        m = _HOOK_RE.match(text_lines[0])
        if m:
            hook = m.group(1).strip()
            rest = m.group(2).strip()
            if rest:
                text_lines[0] = rest
            else:
                text_lines.pop(0)

    return {
        "hook": hook,
        "line1": text_lines[0] if text_lines else "",
        "line2": text_lines[1] if len(text_lines) > 1 else "",
        "credit": credit,
    }


def handle_photo_reply(cred: tg.Credentials, msg: dict, *, debug: bool = False) -> bool:
    """사진 답장 메시지 하나를 처리 — 대상 아니면 False, 카드까지 만들었으면 True.

    git add/commit/push 은 여기서 하지 않는다 — 호출부(poll_telegram_approvals.py)가
    같은 실행에서 승인 상태와 한꺼번에 커밋한다. offset 저장도 호출부 책임이다.
    """
    reply = msg.get("reply_to_message")
    photos = msg.get("photo")
    if not reply or not photos:
        return False

    topic = load_topic(reply["message_id"])
    if topic is None:
        if debug:
            print(f"  · 답장 대상({reply['message_id']})에 해당하는 주제 없음 — 건너뜀")
        return False
    if topic.get("status") != "waiting_photo":
        if debug:
            print(f"  · 이미 처리된 주제({topic.get('key')}) — 건너뜀")
        return False

    caption = (msg.get("caption") or "").strip()
    caption_body = ""  # 인스타 캡션에 들어갈 기사체 본문 — AI 초안에만 있음

    if caption:
        # 사람이 직접 캡션을 썼다 — 이게 항상 AI 초안보다 우선한다.
        parsed = parse_caption(caption)
        if not parsed["line1"]:
            tg.send_message(cred, "헤드라인을 못 읽었어요. 캡션 형식을 확인해주세요.",
                             reply_to_message_id=msg["message_id"])
            return False
    else:
        # 캡션 없이 사진만 왔다 — 알림 보낼 때 만들어둔 AI 초안이 있으면 그걸 쓴다.
        draft = topic.get("draft") or {}
        if not draft.get("line1"):
            tg.send_message(
                cred,
                "사진은 받았는데 헤드라인 문구가 없어요(AI 초안도 없음). "
                "사진 캡션에 헤드라인을 적어 다시 보내주세요.\n"
                "예)\n김서현 볼넷 25개 남발\n제구 완전히 무너졌다",
                reply_to_message_id=msg["message_id"],
            )
            return False
        parsed = {
            "hook": draft.get("hook", ""), "line1": draft["line1"],
            "line2": draft.get("line2", ""), "credit": "",
        }
        caption_body = draft.get("caption_body", "")

    print(f"  🖼  카드 제작: {topic.get('key')} — \"{parsed['line1']}\"")

    # 가장 큰 해상도(배열 마지막)를 받는다
    file_id = photos[-1]["file_id"]
    file_path = tg.get_file_path(cred, file_id)
    ext = Path(file_path).suffix or ".jpg"
    local_photo = PHOTO_INBOX / f"{topic['topic_id']}_{reply['message_id']}{ext}"
    tg.download_file(cred, file_path, local_photo)

    cfg = load_cfg()
    news = NewsCard(
        line1=parsed["line1"], line2=parsed["line2"], hook=parsed["hook"],
        photo=str(local_photo), photo_credit=parsed["credit"],
    )
    card_id = f"news_{topic['topic_id']}"
    payload = Payload(card_id=card_id, title=parsed["line1"], news=news)
    out_dir = ROOT / "out" / "news" / datetime.now(KST).strftime("%Y%m%d_%H%M%S")

    try:
        r = render_card(payload, {"layout": "newscard"}, cfg, out_dir)
    except Exception as e:  # noqa: BLE001
        tg.send_message(cred, f"카드 렌더 실패: {e}", reply_to_message_id=msg["message_id"])
        return False

    url = hosting.upload(r["png"])
    ig_caption = captions.build_news(
        parsed["line1"], parsed["line2"],
        team=topic.get("team"), outlet_count=topic.get("outlet_count", 0),
        body=caption_body,
    )
    tg_preview = f"[{card_id}]\n" + (ig_caption if len(ig_caption) <= 900 else ig_caption[:900] + "…")

    token = secrets.token_hex(4)
    approval_mid = tg.send_photo_for_approval(cred, url, tg_preview, token, local_path=r["png"])

    batch_id = f"{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}_news"
    batch = {
        "batch_id": batch_id, "slot": "news", "caption": ig_caption,
        "carousel": False, "reels": False,
        "created_at": datetime.now(KST).isoformat(), "status": "pending",
        "cards": [{
            "token": token, "card_id": card_id, "kind": "image",
            "url": url, "message_id": approval_mid, "status": "pending",
        }],
    }
    PENDING.mkdir(parents=True, exist_ok=True)
    (PENDING / f"{batch_id}.json").write_text(
        json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tg.send_message(cred, "카드를 만들었어요 — 위 메시지 버튼으로 승인/거부해주세요.",
                     reply_to_message_id=msg["message_id"])

    mark_topic(reply["message_id"], status="card_sent", card_id=card_id,
               sent_at=datetime.now(KST).isoformat())
    return True
