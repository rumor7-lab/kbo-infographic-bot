#!/usr/bin/env python3
"""사진 답장 → 뉴스카드 렌더 — 로컬 수동 테스트/디버그 전용.

  python scripts/news_card.py --once --debug

⚠ 운영 중엔 이 스크립트를 --watch 로 상시 실행하지 마세요.
  실제 처리는 이제 scripts/poll_telegram_approvals.py (GitHub Actions 5분 크론)
  가 전담한다. 텔레그램 getUpdates 의 offset 은 봇 전체 기준 전역이라, 이
  스크립트가 따로 폴링하면 크론과 같은 업데이트를 두고 경쟁하게 되고 — 실측에서
  실제로 크론이 먼저 소비해버려서 로컬엔 '업데이트 0건'으로 보이는 사고가 났다.
  (자세한 이유는 src/news_pipeline.py 모듈 docstring 참고)

  그래서 이 스크립트는 --once 로 "지금 대기 중인 답장이 있는지 눈으로 확인"
  하는 용도로만 쓴다. 실제로 카드를 큐에 올리고 싶으면 크론이 도는 걸 몇 분
  기다리거나, workflow_dispatch 로 approve-poll 워크플로를 수동 실행하세요.

  --no-push 없이 --once 를 실제로 돌리면 이 스크립트도 getUpdates offset 을
  전진시켜버려 크론과 똑같이 업데이트를 소비한다 — 그래서 기본적으로 오프셋을
  저장하지 않는다(항상 최신 대기 목록을 다시 읽는다). 정말 이 스크립트로 큐에
  올리고 싶을 때만 --commit 을 붙이세요(그러면 오프셋도 저장하고 git push 도
  한다 — 크론과는 그 시점에 한해서만 수동으로 경쟁을 피해가며 쓰는 것).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import news_pipeline as ncard  # noqa: E402
from src.publish import telegram as tg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


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


def _git_commit_push(paths: list[str], message: str) -> bool:
    subprocess.run(["git", "add", *paths], cwd=ROOT, check=False)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if diff.returncode == 0:
        return True
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=ROOT, check=True)
    r = subprocess.run(["git", "push"], cwd=ROOT)
    return r.returncode == 0


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="뉴스카드 사진 답장 — 로컬 수동 테스트")
    ap.add_argument("--once", action="store_true", help="한 번 확인 (offset 은 전진시키지 않음)")
    ap.add_argument("--commit", action="store_true",
                     help="실제로 offset 을 전진시키고 처리 결과를 git push 한다 "
                          "(운영용 크론과 그 순간만 경쟁하게 되니 평소엔 쓰지 마세요)")
    ap.add_argument("--debug", action="store_true", help="처리 안 된 답장까지 이유를 출력")
    args = ap.parse_args()

    if not args.once:
        ap.error("--once 를 지정하세요 (상시 --watch 는 더 이상 지원하지 않습니다)")

    _load_env()

    try:
        cred = tg.Credentials.from_env()
    except tg.TelegramError as e:
        print(f"설정 오류: {e}")
        return 1

    # offset=0 → 아직 소비되지 않은(= approve-poll 크론도 아직 안 읽어간) 최근
    # 업데이트를 항상 그대로 다시 읽는다. --commit 을 안 주면 이 호출 자체도
    # offset 을 전진시키지 않으므로 크론의 몫을 가로채지 않는다.
    updates = tg.get_updates(cred, 0)
    processed = 0
    for u in updates:
        msg = u.get("message")
        if not msg:
            continue
        try:
            if ncard.handle_photo_reply(cred, msg, debug=args.debug):
                processed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  처리 오류(계속 진행): {type(e).__name__}: {e}")

    ncard.prune_topics()
    print(f"업데이트 {len(updates)}건 확인 → 카드 {processed}건 제작")

    if args.commit and processed:
        ok = _git_commit_push(
            ["data/pending", "data/topics"], "news: 로컬 수동 처리 — 카드 승인 요청",
        )
        if not ok:
            print("  ⚠ git push 실패 — 수동으로 git pull --no-edit && git push 해주세요")
    elif processed:
        print("  ※ --commit 을 안 줘서 로컬에만 렌더됐습니다. 텔레그램 승인 큐에 "
              "실제로 올리려면 --commit 을 붙여 다시 실행하세요.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
