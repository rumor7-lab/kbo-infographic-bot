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
import secrets
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
from src.publish import telegram as tg               # noqa: E402
from src.render.layout_engine import Payload, Subject  # noqa: E402
from src.render.renderer import load_cfg, render_card  # noqa: E402
from src.validate.rules import gate, validate, validate_subject, validate_subjects  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
APPROVAL = ROOT / "data" / "approval"
PENDING = ROOT / "data" / "pending"          # 텔레그램 승인 대기 배치(카드별 승인 게이트)
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
        # kbo.standings() 는 경기/승/무/패/게임차 등 컬럼을 다 갖고 있어서 columns 를
        # 그대로 넘기면 numeric_columns() 가 6개를 잡아내 select_layout() 이
        # "지표 3개 이상 → 표" 규칙에 걸려 표로 떨어진다. 이 카드는 항상 차트(막대)로
        # 보여주려는 의도라, 차트 판별에 쓰이는 columns 만 팀명+승률로 좁힌다.
        # (rows 자체는 원본 그대로라 _sub 캡션 등은 그대로 살아있음)
        return Payload(
            card_id=card_id, title=card_cfg["title"], kicker=card_cfg.get("kicker", ""),
            columns=["팀명", "승률"], rows=s.rows, metric="승률", as_of=s.as_of,
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
        # rows 를 항상 채우면 hero.html.j2 가 '단일 주인공' 모드 대신 rows 유무로
        # 분기하는 '랭킹 오버레이' 모드로 빠져버려(사진이 있어도!) 의도한 히어로
        # 비주얼이 안 나온다. 그래서 사진을 못 구해 hero → table 로 강등될 때만
        # 안전망으로 rows 를 채운다 — 그래야 강등돼도 최소한 빈 카드가 안 나간다.
        rows_fallback = [] if photo else s.rows
        return Payload(
            # metric 을 지정해둬야 today_top_performer 처럼 fallback_layout: chart 인
            # 카드가 강등될 때 _coerce() 가 '지표 없음'으로 보고 table 로 재강등하지
            # 않는다(타격 순위는 숫자 컬럼이 여러 개라 지정 없인 chart 판정이 안 됨).
            card_id=card_id, title=card_cfg["title"], kicker=card_cfg.get("kicker", ""),
            columns=s.columns, rows=rows_fallback, subject=subject, as_of=s.as_of,
            metric="타율" if not photo else None,
            provisional=card_cfg.get("provisional", False),
        )

    if src in ("kbo.hitter_grid", "kbo.pitcher_grid"):
        # 유형 D(그리드) — '@no._license'의 선수 비교 그리드 포맷 벤치마크.
        # 리그 상위 30명(leaders 원본 range) 중 앞쪽 N명을 사진+스탯 카드로 나열한다.
        # 트레이드 정리처럼 뉴스를 사람이 정리해야 하는 소스가 아니라 KBO 공식
        # 기록만으로 완전 자동 구성 가능한 조합이라 이 둘부터 그리드로 우선 배정.
        kind = "hitter" if "hitter" in src else "pitcher"
        n = card_cfg.get("grid_size", 4)
        s = kbo.leaders(kind, top=30)
        subjects = []
        for row in s.rows[:n]:
            name, team = row.get("선수명", ""), row.get("_team")
            photo = get_photo(name, team)
            if kind == "hitter":
                stats = [
                    {"label": "타율", "value": row.get("타율", "-")},
                    {"label": "홈런", "value": row.get("홈런", "-")},
                    {"label": "타점", "value": row.get("타점", "-")},
                ]
            else:
                stats = [
                    {"label": "평균자책", "value": row.get("평균자책", "-")},
                    {"label": "승", "value": row.get("승", "-")},
                    {"label": "탈삼진", "value": row.get("탈삼진", "-")},
                ]
            subjects.append(
                Subject(
                    name=name, team=team,
                    photo=photo.path if photo else None,
                    photo_pos=photo.css_position if photo else None,
                    stats=stats,
                )
            )
        if not subjects:
            raise SkipCard(f"{kind} 그리드용 선수 데이터 없음")
        return Payload(
            card_id=card_id, title=card_cfg["title"], kicker=card_cfg.get("kicker", ""),
            subjects=subjects, as_of=s.as_of,
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


def _render_all(slot: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict, str]:
    """카드 렌더+검증 공통 루프. run_slot(즉시 발행) / notify_slot(텔레그램 승인) 이 공유한다.

    반환: (로그용 result, 발행 후보(rendered) 목록, slot_cfg, stamp)
    """
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

            # 히어로 카드는 행이 없으므로 주인공을 검증하고,
            # 그리드 카드는 나열된 선수 목록(subjects)을 검증한다
            if payload.subjects:
                rep = validate_subjects(payload.subjects, card_id=card_id)
            elif payload.subject and not payload.rows:
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

    return result, rendered, slot_cfg, stamp


def run_slot(slot: str, *, dry_run: bool = False) -> dict[str, Any]:
    """렌더 후 바로 인스타그램에 발행한다 (승인 절차 없음).

    2주 수동 운영 기간 동안은 workflow_dispatch(수동 실행)로만 쓴다. 상시 자동
    운영은 notify_slot() 쪽(텔레그램 승인 게이트)을 쓴다.
    """
    cfg = load_cfg()
    result, rendered, slot_cfg, stamp = _render_all(slot)

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


def notify_slot(slot: str, *, dry_run: bool = False) -> dict[str, Any]:
    """렌더된 카드를 바로 발행하지 않고 텔레그램으로 승인 요청을 보낸다.

    실제 발행은 scripts/poll_telegram_approvals.py 가 사람이 버튼을 누른 뒤에
    수행한다 — 그래야 카드별로 개별 승인/거부가 가능하고, 그 결과에 따라
    캐러셀 구성(몇 장을 묶을지)이 최종 확정된다.
    """
    cfg = load_cfg()
    result, rendered, slot_cfg, stamp = _render_all(slot)

    if dry_run:
        result["notified"] = "dry-run (텔레그램 전송 안 함)"
        _write_log(result, stamp, slot)
        return result

    if rendered:
        try:
            result["notified"] = _notify_telegram(rendered, slot_cfg, cfg, slot=slot, stamp=stamp)
        except Exception as e:  # noqa: BLE001
            result["notified"] = f"알림 전송 실패: {e}"
            result["notify_trace"] = traceback.format_exc(limit=4)
    else:
        result["notified"] = "전송할 카드 없음"

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


def _tg_caption(card_id: str, caption: str) -> str:
    """텔레그램 캡션 길이 제한(1024자) 대비 — 실제 인스타 캡션은 그대로 두고
    텔레그램에 보여줄 미리보기만 자른다."""
    head = f"[{card_id}]\n"
    body = caption if len(caption) <= 900 else caption[:900] + "…"
    return head + body


def _notify_telegram(
    rendered: list[dict], slot_cfg: dict, cfg: dict, *, slot: str, stamp: str
) -> dict[str, Any]:
    """카드마다 공개 URL을 먼저 확보해두고(실제 발행 시 재업로드 불필요), 텔레그램으로
    승인 요청을 보낸다. 배치 전체가 승인/거부로 결정되기 전까진 아무것도 발행되지 않는다
    — 실제 발행은 scripts/poll_telegram_approvals.py 가 담당."""
    cred = tg.Credentials.from_env()

    first = rendered[0]
    caption = captions.build(
        first["payload"].card_id,
        first["payload"].title,
        first["payload"].rows,
        as_of=first["payload"].as_of,
        provisional=first["payload"].provisional,
        extra_note=first["cfg"].get("footnote_extra", ""),
    )

    is_reels = bool(slot_cfg.get("reels") and rendered[0]["render"].get("mp4"))
    batch_id = f"{stamp}_{slot}"
    cards: list[dict[str, Any]] = []

    if is_reels:
        r = rendered[0]
        url = hosting.upload(Path(r["render"]["mp4"]))
        token = secrets.token_hex(4)
        message_id = tg.send_video_for_approval(cred, url, _tg_caption(r["payload"].card_id, caption), token)
        cards.append({
            "token": token, "card_id": r["payload"].card_id, "kind": "video",
            "url": url, "message_id": message_id, "status": "pending",
        })
    else:
        for r in rendered:
            url = hosting.upload(Path(r["render"]["png"]))
            token = secrets.token_hex(4)
            message_id = tg.send_photo_for_approval(cred, url, _tg_caption(r["payload"].card_id, caption), token)
            cards.append({
                "token": token, "card_id": r["payload"].card_id, "kind": "image",
                "url": url, "message_id": message_id, "status": "pending",
            })

    batch = {
        "batch_id": batch_id,
        "slot": slot,
        "caption": caption,
        "carousel": bool(slot_cfg.get("carousel")),
        "reels": is_reels,
        "created_at": datetime.now(KST).isoformat(),
        "status": "pending",
        "cards": cards,
    }
    PENDING.mkdir(parents=True, exist_ok=True)
    (PENDING / f"{batch_id}.json").write_text(
        json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tg.send_message(
        cred,
        f"'{slot}' 슬롯 카드 {len(cards)}장 — 각 카드 아래 버튼으로 승인/거부해주세요.\n"
        f"전부 결정되면 승인된 카드만 묶여서 자동 발행됩니다.",
    )
    return {"batch_id": batch_id, "cards": len(cards)}


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
    ap.add_argument("--dry-run", action="store_true", help="렌더까지만 (발행/전송 안 함)")
    ap.add_argument(
        "--notify", action="store_true",
        help="바로 발행하지 않고 텔레그램 승인 요청만 보낸다 (카드별 승인 게이트)",
    )
    args = ap.parse_args()

    if args.notify:
        res = notify_slot(args.slot, dry_run=args.dry_run)
    else:
        res = run_slot(args.slot, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))

    failed = [c for c in res["cards"] if c["status"].startswith(("오류", "스킵"))]
    if failed and len(failed) == len(res["cards"]):
        return 1          # 전부 실패면 워크플로를 빨갛게 만들어 알림이 오게 한다
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
