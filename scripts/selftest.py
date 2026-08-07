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

from src.render.layout_engine import NewsCard, Payload, Subject, select_layout  # noqa: E402

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

    # 유형 E — 뉴스카드. 사진이 곧 콘텐츠라 강등이 아니라 '발행 중단'이 정답이다.
    from src.render.layout_engine import LayoutError

    news_ok = Payload(
        card_id="n1", title="원태인 FA",
        news=NewsCard(line1="원태인, FA 나오면 200억인데..", photo="/tmp/x.jpg"),
    )
    lay, why = select_layout(news_ok, {"layout": "newscard"})
    check("사진 있음 + newscard 지정 → newscard", lay == "newscard", f"→ {lay} ({why})")

    news_nophoto = Payload(
        card_id="n2", title="원태인 FA",
        news=NewsCard(line1="원태인, FA 나오면 200억인데.."),
    )
    try:
        select_layout(news_nophoto, {"layout": "newscard", "fallback_layout": "table"})
        check("사진 없으면 newscard 발행 중단", False, "→ 강등돼서 통과해버림")
    except LayoutError as e:
        check("사진 없으면 newscard 발행 중단", True, f"→ {str(e)[:40]}")

    news_notext = Payload(card_id="n3", title="x")
    try:
        select_layout(news_notext, {"layout": "newscard"})
        check("문구 없으면 newscard 발행 중단", False, "→ 통과해버림")
    except LayoutError:
        check("문구 없으면 newscard 발행 중단", True)

    # 행 수 초과 절단
    from src.render.layout_engine import truncate_rows
    big = Payload(card_id="b", title="x", columns=["선수명", "값"],
                  rows=[{"선수명": f"p{i}", "값": i} for i in range(40)])
    truncate_rows(big, "table")
    check("행 수 초과 시 절단 + 각주", len(big.rows) == 14 and "총 40건" in big.footnote_extra,
          f"({len(big.rows)}행, note={big.footnote_extra!r})")


