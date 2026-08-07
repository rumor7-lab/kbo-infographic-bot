#!/usr/bin/env python3
"""사진 답장 → 뉴스카드 렌더 → 텔레그램 승인 요청.

  python scripts/news_card.py --once      한 번만 확인 (테스트용)
  python scripts/news_card.py --watch     2분마다 계속 감시 (상주)

동작
  1. news_watch.py 가 보낸 알림 메시지에 사진으로 답장이 왔는지 확인한다.
     (data/topics/{message_id}.json 이 있고 status 가 waiting_photo 인 것만 대상)
  2. 답장의 캡션에서 헤드라인 문구를 읽는다 — 카드에 쓸 문구는 사람이 직접
     써야 한다(언론사 제목을 그대로 옮기면 저작권 문제가 된다. news_watch.py
     알림에도 "그대로 쓰지 말 것"이라고 이미 적어뒀다).

     캡션 형식 (사진과 함께 보내는 텔레그램 메시지의 캡션 칸에 입력):
       [단독] 김서현 볼넷 25개 남발       ← 1행. 대괄호는 선택(상단 노란 훅 문구로 분리됨)
       제구 완전히 무너졌다              ← 2행. 선택
       ©연합뉴스                         ← 사진 출처. 선택('©' 또는 '출처:' 로 시작)

  3. 사진을 내려받아 뉴스카드(유형 E)로 렌더하고, GitHub Release 에 올려 공개
     URL을 만든 뒤, 기존 텔레그램 승인 게이트(scripts/poll_telegram_approvals.py,
     GitHub Actions 5분 크론)로 승인 요청을 넘긴다. 이 스크립트는 승인/발행까지는
     하지 않는다 — 렌더해서 큐에 올리는 것까지가 역할이다.
  4. data/pending/*.json 은 GitHub Actions 워커가 읽어야 하므로 이 스크립트가
     직접 git commit + push 한다(로컬 실행이라 push 권한이 있다는 전제).

왜 캡션에 헤드라인을 쓰게 했나
  자동으로 기사 제목을 다듬어 쓰면 결국 언론사 표현을 손댄 것에 불과해 저작권
  문제가 남는다. 반면 뒤에 승인 게이트가 있으니 완벽한 자동 생성 문구가 아니어도
  된다 — 사람이 사진 고르는 김에 한 줄 카피도 같이 쓰는 편이 오히려 빠르고 안전하다.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.publish import captions, hosting  # noqa: E402
from src.publish import telegram as tg  # noqa: E402
from src.render.layout_engine import NewsCard, Payload  # noqa: E402
from src.render.renderer import load_cfg, render_card  # noqa: E402
from src.collect.news_trend import KST  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOPIC_DIR = ROOT / "data" / "topics"
PENDING = ROOT / "data" / "pending"
OFFSET_FILE = ROOT / "data" / "telegram_offset_card.json"
# 텔레그램에서 받은 사진 임시 저장소. assets/newsphotos/* 는 이미 .gitignore
# 대상이라(공개 저장소에 보도사진을 커밋하면 안 됨) 여기에 같이 둔다.
PHOTO_INBOX = ROOT / "assets" / "newsphotos" / "_inbox"

_HOOK_RE = re.compile(r"^\[(.+?)\]\s*(.*)$")
_CREDIT_RE = re.compile(r"^(?:©|출처\s*[:：]\s*)(.+)$")


def _load_env() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    import os

    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def _load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return json.loads(OFFSET_FILE.read_text(encoding="utf-8")).get("offset", 0)
        except json.JSONDecodeError:
            return 0
    return 0


def _save_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(json.dumps({"offset": offset}, ensure_ascii=False), encoding="utf-8")


def _load_topic(message_id: int) -> dict | None:
    p = TOPIC_DIR / f"{message_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _mark_topic(message_id: int, **fields) -> None:
    p = TOPIC_DIR / f"{message_id}.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d.update(fields)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _prune_topics(*, days: int = 3) -> None:
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


def _parse_caption(caption: str) -> dict[str, str]:
    """캡션에서 훅/헤드라인 2행/사진출처를 분리한다."""
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


def _git_commit_push(paths: list[str], message: str) -> bool:
    subprocess.run(["git", "add", *paths], cwd=ROOT, check=False)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if diff.returncode == 0:
        return True  # 커밋할 변경 없음
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=ROOT, check=True)
    r = subprocess.run(["git", "push"], cwd=ROOT)
    return r.returncode == 0


def _handle_photo_reply(cred: tg.Credentials, msg: dict, *, debug: bool, no_push: bool) -> bool:
    reply = msg.get("reply_to_message")
    photos = msg.get("photo")
    if not reply or not photos:
        return False

    topic = _load_topic(reply["message_id"])
    if topic is None:
        if debug:
            print(f"  · 답장 대상({reply['message_id']})에 해당하는 주제 없음 — 건너뜀")
        return False
    if topic.get("status") != "waiting_photo":
        if debug:
            print(f"  · 이미 처리된 주제({topic.get('key')}) — 건너뜀")
        return False

    caption = (msg.get("caption") or "").strip()
    if not caption:
        tg.send_message(
            cred,
            "사진은 받았는데 헤드라인 문구가 없어요. 사진 캡션에 헤드라인을 적어 다시 보내주세요.\n"
            "예)\n김서현 볼넷 25개 남발\n제구 완전히 무너졌다",
            reply_to_message_id=msg["message_id"],
        )
        return False

    parsed = _parse_caption(caption)
    if not parsed["line1"]:
        tg.send_message(cred, "헤드라인을 못 읽었어요. 캡션 형식을 확인해주세요.",
                         reply_to_message_id=msg["message_id"])
        return False

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

    _mark_topic(reply["message_id"], status="card_sent", card_id=card_id,
                sent_at=datetime.now(KST).isoformat())

    if not no_push:
        ok = _git_commit_push(
            ["data/pending", "data/topics"],
            f"news: {card_id} 카드 승인 요청",
        )
        if not ok:
            print("  ⚠ git push 실패 — 수동으로 git pull --no-edit && git push 해주세요"
                  " (안 하면 승인 버튼을 눌러도 발행이 안 됩니다)")

    return True


def check_once(*, debug: bool = False, no_push: bool = False) -> int:
    cred = tg.Credentials.from_env()
    offset = _load_offset()
    updates = tg.get_updates(cred, offset)
    processed = 0

    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        msg = u.get("message")
        if not msg:
            continue
        try:
            if _handle_photo_reply(cred, msg, debug=debug, no_push=no_push):
                processed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  처리 오류(계속 진행): {type(e).__name__}: {e}")

    _save_offset(offset)
    _prune_topics()

    if debug:
        print(f"[{datetime.now():%H:%M}] 업데이트 {len(updates)}건 확인 → 카드 {processed}건 제작")
    return processed


def main() -> int:
    ap = argparse.ArgumentParser(description="뉴스카드 사진 답장 처리")
    ap.add_argument("--once", action="store_true", help="한 번만 확인")
    ap.add_argument("--watch", action="store_true", help="주기적으로 계속 감시")
    ap.add_argument("--interval", type=int, default=2, help="감시 주기(분), 기본 2")
    ap.add_argument("--debug", action="store_true", help="처리 안 된 답장까지 이유를 출력")
    ap.add_argument("--no-push", action="store_true",
                     help="git commit/push 생략 (로컬 테스트용 — 이 상태로는 승인해도 발행 안 됨)")
    args = ap.parse_args()

    if not args.once and not args.watch:
        ap.error("--once 또는 --watch 중 하나를 지정하세요")

    _load_env()

    try:
        tg.Credentials.from_env()
    except tg.TelegramError as e:
        print(f"설정 오류: {e}")
        return 1

    kw = dict(debug=args.debug, no_push=args.no_push)

    if args.once:
        check_once(**kw)
        return 0

    print(f"카드 제작 감시 시작 — {args.interval}분 간격. 중지는 Ctrl+C")
    while True:
        try:
            check_once(**kw)
        except tg.TelegramError as e:
            print(f"  텔레그램 오류: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  오류(계속 진행): {type(e).__name__}: {e}")
        try:
            time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            print("\n감시 종료")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
