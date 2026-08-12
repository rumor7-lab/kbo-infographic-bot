#!/usr/bin/env python3
"""텔레그램 승인 폴링 — 몇 분 간격 크론으로 돌린다 (서버 상시 대기 없음).

흐름
  1. data/telegram_offset.json 에 저장해둔 offset 부터 getUpdates 로 업데이트를 가져온다.
  2. "approve:<token>" / "reject:<token>" 콜백을 data/pending/*.json 배치에서 찾아
     해당 카드의 상태를 갱신하고, 텔레그램 메시지를 갱신해 버튼을 없앤다.
  3. 배치 안의 모든 카드가 결정됐으면(더 이상 pending 없음) 그제서야 실제 인스타그램
     발행을 수행한다 — 승인된 카드만 묶어서(캐러셀/단일/릴스) 올린다.
  4. 사진이 첨부된 답장 메시지는 src/news_pipeline.py 에 넘겨 뉴스카드로 렌더하고
     승인 큐(3)에 새로 등록한다.

GitHub Actions 크론으로 이 스크립트만 반복 실행하면 되고, 상태는 매번 git commit
으로 저장소에 남겨서 다음 실행에서 이어받는다(러너가 매번 새로 뜨는 걸 전제).

왜 사진 답장 처리까지 이 스크립트가 다 하나
  텔레그램 getUpdates 의 offset 은 봇 전체 기준 전역이다 — 어떤 클라이언트든
  offset 을 지정해 부르면 그보다 작은 update_id 는 서버에서 지워져서 다른
  누구에게도 다시 안 온다. 그래서 이 스크립트(5분 크론)와 로컬 스크립트가
  각자 자기 offset 으로 따로 폴링하면 아예 경쟁이 생긴다 — 로컬이 미처 보기
  전에 이 크론이 먼저 업데이트를 '소비'해버려서 로컬엔 0건으로 보이는 사고가
  실측에서 났다(sha 참고: 로컬 news_card.py 최초 버전). 봇 하나엔 getUpdates
  소비자가 하나여야 하므로, 사진 답장 처리도 이 스크립트로 합쳤다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import news_pipeline as ncard  # noqa: E402
from src.publish import hosting  # noqa: E402  (미사용이지만 재발행 확장 대비 임포트 유지)
from src.publish import instagram as ig  # noqa: E402
from src.publish import telegram as tg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PENDING = ROOT / "data" / "pending"
OFFSET_FILE = ROOT / "data" / "telegram_offset.json"


def _load_offset() -> int:
    if OFFSET_FILE.exists():
        return json.loads(OFFSET_FILE.read_text(encoding="utf-8")).get("offset", 0)
    return 0


def _save_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(json.dumps({"offset": offset}, ensure_ascii=False), encoding="utf-8")


def _load_batches() -> list[Path]:
    if not PENDING.exists():
        return []
    return sorted(PENDING.glob("*.json"))


def _find_card(batch: dict, token: str) -> dict | None:
    for c in batch["cards"]:
        if c["token"] == token:
            return c
    return None


def _card_status_line(card: dict) -> str:
    mark = {"approved": "✅ 승인됨", "rejected": "❌ 거부됨", "pending": "⏳ 대기중"}
    return f"[{card['card_id']}] {mark.get(card['status'], card['status'])}"


def _finalize_batch(batch_path: Path, batch: dict, cred_tg: tg.Credentials) -> None:
    approved = [c for c in batch["cards"] if c["status"] == "approved"]

    if not approved:
        batch["status"] = "discarded"
        tg.send_message(cred_tg, f"'{batch['slot']}' 슬롯 — 전부 거부되어 발행하지 않습니다.")
        batch_path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    try:
        cred_ig = ig.Credentials.from_env()
        if batch["reels"]:
            media_id = ig.post_reel(cred_ig, approved[0]["url"], batch["caption"])
        elif batch["carousel"] and len(approved) >= 2:
            media_id = ig.post_carousel(cred_ig, [c["url"] for c in approved], batch["caption"])
        else:
            media_id = ig.post_single(cred_ig, approved[0]["url"], batch["caption"])
        batch["status"] = "published"
        batch["media_id"] = media_id
        tg.send_message(
            cred_tg,
            f"✅ '{batch['slot']}' 슬롯 발행 완료 — 승인된 {len(approved)}장 "
            f"(media_id={media_id})",
        )
    except Exception as e:  # noqa: BLE001
        batch["status"] = "publish_failed"
        batch["error"] = str(e)
        tg.send_message(cred_tg, f"⚠️ '{batch['slot']}' 슬롯 발행 실패: {e}")

    batch_path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    try:
        cred_tg = tg.Credentials.from_env()
    except tg.TelegramError as e:
        # TG_BOT_TOKEN/TG_CHAT_ID 시크릿을 아직 등록 전이면 5분마다 워크플로가
        # 빨갛게 실패하며 알림 메일이 쌓인다 — 설정 전엔 조용히 스킵한다.
        print(f"텔레그램 자격증명 미설정, 건너뜀: {e}")
        return 0
    offset = _load_offset()

    updates = tg.get_updates(cred_tg, offset)
    processed = 0
    cards_made = 0

    for u in updates:
        offset = max(offset, u["update_id"] + 1)

        # 사진 답장(뉴스카드 소재) — 콜백이 아니라 일반 메시지로 온다.
        msg = u.get("message")
        if msg:
            try:
                if ncard.handle_photo_reply(cred_tg, msg):
                    cards_made += 1
            except Exception as e:  # noqa: BLE001
                print(f"  뉴스카드 처리 오류(계속 진행): {type(e).__name__}: {e}")
            continue

        cq = u.get("callback_query")
        if not cq:
            continue
        data = cq.get("data", "")
        action, _, token = data.partition(":")
        print(f"  콜백 수신: update_id={u['update_id']} data={data!r}")
        if action not in ("approve", "reject") or not token:
            print(f"    → 형식 불일치(action={action!r}, token={token!r}), 무시")
            continue

        matched = False
        seen_token_elsewhere = False  # 토큰은 찾았는데 이미 결정됐거나 배치가 안 pending
        for batch_path in _load_batches():
            batch = json.loads(batch_path.read_text(encoding="utf-8"))
            card = _find_card(batch, token)
            if card is None:
                continue
            if batch.get("status") != "pending" or card["status"] != "pending":
                seen_token_elsewhere = True
                print(f"    → 토큰 {token} 은 {batch_path.name} 에 있지만 "
                      f"batch.status={batch.get('status')!r} card.status={card['status']!r} — 건너뜀")
                continue

            card["status"] = "approved" if action == "approve" else "rejected"
            tg.edit_caption(cred_tg, card["message_id"], _card_status_line(card))
            tg.answer_callback(
                cred_tg, cq["id"],
                "승인했습니다" if action == "approve" else "거부했습니다",
            )
            batch_path.write_text(
                json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            matched = True
            processed += 1
            print(f"    → 매칭 성공: {batch_path.name} / {card['card_id']} → {card['status']}")
            break

        if not matched:
            if not seen_token_elsewhere:
                print(f"    → 토큰 {token} 을 어떤 배치에서도 못 찾음 "
                      f"(data/pending 에 해당 카드가 없음)")
            tg.answer_callback(cred_tg, cq["id"], "이미 처리된 카드입니다")

    _save_offset(offset)
    ncard.prune_topics()

    # 모든 카드가 결정된 배치는 최종 발행 처리
    finalized = 0
    for batch_path in _load_batches():
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        if batch.get("status") != "pending":
            continue
        if any(c["status"] == "pending" for c in batch["cards"]):
            continue
        _finalize_batch(batch_path, batch, cred_tg)
        finalized += 1

    print(f"콜백 처리 {processed}건, 뉴스카드 제작 {cards_made}건, "
          f"배치 확정 {finalized}건, 다음 offset={offset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