def stage_news_trend() -> None:
    """화제 감지 클러스터링 — 네트워크 없이 가짜 기사로 검증."""
    print("\n[3.5] 뉴스 화제 감지 (클러스터링)")
    from datetime import datetime, timedelta

    from src.collect.news_trend import KST, Article, _clean_title, cluster

    now = datetime.now(KST)

    def A(title: str, outlet: str, mins: int) -> Article:
        return Article(title=title, link=f"https://{outlet}/x{mins}",
                       published=now - timedelta(minutes=mins), outlet=outlet)

    arts = [
        A("원태인 FA 시장 나오면 200억 예상", "a.co", 10),
        A("삼성 원태인, FA 대박 예고... 200억설", "b.co", 25),
        A("원태인 FA 앞두고 MLB 관심", "c.co", 40),
        A("원태인, 메이저리그 스카우터도 주목", "d.co", 55),
        A("김서현 제구 난조 심각", "a.co", 15),
        A("한화 김서현, 35구 중 볼 25개", "b.co", 30),
        A("김서현 부진에 한화 팬들 한숨", "c.co", 45),
        A("오늘 프로야구 경기 일정 안내", "e.co", 20),
        A("류현진 은퇴 시사 발언", "a.co", 5),
        A("한화 류현진, 은퇴 관련 입 열었다", "b.co", 12),
        A("류현진 은퇴설에 구단 공식 입장", "c.co", 18),
        A("류현진 은퇴 언급 파장 확산", "d.co", 22),
    ]
    topics = {t.key: t for t in cluster(arts)}

    # 표현이 달라도 같은 인물이면 묶여야 한다
    ryu = next((t for t in topics.values() if "류현진" in t.keywords), None)
    check("표현 달라도 같은 인물끼리 묶임", ryu is not None and ryu.article_count == 4,
          f"({ryu.article_count if ryu else 0}건)")

    # 같은 팀의 다른 사건이 팀명으로 이어지면 안 된다
    check("팀명만으로는 다른 사건이 안 묶임",
          ryu is not None and not any("김서현" in a.title for a in ryu.articles))

    # 주제어가 없어 실제로 놓쳤던 케이스 — 교집합으로 좁히면 이 기사를 놓친다
    won = next((t for t in topics.values() if "원태인" in t.keywords), None)
    check("주제어 확장으로 파생 기사도 포함",
          won is not None and won.article_count == 4, f"({won.article_count if won else 0}건)")

    # 대표어는 '희귀어'가 아니라 '반복되는 주인공'이어야 한다
    check("대표어가 사건 주인공", ryu is not None and "류현진" in ryu.key, f"(key={ryu.key if ryu else ''!r})")

    # 팀은 묶인 기사 전체에서 추정 (제목 하나엔 팀명이 없을 수 있음)
    check("클러스터 전체로 팀 추정", ryu is not None and ryu.team == "한화",
          f"(team={ryu.team if ryu else None})")

    # 매체 수는 중복 매체를 1로 센다
    kim = next((t for t in topics.values() if "김서현" in t.keywords), None)
    check("매체 수는 중복 제거", kim is not None and kim.outlet_count == 3,
          f"({kim.outlet_count if kim else 0}곳)")

    check("제목 태그·머리표 제거",
          _clean_title("<b>원태인</b> FA &quot;200억&quot;") == '원태인 FA "200억"'
          and _clean_title("[단독] 원태인 FA 임박") == "원태인 FA 임박")

    # 조사/접미사 차이 — '은퇴' vs '은퇴설에' 가 갈라지면 같은 사건이 쪼개진다.
    # 기사 수가 적을 땐 희귀도 판정이 무력해져서 이 문제가 특히 잘 드러난다.
    small = [
        A("류현진 은퇴 시사 발언", "a.co", 5),
        A("한화 류현진, 은퇴 관련 입 열었다", "b.co", 12),
        A("류현진 은퇴설에 구단 공식 입장", "c.co", 18),
        A("류현진 은퇴 언급 파장 확산", "d.co", 22),
    ]
    got = cluster(small)
    biggest = max(got, key=lambda t: t.article_count)
    check("조사 달라도 같은 사건으로 묶임(소규모)",
          biggest.article_count == 4, f"({biggest.article_count}/4건, key={biggest.key!r})")

    # 야구 필터 — 구단명이 곧 대기업명이라 기업/정치 뉴스가 대량으로 섞여 들어온다.
    # 실측에서 '2분기 heat'(삼성전자), '김정관 장관' 이 상위 화제를 먹었다.
    from src.collect.news_trend import is_baseball

    block = ["삼성전자 2분기 실적 heat", "김정관 장관 여수 상품",
             "LG 트윈스타워 매각", "롯데케미칼 실적 부진", "한화솔루션 주가"]
    passes = ["류현진 은퇴 시사", "한화 이글스 연승", "LG 트윈스의 역전승",
              "KIA 타이거즈 김도영 홈런", "김서현 볼넷 남발"]
    bad = [s for s in block if is_baseball(s)] + [s for s in passes if not is_baseball(s)]
    check("야구 기사 필터 (기업·정치 뉴스 차단)", not bad, f"오판: {bad}")

    # 부분 문자열로 보면 '트윈스타워' 가 '트윈스' 로 통과한다
    check("복합명사 오탐 차단", not is_baseball("LG 트윈스타워 매각"))
    check("조사 붙은 팀명은 통과", is_baseball("LG 트윈스의 역전승"))

    # 대량 노이즈 속에서도 클러스터가 뭉개지지 않아야 한다.
    # 희귀도 컷을 전체의 40% 로 잡았을 때 654건에서 205개 매체짜리 괴물이 나왔다.
    noisy = []
    for i in range(60):
        noisy.append(A(f"삼성전자 2분기 실적 heat 전망 {i}", f"n{i % 40}.co", i))
    for i in range(50):
        noisy.append(A(f"폭염 행사 취소 잇따라 {i}", f"p{i % 45}.co", i))
    for i in range(12):
        noisy.append(A(f"류현진 은퇴 시사 발언 파장 {i}", f"b{i}.co", i * 3))
    for i in range(9):
        noisy.append(A(f"원태인 FA 200억 전망 {i}", f"c{i}.co", i * 4))

    kept = [a for a in noisy if is_baseball(a.title)]
    check("기업 뉴스가 걸러짐", len(kept) == 21, f"({len(kept)}건, 기대 21)")

    ct = sorted(cluster(kept), key=lambda t: t.outlet_count, reverse=True)
    check("대량 노이즈에도 과병합 없음",
          ct and ct[0].outlet_count <= 12, f"(최대 {ct[0].outlet_count if ct else 0}개 매체)")
    check("대표어에 숫자가 앞서지 않음",
          all(not t.key.split()[0][0].isdigit() for t in ct if t.key),
          f"({[t.key for t in ct[:3]]})")

    # 조사 제거 — '폭염으로' 와 '폭염에' 는 서로의 앞부분이 아니라 접두 일치로도
    # 안 묶인다. 어간을 맞춰야 같은 사건으로 인식된다.
    from src.collect.news_trend import _stem

    stem_cases = [("폭염으로", "폭염"), ("폭염에", "폭염"), ("주말까지", "주말"),
                  ("타이거즈의", "타이거즈"), ("선발로", "선발"),
                  ("류현진", "류현진"), ("김서현", "김서현"), ("200억", "200억")]
    wrong = [(w, _stem(w)) for w, want in stem_cases if _stem(w) != want]
    check("조사 제거 (사람 이름은 보존)", not wrong, f"오류: {wrong}")

    # 실측에서 폭염 경기 취소 한 건이 표현 차이로 5조각(29/12/8/7/6곳)으로 갈렸다.
    # 62곳짜리 대형 사건이 29곳으로 축소돼 보이면 화제 판단이 어긋난다.
    split = []
    for i in range(29):
        split.append(A(f"폭염으로 경기 취소 KBO {i}", f"a{i}.co", i * 2))
    for i in range(12):
        split.append(A(f"폭염 속 경기 재개 결정 {i}", f"b{i}.co", i * 3))
    for i in range(8):
        split.append(A(f"주말까지 폭염에 경기 차질 {i}", f"c{i}.co", i * 4))
    for i in range(9):
        split.append(A(f"류현진 은퇴 시사 발언 {i}", f"e{i}.co", i * 3))

    st = sorted(cluster(split), key=lambda t: t.article_count, reverse=True)
    check("표현 갈린 같은 사건이 하나로 합쳐짐",
          st and st[0].article_count == 49, f"({st[0].article_count if st else 0}/49건)")
    check("합칠 때 무관한 사건은 안 섞임",
          all(not any("류현진" in a.title for a in t.articles)
              for t in st if t.article_count == 49))

    # 실측에서 무관한 단독 기사 두 개가 흔한 단어 하나("홈런")만 우연히
    # 겹쳐서 합쳐졌다("하지원 시구" + "삼진과 홈런 사이" 칼럼). 기사 1건짜리
    # 주제는 '과반 지배어' == '그 기사의 단어 전부'가 되므로, 안전장치 없이는
    # 흔한 단어 하나로도 합쳐진다 — _merge_same_event() 가 이걸 막아야 한다.
    hr_bg = [
        A(f"{name} 시즌 20호 홈런 작성", f"filler{i}.co", i * 5)
        for i, name in enumerate(["최정", "강백호", "노시환", "박병호", "moon", "구자욱", "김재환", "양의지"])
    ]
    hr_targets = [
        A("[2026 프로야구 삼진과 홈런 사이] 박진만·김원형의 첫 환희냐", "x.co", 60),
        A("한국배우 최초 하지원, 홈런 역주행 KBO 시구 완벽 성공", "y.co", 65),
    ]
    hr_topics = cluster(hr_bg + hr_targets)
    mixed = any(
        any("박진만" in a.title for a in t.articles) and any("하지원" in a.title for a in t.articles)
        for t in hr_topics
    )
    check("무관한 단독 기사가 흔한 단어 하나로 안 묶임", not mixed)

    # 인터뷰·드라마성 단독 기사 감지 — 매체 수로는 절대 안 잡히는 부류다.
    # 실제로 네이버 스포츠 '인기순'에 떠 있던 제목 그대로 넣은 회귀 테스트
    # (2026-08-07 실측). 매체 수 신호와 무관하게 이 기사들이 실제로 많이
    # 읽힌다는 걸 사용자가 스크린샷으로 직접 확인해줬다.
    from src.collect.news_trend import human_interest

    real_hits = [
        "'21세기 최악의 돔이다' 허구연 총재 혹평의 고척돔, '살인 폭염' 시대 최고의 야구장이다",
        "'파격 결단' KIA 또 일본 보낸다고? 왜?...\"1명만 성과 보여줘도 큰 보람\" 어떻게 확인 얻었나",
        "눈물 흘리고 2군에 갔던 그 선수 맞나...한화 150km 인간승리 드라마 현실로, 프로 8년차에 찾아온 기적 같은 순간",
        "FA? 다년계약? 구자욱 \"단장님, 우승부터 하겠습니다\"...구단주 6일 선수단 소집→무슨 이야기 했나",
    ]
    misses = [
        s for s in real_hits if human_interest(s) < 2
    ]
    check("인터뷰·드라마성 실제 인기 기사 감지(관심점수 2점 이상)", not misses,
          f"놓침: {misses}")

    # 평범한 경기결과/일정 제목은 승격되면 안 된다 — 안 그러면 매일 수십 건이
    # '단독성 화제'로 오탐되어 알림이 스팸이 된다.
    boring = ["오늘 프로야구 경기 일정 안내", "KBO 리그 9일 경기 결과", "두산 vs KT 스코어 5-3"]
    false_pos = [s for s in boring if human_interest(s) >= 2]
    check("평범한 결과·일정 제목은 관심점수 오탐 없음", not false_pos, f"오탐: {false_pos}")


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
        "newscard": Payload(
            card_id="news_test", title="원태인 FA",
            news=NewsCard(
                hook="ERA 4.14인데..",
                category="크보순삭",
                line1="원태인, FA 나오면 <em>200억</em>인데..",
                line2="MLB도 갈 수 있다는 믈브 스카우터",
                photo=str(ROOT / "assets" / "sample.jpg"),
                photo_credit="연합뉴스",
            ),
        ),
    }
    for want, payload in samples.items():
        html, lay, why = render_html(payload, {"layout": want}, cfg)
        ok = lay == want and len(html) > 1500
        check(f"{want} 템플릿 렌더", ok, f"→ {lay}, {len(html)}bytes")

        # 브랜드 프레임 일관성 검사.
        # 주의: CSS 정의(.frame-head{...})는 base 가 항상 내보내므로 문자열 존재만
        # 보면 항상 통과한다 — 실제 '엘리먼트'가 렌더됐는지를 body 에서 확인해야 한다.
        body = html.split("<body>", 1)[1]
        check(f"  {want}: 브랜드 배지 존재", 'class="badge"' in body)
        if want == "newscard":
            # 뉴스카드는 헤드라인이 하단에 오는 포맷이라 상단 타이틀/하단 푸터
            # 프레임을 의도적으로 비운다. 배지만 공유해서 계정 일관성을 지킨다.
            check("  newscard: 상단 프레임 비움", "<header" not in body)
            check("  newscard: 하단 프레임 비움", "<footer" not in body)
            check("  newscard: 헤드라인 강조 태그 보존", "<em>" in body)
        else:
            check(f"  {want}: <header> 렌더", "<header" in body)
            check(f"  {want}: <footer> 렌더", "<footer" in body)
        check(f"  {want}: <main> 렌더", "<main" in body)

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
    stage_news_trend()
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
