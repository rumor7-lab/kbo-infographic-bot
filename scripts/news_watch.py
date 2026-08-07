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
import subprocess
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


def _git_commit_push_topics() -> None:
    """새로 만든 data/topics/*.json 을 GitHub 에 올린다.

    이 스크립트는 로컬에서만 돌아가지만, 사진 답장 처리(scripts/news_card.py
    로직)는 GitHub Actions 체크아웃에서 실행된다 — 완전히 다른 파일 시스템이다.
    여기서 커밋/푸시를 안 하면 주제 파일이 로컬 디스크에만 있고 GitHub 쪽은
    존재 자체를 모르므로, 사람이 사진으로 답장해도 '해당 주제 없음'으로
    조용히 무시된다(실측으로 확인 — 알림은 왔는데 사진 답장에 반응이 없었던
    사고의 진짜 원인이었다. offset 경쟁 문제와는 별개의 버그였다).
    """
    try:
        subprocess.run(["git", "add", "data/topics"], cwd=ROOT, check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
        if diff.returncode == 0:
            return  # 커밋할 변경 없음
        subprocess.run(
            ["git", "commit", "-q", "-m", "news: 새 주제 알림"], cwd=ROOT, check=True
        )
        r = subprocess.run(["git", "push"], cwd=ROOT)
        if r.returncode != 0:
            print("  ⚠ 주제 파일 push 실패 — 사진 답장이 GitHub 쪽에서 안 잡힐 수 있습니다. "
                  "수동으로 git pull --no-edit && git push 해주세요")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 주제 파일 git 커밋 중 오류(계속 진행): {e}")


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


def _format_alert(topic: nt.Topic, *, grown: bool = False, by_interest: bool = False) -> str:
    if by_interest:
        head = "✨ 화제될 만한 단독 기사"
    else:
        head = "📈 더 커짐" if grown else "🔥 지금 터짐"
    lines = [f"{head} — {topic.key}", ""]
    if by_interest:
        # 단독/인터뷰성 기사는 매체 수가 낮은 게 당연하다 — 조건이 다르다는 걸
        # 명시해야 '왜 1~2곳인데 왔지' 하는 혼란이 없다.
        lines.append(
            f"매체 {topic.outlet_count}곳(단독성) · 인터뷰·드라마 신호 {topic.interest}점"
            + (f" · {topic.team}" if topic.team else "")
        )
    else:
        lines.append(
            f"매체 {topic.outlet_count}곳 · 시간당 {topic.velocity:.1f}건"
            + (f" · {topic.team}" if topic.team else "")
        )
    lines += [
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


def check_once(*, hours: int, min_outlets: int, min_interest: int, top: int,
               quiet: bool, debug: bool = False) -> int:
    def progress(q: str, got: int, fresh: int) -> None:
        if debug:
            print(f"    검색 '{q}': 수신 {got}건 → 최근 {hours}시간 내 {fresh}건")

    articles = nt.collect(hours=hours, on_query=progress)
    all_topics = nt.rank(nt.cluster(articles))

    # 두 가지 서로 다른 이유로 '터진 것'이 될 수 있다 — 매체 수(속보성)
    # 아니면 인터뷰·드라마 신호(단독 기사인데 사람들이 많이 읽을 법함).
    # all_topics 는 매체 수로만 정렬돼 있어서, 여기서 그냥 상위 N개를 자르면
    # 인터뷰성 단독 기사(매체 수가 원래 낮음)는 항상 뒤로 밀려 잘려나간다 —
    # "구자욱 FA 인터뷰"를 놓쳤던 문제를 이 정렬 순서 그대로 재현하게 되는 셈이다.
    # 그래서 두 채널의 후보 목록을 따로 뽑고 각자의 기준으로 상위 N개씩 뽑는다.
    outlet_hits = [t for t in all_topics if t.outlet_count >= min_outlets][:top]
    interest_hits = sorted(
        (t for t in all_topics
         if t.outlet_count < min_outlets and t.interest >= min_interest),
        key=lambda t: t.interest, reverse=True,
    )[:top]
    topics = [(t, True) for t in outlet_hits] + [(t, False) for t in interest_hits]

    if debug or not topics:
        # 결과가 없을 때 그냥 '없음'만 찍으면 원인을 알 수 없다.
        # 몇 건을 모았고 상위 주제가 몇 매체였는지 보여줘야 임계값을 조정할 수 있다.
        print(f"[{datetime.now():%H:%M}] 기사 {len(articles)}건 수집 → "
              f"주제 {len(all_topics)}개 (매체 {len(outlet_hits)}개 + "
              f"관심 {len(interest_hits)}개 = {len(topics)}개)")
        if not articles:
            print("    → 기사가 0건입니다. 네이버 앱의 '사용 API' 에 검색이 있는지,"
                  " 키가 맞는지 확인하세요.")
        elif all_topics:
            print("    상위 주제 — 매체 수 기준 (기준 미달 포함):")
            for t in all_topics[:5]:
                by_outlet = t.outlet_count >= min_outlets
                mark = "✓" if by_outlet else " "
                print(f"      {mark} 매체 {t.outlet_count}곳 · {t.velocity:4.1f}건/h · "
                      f"관심 {t.interest}점 · {t.key}")
                if debug:
                    # 대표어만 봐서는 진짜 하나의 사건인지 판단이 안 될 때가 있다
                    # ("중단 이후" 처럼). 실제 제목을 같이 보여줘 눈으로 검증한다.
                    for h in t.headline_candidates(2):
                        print(f"          · {h}")

            # 매체 수 기준 목록엔 인터뷰성 기사가 애초에 안 보인다(매체 수가
            # 낮으니 저 정렬에선 밑바닥). 그래서 관심점수 기준 상위도 따로 보여준다.
            by_interest_all = sorted(
                (t for t in all_topics if t.outlet_count < min_outlets),
                key=lambda t: t.interest, reverse=True,
            )[:5]
            if by_interest_all:
                print("    상위 주제 — 관심점수 기준 (매체수 기준 미달만, 기준 미달 포함):")
                for t in by_interest_all:
                    mark = "✓" if t.interest >= min_interest else " "
                    print(f"      {mark} 관심 {t.interest}점 · 매체 {t.outlet_count}곳 · {t.key}")
                    if debug:
                        for h in t.headline_candidates(2):
                            print(f"          · {h}")

    if not topics:
        return 0

    seen = _load_seen()
    sent = 0

    for t, by_outlet in topics:
        tid = _topic_id(t)
        prev = seen.get(tid)
        grown = False

        if prev:
            # 같은 주제라도 규모가 크게 커졌으면 한 번 더 알린다
            # (인터뷰성 단독 기사는 매체 수가 안 느니 이 재알림 자체가 해당 없음)
            if by_outlet and t.outlet_count >= prev.get("outlets", 0) * REALERT_GROWTH:
                grown = True
            else:
                print(f"  · 이미 알림: {t.key} (매체 {t.outlet_count}곳)")
                continue

        label = "🔥" if by_outlet else "✨"
        print(f"  {label} {t.key} — 매체 {t.outlet_count}곳, {t.velocity:.1f}건/h, "
              f"관심 {t.interest}점" + (" [더 커짐]" if grown else ""))

        if not quiet:
            try:
                cred = tg.Credentials.from_env()
                mid = tg.send_message(
                    cred, _format_alert(t, grown=grown, by_interest=not by_outlet)
                )
                _save_topic(t, mid)
                sent += 1
            except Exception as e:  # noqa: BLE001
                print(f"     텔레그램 전송 실패: {e}")
                continue

        seen[tid] = {"at": time.time(), "outlets": t.outlet_count, "key": t.key}

    _save_seen(seen)
    if sent:
        _git_commit_push_topics()
    return sent


def main() -> int:
    ap = argparse.ArgumentParser(description="KBO 화제 감시")
    ap.add_argument("--once", action="store_true", help="한 번만 확인")
    ap.add_argument("--watch", action="store_true", help="주기적으로 계속 감시")
    ap.add_argument("--interval", type=int, default=10, help="감시 주기(분), 기본 10")
    ap.add_argument("--hours", type=int, default=6, help="최근 몇 시간을 볼지, 기본 6")
    ap.add_argument("--min-outlets", type=int, default=4,
                    help="이 수 이상 매체가 다뤄야 '터진 것'으로 본다, 기본 4")
    ap.add_argument("--min-interest", type=int, default=2,
                    help="매체 수가 낮아도 이 점수 이상 인터뷰·드라마 신호가 있으면 "
                         "'터진 것'으로 본다(0이면 이 신호 끔), 기본 2")
    ap.add_argument("--top", type=int, default=5, help="한 번에 최대 몇 개, 기본 5")
    ap.add_argument("--quiet", action="store_true",
                    help="텔레그램 전송 없이 화면에만 출력 (테스트용)")
    ap.add_argument("--debug", action="store_true",
                    help="검색어별 수신 건수와 기준 미달 주제까지 전부 출력")
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
              min_interest=args.min_interest,
              top=args.top, quiet=args.quiet, debug=args.debug)

    if args.once:
        try:
            check_once(**kw)
        except nt.NewsError as e:
            # 검색 API 미허용/키 오류가 제일 흔하다. 트레이스백보다 원인을 보여준다.
            print(f"\n뉴스 조회 실패: {e}")
            print("\n확인할 것")
            print("  1. https://developers.naver.com/apps → 내 애플리케이션 →")
            print("     '인싸이트보드' → 연필 아이콘 → 사용 API 에 '검색' 이 있는지")
            print("  2. .env 의 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 값이 맞는지")
            return 1
        return 0

    print(f"화제 감시 시작 — {args.interval}분 간격, 최근 {args.hours}시간, "
          f"매체 {args.min_outlets}곳 이상 또는 관심점수 {args.min_interest}점 이상. "
          f"중지는 Ctrl+C")
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
