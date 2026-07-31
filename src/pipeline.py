"""오케스트레이터 — 슬롯 하나를 처음부터 끝까지 실행.

  수집 → 검증 → 유형 자동선택 → 렌더 → 공개URL → 발행

정책
  - 검증 실패 카드는 스킵. 슬롯 전체를 죽이지 않는다.
  - risk: high 카드는 발행하지 않고 승인 큐(data/approval/)에 넣는다.
  - --dry-run 은 렌더까지만. 로컬 화면 확인용.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.collect import kbo_official as kbo          # noqa: E402
from src.collect.player_photo import get_photo        # noqa: E402
from src.publish import captions, hosting            # noqa: E402
from src.publish import instagram as ig              # noqa: E402
from src.render.layout_engine import Payload, Subject  # noqa: E402
from src.render.renderer import load_cfg, render_card  # noqa: E402
from src.validate.rules import gate, validate, validate_subject  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
APPROVAL = ROOT / "data" / "approval"
LOG = ROOT / "data" / "runs"

WEEKDAY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class SkipCard(Exception):
    """이 카드만 건너뛴다."""


# ── 소스 → Payload ───────────────────────────────
def build_payload(card_id: str, card_cfg: dict, cfg: dict) -> Payload:
    src = card_cfg["source"]
    now = datetime.now(KST)

    if src == "kbo.standings":
        s = kbo.standings()
        return Payload(
            card_id=card_id, title=card_cfg["title"], kicker=card_cfg.get("kicker", ""),
            columns=s.columns, rows=s.rows, metric="승률", as_of=s.as_of,
        )

    if src in ("kbo.games_yesterday", "kbo.games_today"):
        target = (now - timedelta(days=1)).date() if src.endswith("yesterday") else now.date()
        s = kbo.games(target)
        return Payload(
            card_id=card_id, title=card_cfg["title"], kicker=card_cfg.get("kicker", ""),
            columns=s.columns, rows=s.rows, as_of=s.as_of,
            provisional=card_cfg.get("provisional", False),
        )

    if src in ("kbo.hitter_leaders", "kbo.pitcher_leaders"):
        kind = "hitter" if "hitter" in src else "pitcher"
        s = kbo.leaders(kind)
        return Payload(
            card_id=card_id, title=card_cfg["title"], kicker=card_cfg.get("kicker", ""),
            columns=s.columns, rows=s.rows, as_of=s.as_of,
        )

    if src == "kbo.team_risp_worst":
        s = kbo.team_risp_worst()
        return Payload(
            card_id=card_id, title=card_cfg["title"], kicker=card_cfg.get("kicker", ""),
            columns=s.columns, rows=s.rows, metric="득점권타율", as_of=s.as_of,
            footnote_extra=card_cfg.get("footnote_extra", ""),
        )

    if src == "kbo.milestone_watch":
        s = kbo.milestone_watch()
        return Payload(
            card_id=card_id, title=card_cfg["title"], kicker=card_cfg.get("kicker", ""),
            columns=s.columns, rows=s.rows, as_of=s.as_of,
            footnote_extra="KBO 공식 기록 기반 자체 계산",
        )

    if src in ("kbo.top_performer_yesterday", "kbo.top_performer_today"):
        # TODO: 경기별 선수 기록(게임센터 박스스코어) 파싱으로 교체하면 정확도가 올라간다.
        #       현재는 시즌 타격 1위를 주인공으로 세우는 근사 방식.
        s = kbo.leaders("hitter")
        if not s.rows:
            raise SkipCard("타격 순위 데이터 없음")
        top = s.rows[0]
        name, team = top.get("선수명", ""), top.get("_team")
        photo = get_photo(name, team)
        subject = Subject(
            name=name, team=team,
            photo=photo.path if photo else None,
            photo_pos=photo.css_position if photo else None,
            headline=name,
            sub=f"{team} · 시즌 타율 {top.get('타율', '-')} 리그 1위",
            stats=[
                {"label": "타율", "value": top.get("타율", "-")},
                {"label": "홈런", "value": top.get("홈런", "-")},
                {"label": "타점", "value": top.get("타점", "-")},
            ],
        )
        return Payload(
            card_id=card_id, title=card_cfg["title"], kicker=card_cfg.get("kicker", ""),
            columns=s.columns, rows=[], subject=subject, as_of=s.as_of,
            provisional=card_cfg.get("provisional", False),
        )

    if src.startswith("manual."):
        name = src.split(".", 1)[1]
        p = ROOT / "data" / "manual" / f"{name}.yml"
        if not p.exists():
            raise SkipCard(f"수동 데이터 파일 없음: {p.relative_to(ROOT)}")
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        return Payload(
            card_id=card_id, title=card_cfg["title"], kicker=card_cfg.get("kicker", ""),
            columns=d["columns"], rows=d["rows"],
            as_of=d.get("as_of", now.strftime("%Y.%m.%d")),
            footnote_extra=d.get("note", ""),
        )

    if src == "compute.playoff_odds":
        from src.compute.playoff_odds import SimulationError, simulate

        standings_snap = kbo.standings()
        try:
            rows = simulate(standings_snap.rows)
        except SimulationError as e:
            raise SkipCard(str(e)) from e
        return Payload(
            card_id=card_id, title=card_cfg["title"], kicker=card_cfg.get("kicker", ""),
            columns=["팀명", "가을야구확률"], rows=rows, metric="가을야구확률",
            as_of=standings_snap.as_of,
            footnote_extra=card_cfg.get("footnote_extra", ""),
        )

    if src.startswith("compute."):
        raise SkipCard(
            f"'{src}' 는 아직 미구현입니다 (src/compute/ 에 구현 후 활성화). "
            "미구현 소스는 가짜 데이터로 채우지 않고 스킵합니다."
        )

    raise SkipCard(f"알 수 없는 소스: {src}")


# ── 슬롯 실행 ────────────────────────────────────
def resolve_cards(slot_cfg: dict, cards: dict, now: datetime) -> list[tuple[str, dict]]:
    out = []
    for cid in slot_cfg["cards"]:
        cfg = cards[cid]
        if "rotation" in cfg:                      # 요일 로테이션 해석
            key = WEEKDAY[now.weekday()]
            cid = cfg["rotation"][key]
            cfg = cards[cid]
        out.append((cid, cfg))
    return out


def run_slot(slot: str, *, dry_run: bool = False) -> dict[str, Any]:
    cfg = load_cfg()
    slot_cfg = cfg["cards"]["slots"][slot]
    now = datetime.now(KST)
    stamp = now.strftime("%Y%m%d_%H%M")
    out_dir = OUT / stamp / slot
    result: dict[str, Any] = {"slot": slot, "at": now.isoformat(), "cards": []}

    if kbo.has_recent_corrections():
        result["note"] = "최근 기록 정정 이력 있음 — 수치 재확인 권장"

    rendered: list[dict[str, Any]] = []

    for card_id, card_cfg in resolve_cards(slot_cfg, cfg["cards"]["cards"], now):
        entry: dict[str, Any] = {"card_id": card_id}
        try:
            payload = build_payload(card_id, card_cfg, cfg)

            # 히어로 카드는 행이 없으므로 주인공을 검증한다
            if payload.subject and not payload.rows:
                rep = validate_subject(payload.subject, card_id=card_id)
            else:
                rep = validate(
                    payload.rows, payload.columns, cfg,
                    card_id=card_id, label_col=payload.label_column(),
                    prev_rows=_prev_rows(card_id),
                )
            entry["validation"] = rep.summary()
            if not gate(rep, cfg):
                raise SkipCard(f"검증 실패 → 발행 스킵 ({rep.summary()})")

            card_cfg = {**card_cfg, "_reels": bool(slot_cfg.get("reels"))}
            r = render_card(payload, card_cfg, cfg, out_dir)
            entry.update({"layout": r["layout"], "reason": r["reason"], "png": str(r["png"])})
            if r.get("mp4"):
                entry["mp4"] = str(r["mp4"])

            if card_cfg.get("risk") == "high":
                _queue_approval(card_id, payload, r, out_dir)
                entry["status"] = "승인대기 (risk: high)"
            else:
                rendered.append({"payload": payload, "render": r, "cfg": card_cfg})
                entry["status"] = "렌더 완료"

        except SkipCard as e:
            entry["status"] = f"스킵: {e}"
        except Exception as e:  # noqa: BLE001
            entry["status"] = f"오류: {e}"
            entry["trace"] = traceback.format_exc(limit=4)

        result["cards"].append(entry)

    if dry_run:
        result["published"] = "dry-run (발행 안 함)"
        _write_log(result, stamp, slot)
        return result

    if rendered:
        try:
            result["published"] = _publish(rendered, slot_cfg, cfg)
        except Exception as e:  # noqa: BLE001
            result["published"] = f"발행 실패: {e}"
            result["publish_trace"] = traceback.format_exc(limit=4)
    else:
        result["published"] = "발행할 카드 없음"

    _write_log(result, stamp, slot)
    return result


def _publish(rendered: list[dict], slot_cfg: dict, cfg: dict) -> dict[str, Any]:
    cred = ig.Credentials.from_env()
    quota = ig.remaining_quota(cred)
    days = ig.token_days_left(cred)
    info: dict[str, Any] = {"quota_left": quota, "token_days_left": days}
    if days is not None and days < 10:
        info["warning"] = f"토큰 만료 {days}일 전 — 갱신 필요"

    first = rendered[0]
    caption = captions.build(
        first["payload"].card_id,
        first["payload"].title,
        first["payload"].rows,
        as_of=first["payload"].as_of,
        provisional=first["payload"].provisional,
        extra_note=first["cfg"].get("footnote_extra", ""),
    )

    # 릴스 슬롯
    if slot_cfg.get("reels") and rendered[0]["render"].get("mp4"):
        url = hosting.upload(Path(rendered[0]["render"]["mp4"]))
        info["reel_id"] = ig.post_reel(cred, url, caption)
        return info

    # 캐러셀 (2장 이상일 때만) / 단일
    pngs = [Path(r["render"]["png"]) for r in rendered]
    urls = [hosting.upload(p) for p in pngs]
    if slot_cfg.get("carousel") and len(urls) >= 2:
        info["media_id"] = ig.post_carousel(cred, urls[:10], caption)
    else:
        info["media_id"] = ig.post_single(cred, urls[0], caption)
    return info


def _prev_rows(card_id: str) -> list[dict] | None:
    """전일 스냅샷 로드 — 누적 스탯 역행 검사에 사용."""
    y = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")
    for name in (card_id, card_id.replace("team_", ""), "standings"):
        p = ROOT / "data" / "snapshots" / y / f"{name}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8")).get("rows")
            except json.JSONDecodeError:
                return None
    return None


def _queue_approval(card_id: str, payload: Payload, r: dict, out_dir: Path) -> None:
    APPROVAL.mkdir(parents=True, exist_ok=True)
    (APPROVAL / f"{card_id}.json").write_text(
        json.dumps(
            {
                "card_id": card_id,
                "title": payload.title,
                "png": str(r["png"]),
                "as_of": payload.as_of,
                "queued_at": datetime.now(KST).isoformat(),
                "approved": False,
                "note": "확인 후 approved: true 로 바꾸고 publish_approved.py 실행",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_log(result: dict, stamp: str, slot: str) -> None:
    LOG.mkdir(parents=True, exist_ok=True)
    (LOG / f"{stamp}_{slot}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="KBO 인포그래픽 슬롯 실행")
    ap.add_argument("slot", choices=["morning", "noon", "night"])
    ap.add_argument("--dry-run", action="store_true", help="렌더까지만 (발행 안 함)")
    args = ap.parse_args()

    res = run_slot(args.slot, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))

    failed = [c for c in res["cards"] if c["status"].startswith(("오류", "스킵"))]
    if failed and len(failed) == len(res["cards"]):
        return 1          # 전부 실패면 워크플로를 빨갛게 만들어 알림이 오게 한다
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
