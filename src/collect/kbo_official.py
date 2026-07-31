"""KBO 공식 홈페이지 수집기 (1차 소스).

셀렉터·컬럼 구조는 2026-07-30 실제 페이지에서 확인했다. 확인 내역은 각 함수 주석에.

원칙
  - 저빈도 접근: 하루 3회 슬롯 × 필요한 페이지만. 병렬 크롤링 금지.
  - 수치(사실 데이터)만 가져와 자체 디자인으로 재구성한다. 표/이미지/기사 원본은 쓰지 않는다.
  - 파싱 실패 시 절대 추측하지 않고 CollectError 를 던진다 → 상위에서 발행 스킵.
  - 셀렉터는 SELECTORS 한 곳에만 있다. 사이트 개편 시 여기만 고치면 된다.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[2]
SNAP_DIR = ROOT / "data" / "snapshots"

BASE = "https://www.koreabaseball.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

URLS = {
    "standings": f"{BASE}/Record/TeamRank/TeamRankDaily.aspx",
    "hitter": f"{BASE}/Record/Player/HitterBasic/Basic1.aspx",
    "pitcher": f"{BASE}/Record/Player/PitcherBasic/Basic1.aspx",
    "team_hitter_2": f"{BASE}/Record/Team/Hitter/Basic2.aspx",
    "scoreboard": f"{BASE}/Schedule/ScoreBoard.aspx",
    "correction": f"{BASE}/Record/RecordCorrect/RecordCorrect.aspx",
}

# ── 실측 확인된 셀렉터 (2026-07-30) ──────────────
SELECTORS = {
    # 팀 순위: table.tData, 10행. 같은 페이지에 상대전적 표(pnlVsTeam)가 하나 더 있어 udpRecord 로 한정 필수
    "standings_table": "#cphContents_cphContents_cphContents_udpRecord table.tData",
    # 선수 기록: table.tData01, 타자 30행 / 투수 20행
    "leaders_table": "#cphContents_cphContents_cphContents_udpContent table.tData01",
    # 팀 타자 기록(2페이지): 10행, RISP 등 포함
    "team_hitter_table": "#cphContents_cphContents_cphContents_udpContent table",
    # 스코어보드: 경기당 .smsScore 블록 1개
    "scoreboard_block": ".smsScore",
    "scoreboard_linescore": "table.tScore",
    "scoreboard_date_input": "input[id$='hfSearchDate']",
    "scoreboard_prev_btn": "#cphContents_cphContents_cphContents_btnPreDate",
}

# KBO 는 선수 기록 표에 영문 약어를 쓴다. 카드에 그대로 쓰면 안 되므로 한글로 매핑.
HITTER_LABELS = {
    "순위": "순위", "선수명": "선수명", "팀명": "팀명",
    "AVG": "타율", "G": "경기", "PA": "타석", "AB": "타수", "R": "득점",
    "H": "안타", "2B": "2루타", "3B": "3루타", "HR": "홈런", "TB": "총루타",
    "RBI": "타점", "SAC": "희생타", "SF": "희생플라이",
}
PITCHER_LABELS = {
    "순위": "순위", "선수명": "선수명", "팀명": "팀명",
    "ERA": "평균자책", "G": "경기", "W": "승", "L": "패", "SV": "세이브",
    "HLD": "홀드", "WPCT": "승률", "IP": "이닝", "H": "피안타", "HR": "피홈런",
    "BB": "볼넷", "HBP": "사구", "SO": "탈삼진", "R": "실점", "ER": "자책",
    "WHIP": "WHIP",
}
# 카드에 실을 컬럼 (표가 너무 넓으면 못 읽는다)
HITTER_SHOW = ["순위", "선수명", "팀명", "타율", "홈런", "타점"]
PITCHER_SHOW = ["순위", "선수명", "팀명", "평균자책", "승", "탈삼진"]

TEAM_ALIASES = {
    "두산 베어스": "두산", "LG 트윈스": "LG", "삼성 라이온즈": "삼성",
    "KIA 타이거즈": "KIA", "KT 위즈": "KT", "SSG 랜더스": "SSG",
    "롯데 자이언츠": "롯데", "한화 이글스": "한화", "NC 다이노스": "NC",
    "키움 히어로즈": "키움",
}
VALID_TEAMS = ("삼성", "KT", "LG", "KIA", "두산", "한화", "NC", "롯데", "SSG", "키움")


class CollectError(RuntimeError):
    """수집/파싱 실패. 이 예외가 나면 발행을 스킵한다."""


@dataclass
class Snapshot:
    key: str
    fetched_at: str
    as_of: str
    columns: list[str]
    rows: list[dict[str, Any]]
    meta: dict[str, Any]

    def save(self) -> Path:
        d = SNAP_DIR / datetime.now(KST).strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{self.key}.json"
        p.write_text(
            json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return p


# ── 저수준 유틸 ──────────────────────────────────
_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
_last_call = 0.0
MIN_INTERVAL = 2.0


def _get(url: str, params: dict | None = None) -> BeautifulSoup:
    global _last_call
    wait = MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    try:
        r = _session.get(url, params=params, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        raise CollectError(f"요청 실패 {url}: {e}") from e
    finally:
        _last_call = time.time()
    return BeautifulSoup(r.text, "html.parser")


def _table_to_rows(soup: BeautifulSoup, selector: str) -> tuple[list[str], list[dict]]:
    table = soup.select_one(selector)
    if table is None:
        raise CollectError(f"표를 찾지 못했습니다 (selector={selector}). 사이트 개편 의심")

    head = [th.get_text(strip=True) for th in table.select("thead th")]
    if not head:
        raise CollectError(f"표 헤더를 읽지 못했습니다 (selector={selector})")

    rows: list[dict] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 2:
            continue
        n = min(len(cells), len(head))
        rows.append({head[i]: cells[i] for i in range(n)})
    if not rows:
        raise CollectError("표에 데이터 행이 없습니다")
    return head, rows


def _norm_team(v: str) -> str:
    v = (v or "").strip()
    return TEAM_ALIASES.get(v, v)


def _now_str() -> str:
    return datetime.now(KST).strftime("%Y.%m.%d")


# ── 팀 순위 ──────────────────────────────────────
def standings() -> Snapshot:
    """팀 순위. 유형 B(차트) 로 렌더되는 대표 카드.

    실측 헤더(2026-07-30):
      순위 | 팀명 | 경기 | 승 | 패 | 무 | 승률 | 게임차 | 최근10경기 | 연속 | 홈 | 방문
      ※ 승-패-무 순서. 승-무-패 아님.
    """
    soup = _get(URLS["standings"])
    _, rows = _table_to_rows(soup, SELECTORS["standings_table"])

    out = []
    for r in rows:
        team = _norm_team(r.get("팀명", ""))
        if team not in VALID_TEAMS:
            continue
        out.append(
            {
                "순위": r.get("순위", ""),
                "팀명": team,
                "_team": team,
                "경기": r.get("경기", ""),
                "승": r.get("승", ""),
                "패": r.get("패", ""),
                "무": r.get("무", ""),
                "승률": r.get("승률", ""),
                "게임차": r.get("게임차", ""),
                "최근10경기": r.get("최근10경기", ""),
                "연속": r.get("연속", ""),
                # 바 차트 안쪽 보조 텍스트
                "_sub": f"{r.get('승', '')}승 {r.get('무', '')}무 {r.get('패', '')}패",
            }
        )
    if len(out) != 10:
        raise CollectError(f"팀 수가 10개가 아닙니다: {len(out)}개")

    s = Snapshot(
        key="standings",
        fetched_at=datetime.now(KST).isoformat(),
        as_of=_now_str(),
        columns=["순위", "팀명", "경기", "승", "무", "패", "승률", "게임차"],
        rows=out,
        meta={"source": "KBO 공식", "url": URLS["standings"], "metric": "승률"},
    )
    s.save()
    return s


# ── 선수 기록 ────────────────────────────────────
def leaders(kind: str = "hitter", top: int = 10) -> Snapshot:
    """타격/투수 순위 TOP N. 유형 A(표) 로 렌더.

    실측 헤더(2026-07-30):
      타자: 순위 선수명 팀명 AVG G PA AB R H 2B 3B HR TB RBI SAC SF  (30행)
      투수: 순위 선수명 팀명 ERA G W L SV HLD WPCT IP H HR BB HBP SO R ER WHIP (20행)
    """
    if kind not in ("hitter", "pitcher"):
        raise ValueError(kind)

    labels = HITTER_LABELS if kind == "hitter" else PITCHER_LABELS
    show = HITTER_SHOW if kind == "hitter" else PITCHER_SHOW

    soup = _get(URLS[kind])
    head, rows = _table_to_rows(soup, SELECTORS["leaders_table"])

    unknown = [h for h in head if h not in labels]
    if unknown:
        # 컬럼이 추가된 것 자체는 문제가 아니지만 로그로 남길 가치가 있다
        pass

    out = []
    for r in rows[:top]:
        ko = {labels.get(k, k): v for k, v in r.items()}
        row = {c: ko.get(c, "-") for c in show}
        row["_team"] = _norm_team(ko.get("팀명", ""))
        row["_all"] = ko           # 마일스톤 계산용 전체 값 보존
        out.append(row)

    if not out:
        raise CollectError(f"{kind} 순위 데이터가 비었습니다")

    s = Snapshot(
        key=f"{kind}_leaders",
        fetched_at=datetime.now(KST).isoformat(),
        as_of=_now_str(),
        columns=show,
        rows=out,
        meta={"source": "KBO 공식", "url": URLS[kind], "kind": kind,
              "unknown_columns": unknown},
    )
    s.save()
    return s


# ── 팀 타자 심화 기록 (RISP 등) ──────────────────
def team_risp_worst(top: int = 10) -> Snapshot:
    """득점권 타율(RISP) 하위 팀. 유형 B(차트) 로 렌더.

    실측 헤더(2026-07-30, Record/Team/Hitter/Basic2.aspx):
      순위 팀명 AVG BB IBB HBP SO GDP SLG OBP OPS MH RISP PH-BA  (10행)

    원래 기획은 '팀별 잔루 WORST'였으나 KBO 공식에는 팀 시즌 누적 잔루 통계가
    없다(타자/투수/주루 페이지 전부 확인). 잔루 랭킹은 STATIZ 전용 데이터라
    크롤링 금지 정책상 대신 이 지표를 쓴다.
    """
    soup = _get(URLS["team_hitter_2"])
    head, rows = _table_to_rows(soup, SELECTORS["team_hitter_table"])

    if "RISP" not in head:
        raise CollectError(f"RISP 컬럼을 찾지 못했습니다 (헤더: {head}). 사이트 개편 의심")

    out = []
    for r in rows:
        team = _norm_team(r.get("팀명", ""))
        if team not in VALID_TEAMS:
            continue
        risp = r.get("RISP", "")
        try:
            risp_val = float(risp)
        except ValueError:
            continue
        out.append({"팀명": team, "_team": team, "득점권타율": f"{risp_val:.3f}", "_sort": risp_val})

    if len(out) != 10:
        raise CollectError(f"팀 수가 10개가 아닙니다: {len(out)}개")

    out.sort(key=lambda r: r["_sort"])  # 낮은 순 = WORST 부터
    for r in out:
        del r["_sort"]

    s = Snapshot(
        key="team_risp_worst",
        fetched_at=datetime.now(KST).isoformat(),
        as_of=_now_str(),
        columns=["팀명", "득점권타율"],
        rows=out,
        meta={"source": "KBO 공식", "url": URLS["team_hitter_2"], "metric": "득점권타율"},
    )
    s.save()
    return s


# ── 경기 결과 ────────────────────────────────────
def games(target: date | None = None) -> Snapshot:
    """특정 날짜 경기 결과.

    실측 구조(2026-07-30):
      - 경기당 `.smsScore` 블록. 안에 `table.tScore` 라인스코어
        헤더: TEAM 1..12 R H E B / 1행=원정, 2행=홈
      - 날짜 이동: hidden `input[id$='hfSearchDate']` (YYYYMMDD) +
        `#cphContents_cphContents_cphContents_btnPreDate` 클릭 → 하루 전으로 postback
      → 오늘부터 목표 날짜까지 '전일' 버튼을 눌러 이동하는 방식이 가장 안정적이다.
    """
    from playwright.sync_api import sync_playwright

    today = datetime.now(KST).date()
    target = target or (today - timedelta(days=1))
    back = (today - target).days
    if back < 0:
        raise CollectError("미래 날짜는 조회할 수 없습니다")
    if back > 14:
        raise CollectError(f"{back}일 전 데이터는 이 방식으로 조회하지 않습니다")

    ymd = target.strftime("%Y%m%d")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        try:
            page.goto(URLS["scoreboard"], wait_until="domcontentloaded", timeout=30000)
            # hfSearchDate 는 <input type="hidden"> 이라 절대 "visible" 상태가 안 된다.
            # 기본 wait_for_selector 는 visible 을 기다리므로 DOM에 붙기만(attached) 하면
            # 되는 걸로 바꿔야 한다 — 실제로 이 타임아웃 버그가 있었다.
            page.wait_for_selector(
                SELECTORS["scoreboard_date_input"], state="attached", timeout=15000
            )

            for _ in range(back):
                cur = page.input_value(SELECTORS["scoreboard_date_input"])
                if cur == ymd:
                    break
                page.click(SELECTORS["scoreboard_prev_btn"])
                page.wait_for_function(
                    """(args) => {
                        const el = document.querySelector(args.sel);
                        return el && el.value !== args.prev;
                    }""",
                    arg={"sel": SELECTORS["scoreboard_date_input"], "prev": cur},
                    timeout=15000,
                )

            reached = page.input_value(SELECTORS["scoreboard_date_input"])
            if reached != ymd:
                raise CollectError(f"날짜 이동 실패: 목표 {ymd}, 도달 {reached}")
            html = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select(SELECTORS["scoreboard_block"])
    if not blocks:
        raise CollectError(f"{ymd} 경기 블록이 0건 (경기 없는 날일 수 있음)")

    out = []
    for b in blocks:
        t = b.select_one(SELECTORS["scoreboard_linescore"])
        if t is None:
            continue
        trs = (t.find("tbody") or t).find_all("tr")
        if len(trs) < 2:
            continue

        def parse(tr) -> tuple[str, dict[str, str]]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            if not cells:
                return "", {}
            # 뒤에서부터 B, E, H, R
            tail = cells[-4:]
            return _norm_team(cells[0]), {
                "R": tail[0], "H": tail[1], "E": tail[2], "B": tail[3]
            }

        away, a = parse(trs[0])
        home, h = parse(trs[1])
        if away not in VALID_TEAMS or home not in VALID_TEAMS:
            continue
        try:
            ar, hr = int(a["R"]), int(h["R"])
        except (KeyError, ValueError):
            continue

        winner = home if hr > ar else (away if ar > hr else "무승부")
        place_el = b.select_one(".place")
        out.append(
            {
                "경기": f"{away} vs {home}",
                "원정": away,
                "홈": home,
                "스코어": f"{ar} : {hr}",
                "승리": winner,
                "안타": f"{a['H']} : {h['H']}",
                "구장": place_el.get_text(" ", strip=True).split()[0] if place_el else "",
                "_team": winner if winner in VALID_TEAMS else home,
            }
        )

    if not out:
        raise CollectError(f"{ymd} 파싱된 경기가 0건입니다")

    s = Snapshot(
        key=f"games_{ymd}",
        fetched_at=datetime.now(KST).isoformat(),
        as_of=target.strftime("%Y.%m.%d"),
        columns=["경기", "스코어", "승리"],
        rows=out,
        meta={"source": "KBO 공식", "date": ymd, "url": URLS["scoreboard"]},
    )
    s.save()
    return s


# ── 마일스톤 (자체 계산) ─────────────────────────
# '예상 달성 기록' 페이지는 게시판 목록이라(본문/첨부에 표가 들어 있음) 직접 파싱하지 않는다.
# 대신 선수 기록에서 라운드 넘버까지 남은 개수를 직접 계산한다. 의존성이 줄고 검증도 쉽다.
MILESTONES = {
    "홈런": [10, 20, 30, 40, 50],
    "타점": [50, 80, 100, 120],
    "안타": [100, 150, 180, 200],
    "탈삼진": [100, 150, 180, 200],
    "승": [10, 15, 20],
}


def milestone_watch(within: int = 5, top: int = 10) -> Snapshot:
    """라운드 기록 달성이 `within` 개 이내로 임박한 선수 목록."""
    rows: list[dict[str, Any]] = []

    for kind, keys in (("hitter", ("홈런", "타점", "안타")), ("pitcher", ("탈삼진", "승"))):
        try:
            snap = leaders(kind, top=30)
        except CollectError:
            continue
        for r in snap.rows:
            allv = r.get("_all", {})
            for key in keys:
                raw = allv.get(key)
                if raw in (None, "", "-"):
                    continue
                try:
                    cur = int(float(str(raw).replace(",", "")))
                except ValueError:
                    continue
                for goal in MILESTONES[key]:
                    left = goal - cur
                    if 0 < left <= within:
                        rows.append(
                            {
                                "선수명": allv.get("선수명", ""),
                                "팀명": r.get("_team", ""),
                                "_team": r.get("_team", ""),
                                "기록": f"{key} {goal}",
                                "현재": cur,
                                "남은개수": left,
                            }
                        )
                        break

    if not rows:
        raise CollectError("임박한 마일스톤이 없습니다 (오늘은 다른 카드로 대체)")

    rows.sort(key=lambda x: x["남은개수"])
    s = Snapshot(
        key="milestone_watch",
        fetched_at=datetime.now(KST).isoformat(),
        as_of=_now_str(),
        columns=["선수명", "팀명", "기록", "현재", "남은개수"],
        rows=rows[:top],
        meta={"source": "KBO 공식 기록 기반 자체 계산", "within": within},
    )
    s.save()
    return s


# ── 기록 정정 감지 ───────────────────────────────
def has_recent_corrections(days: int = 2) -> bool:
    """기록 정정이 최근에 있었는지 확인. 있으면 실행 로그에 경고를 남긴다."""
    try:
        soup = _get(URLS["correction"])
        text = soup.get_text(" ", strip=True)
        today = datetime.now(KST).date()
        for i in range(days + 1):
            d = today - timedelta(days=i)
            if d.strftime("%Y.%m.%d") in text or d.strftime("%Y-%m-%d") in text:
                return True
    except CollectError:
        pass
    return False
