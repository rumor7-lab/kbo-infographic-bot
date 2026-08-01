#!/usr/bin/env python3
"""텔레그램 승인 폴링 — 몇 분 간격 크론으로 돌린다 (서버 상시 대기 없음).

흐름
  1. data/telegram_offset.json 에 저장해둔 offset 부터 getUpdates 로 콜백을 가져온다.
  2. "approve:<token>" / "reject:<token>" 콜백을 data/pending/*.json 배치에서 찾아
     해당 카드의 상태를 갱신하고, 텔레그램 메시지를 갱신해 버튼을 없앤다.
  3. 배치 안의 모든 카드가 결정됐으면(더 이상 pending 없음) 그제서야 실제 인스타그램
     발행을 수행한다 — 승인된 카드만 묶어서(캐러셀/단일/릴스) 올린다.

GitHub Actions 크론으로 이 스크립트만 반복 실행하면 되고, 상태는 매번 git commit
으로 저장소에 남겨서 다음 실행에서 이어받는다(러너가 매번 새로 뜨는 걸 전제).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    cred_tg = tg.Credentials.from_env()
    offset = _load_offset()

    updates = tg.get_updates(cred_tg, offset)
    processed = 0

    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        cq = u.get("callback_query")
        if not cq:
            continue
        data = cq.get("data", "")
        action, _, token = data.partition(":")
        if action not in ("approve", "reject") or not token:
            continue

        matched = False
        for batch_path in _load_batches():
            batch = json.loads(batch_path.read_text(encoding="utf-8"))
            if batch.get("status") != "pending":
                continue
            card = _find_card(batch, token)
            if card is None or card["status"] != "pending":
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
            break

        if not matched:
            tg.answer_callback(cred_tg, cq["id"], "이미 처리된 카드입니다")

    _save_offset(offset)

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

    print(f"콜백 처리 {processed}건, 배치 확정 {finalized}건, 다음 offset={offset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
