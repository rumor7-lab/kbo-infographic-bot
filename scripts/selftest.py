#!/usr/bin/env python3
"""셀프테스트 — 네트워크 없이 파이프라인 뼈대를 검증한다.

이 프로젝트는 샌드박스에서 실행 검증을 못 한 상태로 작성됐다. 그래서
로컬에서 가장 먼저 이걸 돌려야 한다. 3단계로 나뉜다.

  python scripts/selftest.py            # 1단계: 설정·엔진·템플릿 (네트워크 X)
  python scripts/selftest.py --render   # 2단계: 실제 PNG 3장 생성 (Playwright 필요)
  python scripts/selftest.py --live     # 3단계: KBO 사이트 실제 파싱 확인 (네트워크 O)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.render.layout_engine import Payload, Subject, select_layout  # noqa: E402

PASS, FAIL = "  [OK]", "  [FAIL]"
_fails = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _fails
    if cond:
        print(f"{PASS} {name}")
    else:
        _fails += 1
        print(f"{FAIL} {name} {detail}")


# ── 1단계: 설정 + 레이아웃 엔진 ──────────────────
def stage_config() -> dict:
    print("\n[1] 설정 파일")
    from src.render.renderer import load_cfg

    cfg = load_cfg()
    b = cfg["brand"]
    check("brand.yml 로드", "brand" in b and "colors" in b)
    check("10구단 컬러 정의", len(b["teams"]) == 10, f"({len(b['teams'])}개)")
    check("피드 캔버스 1080x1350", (b["canvas"]["feed"]["width"], b["canvas"]["feed"]["height"]) == (1080, 1350))
    check("릴스 캔버스 1080x1920", (b["canvas"]["reels"]["width"], b["canvas"]["reels"]["height"]) == (1080, 1920))

    slots = cfg["cards"]["slots"]
    check("슬롯 3개", len(slots) == 3, f"({list(slots)})")
    check("슬롯 시각 08:00/12:30/22:30",
          [s["at"] for s in slots.values()] == ["08:00", "12:30", "22:30"])

    cards = cfg["cards"]["cards"]
    missing = []
    for slot in slots.values():
        for cid in slot["cards"]:
            if cid not in cards:
                missing.append(cid)
    check("슬롯이 참조하는 카드가 모두 정의됨", not missing, f"누락: {missing}")

    rot = cards.get("rotating_topic", {}).get("rotation", {})
    bad = [v for v in rot.values() if v not in cards]
    check("요일 로테이션 카드 전부 존재", not bad, f"누락: {bad}")
    check("로테이션 7일 전부 지정", len(rot) == 7, f"({len(rot)}일)")
    return cfg


def stage_engine() -> None:
    print("\n[2] 레이아웃 자동 선택")

    # 유형 A — 다열 표
    table = Payload(
        card_id="t", title="어제 경기 결과",
        columns=["순위", "팀명", "경기", "승", "무", "패", "승률"],
        rows=[{"순위": i, "팀명": f"팀{i}", "경기": 90, "승": 50 - i,
               "무": 2, "패": 38 + i, "승률": round(0.6 - i * 0.02, 3)} for i in range(1, 11)],
    )
    lay, why = select_layout(table, {"layout": "auto"})
    check("다열 데이터 → table", lay == "table", f"→ {lay} ({why})")

    # 유형 B — 단일 지표
    chart = Payload(
        card_id="c", title="팀 승률",
        columns=["팀명", "승률"],
        rows=[{"팀명": f"팀{i}", "승률": round(0.6 - i * 0.02, 3)} for i in range(1, 11)],
    )
    lay, why = select_layout(chart, {"layout": "auto"})
    check("단일 지표 10행 → chart", lay == "chart", f"→ {lay} ({why})")

    # 유형 C — 사진 있음
    hero = Payload(card_id="h", title="연패 탈출",
                   subject=Subject(name="김윤하", team="키움", photo="/tmp/x.jpg",
                                   headline="연패 탈출"))
    lay, why = select_layout(hero, {"layout": "auto"})
    check("주인공 + 사진 → hero", lay == "hero", f"→ {lay} ({why})")

    # 유형 C 강등 — 사진 없음
    nophoto = Payload(card_id="h", title="연패 탈출",
                      subject=Subject(name="김윤하", team="키움", photo=None,
                                      headline="연패 탈출"),
                      columns=["순위", "선수명", "구속"],
                      rows=[{"순위": i, "선수명": f"선수{i}", "구속": 150 + i} for i in range(1, 6)])
    lay, why = select_layout(nophoto, {"layout": "hero", "fallback_layout": "table"})
    check("사진 없으면 hero → table 강등", lay == "table", f"→ {lay} ({why})")

    # 유형 D — 그리드(선수 여러 명 나열)
    grid = Payload(
        card_id="g", title="타격 리더 TOP 4",
        subjects=[Subject(name=f"선수{i}", team="키움", photo="/tmp/x.jpg") for i in range(4)],
    )
    lay, why = select_layout(grid, {"layout": "grid"})
    check("subjects 있음 + grid 지정 → grid", lay == "grid", f"→ {lay} ({why})")

    # 유형 D 강등 — 나열할 선수가 없음
    empty_grid = Payload(card_id="g2", title="타격 리더 TOP 4")
    lay, why = select_layout(empty_grid, {"layout": "grid", "fallback_layout": "table"})
    check("subjects 없으면 grid → table 강등", lay == "table", f"→ {lay} ({why})")

    # 행 수 초과 절단
    from src.render.layout_engine import truncate_rows
    big = Payload(card_id="b", title="x", columns=["선수명", "값"],
                  rows=[{"선수명": f"p{i}", "값": i} for i in range(40)])
    truncate_rows(big, "table")
    check("행 수 초과 시 절단 + 각주", len(big.rows) == 14 and "총 40건" in big.footnote_extra,
          f"({len(big.rows)}행, note={big.footnote_extra!r})")


def stage_title_sizing(cfg: dict) -> None:
    """타이틀 크기 자동 조절 안전장치 확인.

    핵심 회귀 테스트: 10행짜리 순위 차트에서 타이틀을 216px까지 키웠더니 막대가
    헤더 쪽으로 밀려 겹치는 사고가 실제로 있었다. 원인은 행 수를 고려하지 않고
    타이틀 크기부터 정했기 때문 — 이제는 행 수가 요구하는 최소 본문 공간을
    먼저 빼고 남는 예산 안에서만 타이틀을 키우므로, 여기서 그 제약이 실제로
    지켜지는지 카드별로 확인한다."""
    print("\n[title] 타이틀 크기 자동 조절 안전장치")
    from src.render.renderer import fit_title, min_body_px

    feed_h = cfg["brand"]["canvas"]["feed"]["height"]
    footer_h = cfg["brand"]["frame"]["footer_height"]

    # 실제 파이프라인에서 각 카드가 갖는 대략적인 행 수 (10구단/TOP10 계열은 10,
    # 하루 경기·미리보기 계열은 5 안팎). 정확한 값은 실행 시점 데이터에 따라
    # 달라지지만, 여기서는 '최악에 가까운' 쪽으로 잡아 안전 마진을 검증한다.
    ROW_HINTS = {
        "team_standings": 10, "hitter_leaders": 10, "pitcher_leaders": 10,
        "frustration_index": 10, "fa_list": 10, "salary_rank": 10,
        "milestone_watch": 10, "daily_recap": 5, "today_results": 5,
        "weekend_preview": 5, "weekly_digest": 5, "playoff_race": 5,
    }
    # 히어로/그리드는 사진 위 고정 배치(그리드는 격자 자체가 고정 폭/높이)라
    # 행 수 기반 헤더 예산 제약과 무관 — 이 테스트 대상에서 제외한다.
    HERO_CARDS = {"yesterday_heroes", "today_top_performer", "hitter_grid", "pitcher_grid"}

    cards = cfg["cards"]["cards"]
    worst_margin = None
    for cid, c in cards.items():
        if "title" not in c or cid in HERO_CARDS:
            continue  # 히어로는 사진 위 하단 고정 배치라 이 제약과 무관
        title = c["title"]
        declared = c.get("layout") or "auto"
        # chart 가 table 보다 행당 최소 높이가 더 크다(78px vs 46px) → 더 빡빡한 제약.
        # layout: auto 인 카드는 실행 시점 데이터에 따라 chart 로 뽑힐 수도 있으므로
        # 'auto'는 안전하게 chart(더 엄격한 쪽)로 가정해 검증한다.
        layout = "table" if declared == "table" else "chart"
        n_rows = ROW_HINTS.get(cid, 5)

        size, lines, header_px = fit_title(
            title, n_rows=n_rows, layout=layout, canvas_h=feed_h, footer_h=footer_h,
            has_kicker=True, has_title_sub=False,
        )
        body_min = min_body_px(n_rows, layout)
        margin = feed_h - header_px - footer_h - body_min
        ok = lines <= 2 and margin >= 0
        check(
            f"'{title}' ({layout}, {n_rows}행 가정) → {size}px, {lines}줄, 헤더 {header_px}px",
            ok, f"(여유 {margin}px)",
        )
        if worst_margin is None or margin < worst_margin[0]:
            worst_margin = (margin, cid, title)

    if worst_margin:
        print(f"       가장 타이트한 카드: {worst_margin[2]} (여유 {worst_margin[0]}px)")


def stage_playoff_odds() -> None:
    """가을야구 시나리오 몬테카를로 시뮬레이션 — 네트워크 없이 합성 순위표로 검증."""
    print("\n[playoff] 가을야구 확률 시뮬레이션")
    from src.compute.playoff_odds import SimulationError, simulate

    # 1위(76승)~10위(40승)로 확실한 격차를 준 합성 데이터.
    # 상위팀일수록 확률이 높아야 하고, 1위는 사실상 100%에 가까워야 한다.
    synthetic = [
        {"팀명": f"팀{i}", "_team": f"팀{i}", "경기": 100, "승": 76 - i * 4,
         "무": 0, "패": 24 + i * 4, "승률": round((76 - i * 4) / 100, 3)}
        for i in range(10)
    ]
    try:
        out = simulate(synthetic, sims=20_000, seed=42)
        check("10개 팀 전부 반환", len(out) == 10, f"({len(out)}개)")
        probs = [float(r["가을야구확률"].rstrip("%")) for r in out]
        check("확률 내림차순 정렬", probs == sorted(probs, reverse=True), f"{probs}")
        check("1위 팀 확률 90% 이상", probs[0] >= 90.0, f"(1위={probs[0]}%)")
        check("꼴찌 팀 확률 10% 이하", probs[-1] <= 10.0, f"(꼴찌={probs[-1]}%)")
    except SimulationError as e:
        check("시뮬레이션 실행", False, str(e))

    # 팀 수가 10개가 아니면 명확히 에러를 내야 한다 (조용히 이상한 결과를 주면 안 됨)
    try:
        simulate(synthetic[:9], sims=1000)
        check("팀 9개 → SimulationError", False, "예외가 발생하지 않음")
    except SimulationError:
        check("팀 9개 → SimulationError", True)


def stage_validate(cfg: dict) -> None:
    print("\n[3] 검증 룰")
    from src.validate.rules import validate

    ten = [{"팀명": f"팀{i}", "_team": f"팀{i}", "경기": 90, "승": 50, "무": 2, "패": 38,
            "승률": 0.556} for i in range(1, 11)]
    rep = validate(ten, ["팀명", "승률"], cfg, card_id="hitter_leaders", label_col="팀명")
    check("정상 데이터 통과", rep.ok, rep.summary())

    # 업로드 사례 재현: 동일 선수명 9회 반복 (STATIZ 최고 구속 카드)
    dup = [{"선수명": "전준표", "구속": 157 - i // 3} for i in range(9)]
    rep = validate(dup, ["선수명", "구속"], cfg, card_id="x", label_col="선수명")
    check("동일 이름 반복 → 발행 차단", not rep.ok, rep.summary())

    # 승/무/패 합 불일치
    bad = [{"팀명": "삼성", "경기": 90, "승": 57, "무": 2, "패": 40, "승률": 0.62}]
    rep = validate(bad, ["팀명"], cfg, card_id="x", label_col="팀명")
    check("승+무+패 ≠ 경기수 → 차단", not rep.ok, rep.summary())

    # 승률 범위 초과
    bad2 = [{"팀명": "LG", "승률": 1.62}]
    rep = validate(bad2, ["팀명"], cfg, card_id="x", label_col="팀명")
    check("승률 범위 초과 → 차단", not rep.ok, rep.summary())

    # 누적 스탯 역행
    prev = [{"선수명": "김도영", "홈런": 30}]
    cur = [{"선수명": "김도영", "홈런": 12}]
    rep = validate(cur, ["선수명", "홈런"], cfg, card_id="x", label_col="선수명", prev_rows=prev)
    check("누적 스탯 역행 → 차단", not rep.ok, rep.summary())

    # 히어로 카드(행 없음)는 주인공을 검증
    from src.render.layout_engine import Subject
    from src.validate.rules import validate_subject

    good = Subject(name="김윤하", team="키움", photo="/tmp/x.jpg", headline="연패 탈출")
    check("정상 히어로 통과", validate_subject(good, card_id="x").ok)
    check("이름 없는 히어로 차단", not validate_subject(
        Subject(name="", headline="x"), card_id="x").ok)
    check("헤드라인 없는 히어로 차단", not validate_subject(
        Subject(name="김윤하", headline=""), card_id="x").ok)


def stage_template(cfg: dict) -> None:
    print("\n[4] 템플릿 렌더 (HTML 문자열)")
    from src.render.renderer import render_html

    samples = {
        "table": Payload(
            card_id="daily_recap", title="어제 경기 결과", kicker="DAILY",
            columns=["경기", "스코어", "승리"],
            rows=[{"경기": "LG vs 삼성", "스코어": "3 : 5", "승리": "삼성", "_team": "삼성"}] * 5,
            as_of="2026.07.29",
        ),
        "chart": Payload(
            card_id="team_standings", title="KBO 팀 순위", kicker="STANDINGS",
            columns=["팀명", "승률"], metric="승률",
            rows=[{"팀명": t, "_team": t, "승률": round(0.62 - i * 0.025, 3),
                   "_sub": f"{57 - i * 2}승 2무 {35 + i * 2}패"}
                  for i, t in enumerate(["삼성", "KT", "LG", "KIA", "두산",
                                         "한화", "NC", "롯데", "SSG", "키움"])],
            as_of="2026.07.29",
        ),
        "hero": Payload(
            card_id="today_top_performer", title="연패 탈출", kicker="HIGHLIGHT",
            subject=Subject(name="김윤하", team="키움", photo=str(ROOT / "assets" / "sample.jpg"),
                            headline="연패 탈출", sub="데뷔 첫 승 이후 개인 18연패 탈출",
                            stats=[{"label": "이닝", "value": "5.0"},
                                   {"label": "실점", "value": "3"},
                                   {"label": "간격", "value": "731일"}]),
            as_of="2026.07.29",
        ),
        "grid": Payload(
            card_id="hitter_grid", title="타격 리더 TOP 4", kicker="PLAYER GRID",
            subjects=[
                Subject(name="선수1", team="삼성", photo=str(ROOT / "assets" / "sample.jpg"),
                        stats=[{"label": "타율", "value": "0.381"},
                               {"label": "홈런", "value": "13"},
                               {"label": "타점", "value": "51"}]),
                Subject(name="선수2", team="LG", photo=None,   # 사진 실패 폴백 케이스도 같이 확인
                        stats=[{"label": "타율", "value": "0.365"},
                               {"label": "홈런", "value": "10"},
                               {"label": "타점", "value": "44"}]),
                Subject(name="선수3", team="KT", photo=str(ROOT / "assets" / "sample.jpg"),
                        stats=[{"label": "타율", "value": "0.352"},
                               {"label": "홈런", "value": "9"},
                               {"label": "타점", "value": "40"}]),
                Subject(name="선수4", team="KIA", photo=str(ROOT / "assets" / "sample.jpg"),
                        stats=[{"label": "타율", "value": "0.348"},
                               {"label": "홈런", "value": "8"},
                               {"label": "타점", "value": "38"}]),
            ],
            as_of="2026.07.29",
        ),
    }
    for want, payload in samples.items():
        html, lay, why = render_html(payload, {"layout": want}, cfg)
        ok = lay == want and len(html) > 1500 and "frame-foot" in html
        check(f"{want} 템플릿 렌더", ok, f"→ {lay}, {len(html)}bytes")
        # 브랜드 프레임 일관성 — 세 유형 모두 동일 요소를 가져야 한다
        for el in ("badge", "frame-head", "frame-body", "frame-foot"):
            check(f"  {want}: .{el} 존재", el in html)

    # 모션(릴스) 렌더
    html, _, _ = render_html(samples["chart"], {"layout": "chart"}, cfg, motion=True)
    check("릴스 모션 CSS 주입", "growBar" in html and "1920px" in html)


# ── 2단계: 실제 이미지 생성 ──────────────────────
def stage_render(cfg: dict) -> None:
    print("\n[5] 실제 PNG 생성")
    from src.render.renderer import render_html, shoot_png

    out = ROOT / "out" / "selftest"
    out.mkdir(parents=True, exist_ok=True)
    feed = cfg["brand"]["canvas"]["feed"]

    for want, payload in _render_samples().items():
        html, lay, _ = render_html(payload, {"layout": want}, cfg)
        try:
            p = shoot_png(html, out / f"{want}.png", feed["width"], feed["height"])
            size = p.stat().st_size
            check(f"{want}.png 생성", size > 20_000, f"({size // 1024}KB)")
        except Exception as e:  # noqa: BLE001
            check(f"{want}.png 생성", False, str(e)[:120])
    print(f"\n  → {out} 를 열어 눈으로 확인하세요. 세 장의 아웃라인이 동일해야 정상입니다.")


def _render_samples() -> dict:
    from src.render.layout_engine import Subject

    return {
        "grid": Payload(
            card_id="hitter_grid", title="타격 리더 TOP 4", kicker="PLAYER GRID",
            subjects=[
                Subject(name=f"선수{i}", team=t, photo=str(ROOT / "assets" / "sample.jpg"),
                        stats=[{"label": "타율", "value": f"0.{380 - i * 8}"},
                               {"label": "홈런", "value": str(13 - i * 2)},
                               {"label": "타점", "value": str(51 - i * 4)}])
                for i, t in enumerate(["삼성", "LG", "KT", "KIA"])
            ],
            as_of="2026.07.29",
        ),
        "table": Payload(
            card_id="hitter_leaders", title="타격 순위 TOP 10", kicker="BATTING",
            columns=["순위", "선수명", "팀명", "타율", "홈런", "타점"],
            rows=[{"순위": i, "선수명": f"선수{i}", "팀명": t, "_team": t,
                   "타율": f"0.{340 - i * 4}", "홈런": 30 - i, "타점": 90 - i * 3}
                  for i, t in enumerate(["삼성", "KT", "LG", "KIA", "두산",
                                         "한화", "NC", "롯데", "SSG", "키움"], 1)],
            as_of="2026.07.29",
        ),
        "chart": Payload(
            card_id="team_standings", title="KBO 팀 승률 순위", kicker="STANDINGS",
            columns=["팀명", "승률"], metric="승률",
            rows=[{"팀명": t, "_team": t, "승률": f"0.{620 - i * 25}",
                   "_sub": f"{57 - i * 2}승 2무 {35 + i * 2}패"}
                  for i, t in enumerate(["삼성", "KT", "LG", "KIA", "두산",
                                         "한화", "NC", "롯데", "SSG", "키움"])],
            as_of="2026.07.29",
        ),
    }


# ── 3단계: 실제 사이트 파싱 ──────────────────────
def stage_live() -> None:
    print("\n[6] KBO 사이트 실제 파싱 (네트워크)")
    from src.collect import kbo_official as kbo

    try:
        s = kbo.standings()
        check("팀 순위 파싱", len(s.rows) == 10, f"({len(s.rows)}팀)")
        print(f"       1위 {s.rows[0].get('팀명')} {s.rows[0].get('승률')} "
              f"({s.rows[0].get('_sub')})")
    except Exception as e:  # noqa: BLE001
        check("팀 순위 파싱", False, f"{type(e).__name__}: {e}")
        print("       → SELECTORS['standings_table'] 확인 필요")

    for kind, label in (("hitter", "타격"), ("pitcher", "투수")):
        try:
            s = kbo.leaders(kind)
            ok = len(s.rows) == 10 and all("-" not in str(r[s.columns[3]]) for r in s.rows)
            check(f"{label} 순위 파싱", ok, f"({len(s.rows)}명, 컬럼={s.columns})")
            print(f"       1위 {s.rows[0].get('선수명')} "
                  f"{s.columns[3]} {s.rows[0].get(s.columns[3])}")
            if s.meta.get("unknown_columns"):
                print(f"       ! 미매핑 컬럼: {s.meta['unknown_columns']}")
        except Exception as e:  # noqa: BLE001
            check(f"{label} 순위 파싱", False, f"{type(e).__name__}: {e}")

    try:
        s = kbo.games()
        check("어제 경기 파싱", len(s.rows) > 0, f"({len(s.rows)}경기)")
        for r in s.rows:
            print(f"       {r['경기']}  {r['스코어']}  → {r['승리']}")
    except Exception as e:  # noqa: BLE001
        check("어제 경기 파싱", False, f"{type(e).__name__}: {e}")
        print("       → Playwright 설치 여부 / btnPreDate 셀렉터 확인")

    try:
        s = kbo.milestone_watch()
        check("마일스톤 계산", len(s.rows) > 0, f"({len(s.rows)}건)")
        for r in s.rows[:3]:
            print(f"       {r['선수명']}({r['팀명']}) {r['기록']} — {r['남은개수']}개 남음")
    except Exception as e:  # noqa: BLE001
        # 임박한 기록이 없는 날도 있으므로 실패로 세지 않는다
        print(f"  [skip] 마일스톤: {e}")

    try:
        s = kbo.team_risp_worst()
        check("득점권타율(RISP) 파싱", len(s.rows) == 10, f"({len(s.rows)}팀)")
        worst = s.rows[0]
        print(f"       가장 낮은 팀: {worst['팀명']} {worst['득점권타율']}")
    except Exception as e:  # noqa: BLE001
        check("득점권타율(RISP) 파싱", False, f"{type(e).__name__}: {e}")
        print("       → SELECTORS['team_hitter_table'] 확인 필요")

    try:
        from src.compute.playoff_odds import simulate

        s = kbo.standings()
        odds = simulate(s.rows, sims=20_000)
        check("가을야구 확률(실데이터) 계산", len(odds) == 10, f"({len(odds)}팀)")
        for r in odds[:3]:
            print(f"       {r['팀명']} {r['가을야구확률']}")
    except Exception as e:  # noqa: BLE001
        check("가을야구 확률(실데이터) 계산", False, f"{type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", action="store_true", help="실제 PNG 생성 (Playwright 필요)")
    ap.add_argument("--live", action="store_true", help="KBO 사이트 실제 파싱")
    args = ap.parse_args()

    print("=" * 56)
    print(" KBO 인포그래픽 봇 셀프테스트")
    print("=" * 56)

    cfg = stage_config()
    stage_engine()
    stage_title_sizing(cfg)
    stage_playoff_odds()
    stage_validate(cfg)
    stage_template(cfg)
    if args.render:
        stage_render(cfg)
    if args.live:
        stage_live()

    print("\n" + "=" * 56)
    if _fails:
        print(f" 실패 {_fails}건 — 위 [FAIL] 항목을 먼저 해결하세요")
    else:
        print(" 전부 통과")
    print("=" * 56)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
