"""단일 카드를 실제 라이브 데이터로 렌더링 — 특정 카드 샘플만 뽑아보고 싶을 때 사용.

사용법:
    python scripts/render_one.py <card_id>
    python scripts/render_one.py --list        # 사용 가능한 card_id 목록

예:
    python scripts/render_one.py daily_recap        # 유형 A(table) 예시
    python scripts/render_one.py team_standings      # 유형 B(chart) 예시
    python scripts/render_one.py yesterday_heroes     # 유형 C(hero) 예시
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline import build_payload  # noqa: E402
from src.render.renderer import load_cfg, render_card  # noqa: E402


def main() -> int:
    cfg = load_cfg()
    cards = cfg["cards"]["cards"]

    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 1

    if sys.argv[1] == "--list":
        for cid, c in cards.items():
            print(f"  {cid:24s} layout={c.get('layout', 'auto'):8s} {c.get('title', '')}")
        return 0

    card_id = sys.argv[1]
    if card_id not in cards:
        print(f"알 수 없는 card_id: {card_id}")
        print("가능한 값은 --list 로 확인하세요.")
        return 1

    card_cfg = cards[card_id]
    print(f"[{card_id}] 데이터 수집 중 ({card_cfg['source']}) ...")
    payload = build_payload(card_id, card_cfg, cfg)

    out_dir = Path("out") / "manual" / datetime.now().strftime("%Y%m%d_%H%M%S")
    r = render_card(payload, {**card_cfg, "_reels": False}, cfg, out_dir)

    print(f"레이아웃: {r['layout']}  (사유: {r['reason']})")
    print(f"PNG: {r['png'].resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
