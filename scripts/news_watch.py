#!/usr/bin/env python3
"""KBO 화제 감시 — 터진 소식을 찾아 텔레그램으로 알린다.

  python scripts/news_watch.py --once      한 번만 확인 (테스트용)
  python scripts/news_watch.py --watch     10분마다 계속 감시 (상주)

동작
  1. 네이버 뉴스에서 최근 N시간 KBO 기사를 모아 사건별로 묶는다.
  2. 매체 수가 기준을 넘는 주제를 '터진 것'으로 본다.
  3. 이미 알린 주제는 다시 알리지 않는다(data/news_seen.json).
     단 매체 수가 크게 늘면 '더 커졌다'고 한 번 더 알린다.
  4. 텔레그램으로 보내고, 사용자가 그 메시지에 사진을 답장하면
     scripts/news_card.py 가 카드를 만든다.

왜 GitHub Actions 가 아니라 로컬인가
  - 크론이 몇 시간씩 밀리는 게 실측으로 확인됐다. 속보에는 못 쓴다.
  - 카드에 쓸 사진이 로컬에만 있다(공개 저장소에 보도사진을 올릴 수 없음).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.collect import news_trend as nt  # noqa: E402
from src.publish import telegram as tg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SEEN_FILE = ROOT / "data" / "news_seen.json"
TOPIC_DIR = ROOT / "data" / "topics"

# 다시 알릴 기준 — 매체 수가 이 배수만큼 늘면 '더 커졌다'고 한 번 더 알린다.
REALERT_GROWTH = 2.0


def _load_env() -> None:
    """.env 를 환경변수로 읽어들인다 (python-dotenv 의존성 없이)."""
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


def _load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_seen(seen: dict) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 오래된 기록은 버린다 — 며칠 지난 주제가 다시 떠도 그건 새 사건이다.
    cutoff = time.time() - 3 * 86400
    seen = {k: v for k, v in seen.items() if v.get("at", 0) >= cutoff}
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def _topic_id(topic: nt.Topic) -> str:
    """주제 식별자 — 핵심어 2개로 만든다. 표현이 조금 달라도 같은 사건이면 같은 id."""
    return "_".join(topic.keywords[:2])


def _format_alert(topic: nt.Topic, *, grown: bool = False) -> str:
    head = "📈 더 커짐" if grown else "🔥 지금 터짐"
    lines = [
        f"{head} — {topic.key}",
        "",
        f"매체 {topic.outlet_count}곳 · 시간당 {topic.velocity:.1f}건"
        + (f" · {topic.team}" if topic.team else ""),
        f"최초 {topic.oldest:%H:%M} → 최신 {topic.newest:%H:%M}",
        "",
        "제목 참고 (그대로 쓰지 말 것):",
    ]
    for h in topic.headline_candidates(3):
        lines.append(f"  · {h}")
    lines += [
        "",
        "이 소식으로 카드를 만들려면",
        "이 메시지에 답장으로 사진을 보내주세요.",
    ]
    return "\n".join(lines)


def _save_topic(topic: nt.Topic, message_id: int) -> None:
    """사진 답장이 오면 매칭할 수 있게 주제를 저장해둔다."""
    TOPIC_DIR.mkdir(parents=True, exist_ok=True)
    (TOPIC_DIR / f"{message_id}.json").write_text(
        json.dumps(
            {
                "message_id": message_id,
                "topic_id": _topic_id(topic),
                "key": topic.key,
                "keywords": topic.keywords[:8],
                "team": topic.team,
                "outlet_count": topic.outlet_count,
                "headlines": topic.headline_candidates(3),
                "links": [a.link for a in topic.articles[:5]],
                "created_at": datetime.now(nt.KST).isoformat(),
                "status": "waiting_photo",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def check_once(*, hours: int, min_outlets: int, top: int, quiet: bool) -> int:
    topics = nt.hot_topics(hours=hours, min_outlets=min_outlets, top=top)
    if not topics:
        print(f"[{datetime.now():%H:%M}] 기준({min_outlets}개 매체) 넘는 화제 없음")
        return 0

    seen = _load_seen()
    sent = 0

    for t in topics:
        tid = _topic_id(t)
        prev = seen.get(tid)
        grown = False

        if prev:
            # 같은 주제라도 규모가 크게 커졌으면 한 번 더 알린다
            if t.outlet_count >= prev.get("outlets", 0) * REALERT_GROWTH:
                grown = True
            else:
                print(f"  · 이미 알림: {t.key} (매체 {t.outlet_count}곳)")
                continue

        print(f"  🔥 {t.key} — 매체 {t.outlet_count}곳, {t.velocity:.1f}건/h"
              + (" [더 커짐]" if grown else ""))

        if not quiet:
            try:
                cred = tg.Credentials.from_env()
                mid = tg.send_message(cred, _format_alert(t, grown=grown))
                _save_topic(t, mid)
                sent += 1
            except Exception as e:  # noqa: BLE001
                print(f"     텔레그램 전송 실패: {e}")
                continue

        seen[tid] = {"at": time.time(), "outlets": t.outlet_count, "key": t.key}

    _save_seen(seen)
    return sent


def main() -> int:
    ap = argparse.ArgumentParser(description="KBO 화제 감시")
    ap.add_argument("--once", action="store_true", help="한 번만 확인")
    ap.add_argument("--watch", action="store_true", help="주기적으로 계속 감시")
    ap.add_argument("--interval", type=int, default=10, help="감시 주기(분), 기본 10")
    ap.add_argument("--hours", type=int, default=6, help="최근 몇 시간을 볼지, 기본 6")
    ap.add_argument("--min-outlets", type=int, default=4,
                    help="이 수 이상 매체가 다뤄야 '터진 것'으로 본다, 기본 4")
    ap.add_argument("--top", type=int, default=5, help="한 번에 최대 몇 개, 기본 5")
    ap.add_argument("--quiet", action="store_true",
                    help="텔레그램 전송 없이 화면에만 출력 (테스트용)")
    args = ap.parse_args()

    if not args.once and not args.watch:
        ap.error("--once 또는 --watch 중 하나를 지정하세요")

    _load_env()

    try:
        nt._credentials()
    except nt.NewsError as e:
        print(f"설정 오류: {e}")
        return 1

    kw = dict(hours=args.hours, min_outlets=args.min_outlets,
              top=args.top, quiet=args.quiet)

    if args.once:
        check_once(**kw)
        return 0

    print(f"화제 감시 시작 — {args.interval}분 간격, 최근 {args.hours}시간, "
          f"매체 {args.min_outlets}곳 이상. 중지는 Ctrl+C")
    while True:
        try:
            check_once(**kw)
        except nt.NewsError as e:
            print(f"  뉴스 조회 실패: {e}")
        except Exception as e:  # noqa: BLE001
            # 한 번 실패했다고 감시를 멈추면 안 된다 (밤새 돌아야 함)
            print(f"  오류(계속 진행): {type(e).__name__}: {e}")
        try:
            time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            print("\n감시 종료")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
