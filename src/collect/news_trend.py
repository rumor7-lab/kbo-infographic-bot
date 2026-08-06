"""KBO 화제 감지 — 지금 뭐가 터졌는지 찾는다.

핵심 아이디어
  네이버 검색 API 는 조회수를 주지 않는다. 대신 '같은 사건을 몇 개 매체가
  썼는지'를 센다. 2시간 안에 23개 매체가 같은 얘기를 쓰면 그건 터진 것이다.

  이 지표가 조회수보다 오히려 빠르다. 조회수는 이미 퍼진 뒤에 올라가지만
  매체 수는 퍼지는 중에 올라간다. 속보 계정에는 이쪽이 맞는 신호다.

저작권 주의
  여기서 가져오는 제목/요약은 '무엇이 화제인지' 판단용으로만 쓴다. 카드에
  그대로 옮겨 쓰면 저작권 침해다(사실은 보호 대상이 아니지만 표현은 보호된다).
  카드 문구는 사실만 참고해서 직접 새로 쓴다.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
API = "https://openapi.naver.com/v1/search/news.json"

# KBO 10개 구단. 팀명이 제목에 있으면 야구 뉴스일 확률이 높다.
TEAMS = {
    "KIA": ["KIA", "기아", "타이거즈"],
    "삼성": ["삼성", "라이온즈"],
    "LG": ["LG", "트윈스"],
    "두산": ["두산", "베어스"],
    "KT": ["KT", "위즈"],
    "SSG": ["SSG", "랜더스"],
    "롯데": ["롯데", "자이언츠"],
    "한화": ["한화", "이글스"],
    "NC": ["NC", "다이노스"],
    "키움": ["키움", "히어로즈"],
}

# 검색어 — 너무 좁으면 놓치고 너무 넓으면 축구/농구가 섞인다.
QUERIES = ["KBO", "프로야구"] + list(TEAMS.keys())

# 제목에서 걷어낼 것들
_TAG_RE = re.compile(r"<[^>]+>")            # API 가 검색어에 <b> 태그를 씌워 보낸다
_BRACKET_RE = re.compile(r"^\s*[\[\(][^\]\)]{1,12}[\]\)]\s*")  # [단독], (영상) 머리표
_NOISE = re.compile(r"[^\w가-힣]+")

# 화제성 판단에서 빼야 하는 흔한 단어 (이것만 겹치는 건 같은 사건이 아니다)
STOPWORDS = {
    "프로야구", "KBO", "리그", "경기", "선수", "감독", "구단", "야구",
    "오늘", "내일", "어제", "시즌", "기록", "이날", "관련", "대한", "위해",
}

# 등장 빈도가 낮아도 '이 단어 하나로 같은 사건이라 단정하면 안 되는' 단어들.
# 팀명이 대표적이다 — 같은 팀의 전혀 다른 사건이 팀명으로 이어져 버린다.
WEAK_KEYWORDS = {alias for aliases in TEAMS.values() for alias in aliases} | {
    "프로", "구단", "팬들", "인터뷰", "발표", "공식", "입장", "소식",
}


@dataclass
class Article:
    title: str
    link: str
    published: datetime
    outlet: str = ""


@dataclass
class Topic:
    """같은 사건을 다룬 기사 묶음."""

    key: str
    keywords: list[str]
    articles: list[Article] = field(default_factory=list)

    @property
    def outlet_count(self) -> int:
        """서로 다른 매체 수 = 화제 강도. 같은 매체가 여러 번 쓴 건 1로 센다."""
        return len({a.outlet for a in self.articles if a.outlet})

    @property
    def article_count(self) -> int:
        return len(self.articles)

    @property
    def newest(self) -> datetime:
        return max(a.published for a in self.articles)

    @property
    def oldest(self) -> datetime:
        return min(a.published for a in self.articles)

    @property
    def velocity(self) -> float:
        """시간당 기사 수. 같은 매체 수라도 짧은 시간에 몰렸으면 더 뜨겁다."""
        span = (self.newest - self.oldest).total_seconds() / 3600
        return self.article_count / max(span, 0.5)

    @property
    def team(self) -> str | None:
        """구단 추정 — 카드 팀 컬러에 쓴다.

        제목 하나만 보면 팀명이 안 적힌 기사가 많아 자주 놓친다("류현진 은퇴 시사
        발언"). 묶인 기사 전체에서 가장 많이 언급된 팀을 고른다.
        """
        counts: dict[str, int] = defaultdict(int)
        for a in self.articles:
            t = team_of(a.title)
            if t:
                counts[t] += 1
        return max(counts, key=lambda k: counts[k]) if counts else None

    def headline_candidates(self, n: int = 3) -> list[str]:
        """제목 후보 — 그대로 쓰지 말고 새로 쓸 때 참고만 한다."""
        seen, out = set(), []
        for a in sorted(self.articles, key=lambda x: x.published, reverse=True):
            t = a.title.strip()
            if t.lower() in seen:
                continue
            seen.add(t.lower())
            out.append(t)
            if len(out) >= n:
                break
        return out


class NewsError(RuntimeError):
    pass


def _credentials() -> tuple[str, str]:
    cid = os.getenv("NAVER_CLIENT_ID")
    secret = os.getenv("NAVER_CLIENT_SECRET")
    if not cid or not secret:
        raise NewsError(
            "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 이 없습니다. "
            "https://developers.naver.com/apps 에서 애플리케이션 등록 후 발급받으세요."
        )
    return cid, secret


def _clean_title(raw: str) -> str:
    t = _TAG_RE.sub("", raw)
    t = (t.replace("&quot;", '"').replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&apos;", "'"))
    return _BRACKET_RE.sub("", t).strip()


def _outlet(link: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", link or "")
    return m.group(1) if m else ""


def fetch(query: str, *, display: int = 100) -> list[Article]:
    """네이버 뉴스 검색 — 최신순."""
    cid, secret = _credentials()
    r = requests.get(
        API,
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
        params={"query": query, "display": display, "sort": "date"},
        timeout=15,
    )
    if r.status_code != 200:
        raise NewsError(f"네이버 뉴스 검색 실패 {r.status_code}: {r.text[:200]}")

    out = []
    for item in r.json().get("items", []):
        try:
            pub = parsedate_to_datetime(item["pubDate"]).astimezone(KST)
        except Exception:  # noqa: BLE001
            continue
        # originallink 가 실제 언론사 도메인이라 매체 구분에 더 정확하다.
        link = item.get("originallink") or item.get("link", "")
        out.append(Article(
            title=_clean_title(item.get("title", "")),
            link=link,
            published=pub,
            outlet=_outlet(link),
        ))
    return out


def _keywords(title: str) -> set[str]:
    """제목에서 사건을 식별할 만한 단어만 남긴다."""
    words = [w for w in _NOISE.split(title) if len(w) >= 2]
    return {w for w in words if w not in STOPWORDS}


def _overlap(ka: set[str], kb: set[str]) -> set[str]:
    """두 키워드 집합의 교집합 — 한국어 조사/접미사 차이를 흡수한다.

    완전 일치만 보면 '은퇴' 와 '은퇴설에' 가 남남이 되어 같은 사건이 갈라진다
    (실제로 류현진 은퇴 기사 4건 중 1건이 이것 때문에 떨어져 나갔다).
    형태소 분석기를 붙이면 정확하겠지만 의존성이 무거워서, 한쪽이 다른 쪽의
    앞부분이면 같은 말로 본다 — 한국어는 어간이 앞에 오고 조사가 뒤에 붙으므로
    이 규칙만으로도 대부분 잡힌다.

    짧은 단어(2자 미만)는 우연한 앞글자 일치가 많아 완전 일치만 인정한다.
    반환값은 '더 짧은 쪽'(어간에 가까운 형태)으로 통일한다.
    """
    out: set[str] = set()
    for a in ka:
        for b in kb:
            if a == b:
                out.add(a)
            elif len(a) >= 2 and len(b) >= 2:
                if a.startswith(b):
                    out.add(b)
                elif b.startswith(a):
                    out.add(a)
    return out


def cluster(articles: list[Article]) -> list[Topic]:
    """제목 키워드가 겹치는 기사끼리 묶는다.

    단순히 '겹친 단어 수'로 판단하면 안 된다. 매체마다 제목 표현이 달라서
    "김서현 제구 난조" 와 "한화 김서현, 35구 중 볼 25개" 는 '김서현' 하나만
    겹치는데, 이건 명백히 같은 사건이다. 반대로 흔한 단어 2개가 겹치는 건
    우연일 수 있다.

    그래서 '희귀한 단어가 겹쳤는가'를 본다. 선수 이름처럼 전체 기사 중 소수에만
    나오는 단어가 겹치면 같은 사건으로 보고, 흔한 단어는 2개 이상 겹쳐야 인정한다.
    """
    kw_cache = {id(a): _keywords(a.title) for a in articles}

    # 문서 빈도 — 몇 개 기사에 나오는 단어인가
    df: dict[str, int] = defaultdict(int)
    for ks in kw_cache.values():
        for w in ks:
            df[w] += 1

    n = max(len(articles), 1)
    # 전체의 40% 이하에만 등장하면 '희귀' = 사건 식별력이 있다고 본다.
    # 30% 로 잡았더니 크게 터진 주제는 자기 주제어가 흔해져서 오히려 안 묶이는
    # 자기모순이 생겼다(원태인이 12건 중 4건에 나오는데 컷이 3이라 탈락).
    rare_cut = max(2, int(n * 0.4))

    def is_rare(w: str) -> bool:
        # 팀명은 df 가 낮아도 단독 연결어가 되면 안 된다. 같은 팀의 서로 다른
        # 사건이 팀명 하나로 이어져 버린다(류현진 은퇴 + 김서현 부진이 '한화'로
        # 한 덩어리가 되는 문제가 실제로 났다).
        return w not in WEAK_KEYWORDS and df[w] <= rare_cut

    def same_topic(ka: set[str], kb: set[str]) -> bool:
        overlap = _overlap(ka, kb)
        if not overlap:
            return False
        if any(is_rare(w) for w in overlap):
            return True
        # 희귀어가 없으면 흔한 단어가 2개 이상 겹쳐야 인정 (팀명 제외)
        return len([w for w in overlap if w not in WEAK_KEYWORDS]) >= 2

    topics: list[Topic] = []
    for a in sorted(articles, key=lambda x: x.published, reverse=True):
        ka = kw_cache[id(a)]
        if not ka:
            continue
        placed = False
        for t in topics:
            if same_topic(ka, set(t.keywords)):
                t.articles.append(a)
                # 교집합으로 좁히면 주제어가 사라져 이후 기사를 놓친다(원태인 건에서
                # 'FA' 만 남아 'MLB 스카우터' 기사를 못 붙였음). 합집합으로 넓힌다.
                t.keywords = sorted(set(t.keywords) | ka)
                placed = True
                break
        if not placed:
            topics.append(Topic(key="", keywords=sorted(ka), articles=[a]))

    # 대표어 뽑기 — '희귀한 순'으로 고르면 안 된다. 제일 희귀한 단어는 보통
    # 한 매체만 쓴 특이 표현이라 정작 주인공을 놓친다("류현진 은퇴" 대신 "공식 발언").
    # 사건의 주인공은 '클러스터 안에서 반복되는 단어'다. 클러스터 내 등장 횟수를
    # 1순위로, 전체 희귀도를 2순위로 본다.
    for t in topics:
        in_cluster: dict[str, int] = defaultdict(int)
        for a in t.articles:
            for w in kw_cache[id(a)]:
                in_cluster[w] += 1
        ranked = sorted(
            t.keywords,
            key=lambda w: (-in_cluster[w], w in WEAK_KEYWORDS, df[w], w),
        )
        t.keywords = ranked
        t.key = " ".join(ranked[:2])
    return topics


def hot_topics(
    *, hours: int = 6, min_outlets: int = 3, top: int = 5,
    queries: list[str] | None = None,
) -> list[Topic]:
    """최근 N시간 화제 주제를 매체 수 기준으로 정렬해 반환."""
    since = datetime.now(KST) - timedelta(hours=hours)

    # 같은 기사가 여러 검색어에 걸리므로 링크로 중복 제거
    seen: dict[str, Article] = {}
    for q in (queries or QUERIES):
        try:
            for a in fetch(q):
                if a.published >= since and a.link and a.link not in seen:
                    seen[a.link] = a
        except NewsError:
            raise
        except Exception:  # noqa: BLE001
            continue  # 검색어 하나 실패로 전체를 죽이지 않는다

    topics = cluster(list(seen.values()))
    topics = [t for t in topics if t.outlet_count >= min_outlets]
    # 매체 수 우선, 같으면 속도(시간당 기사 수)로 가른다
    topics.sort(key=lambda t: (t.outlet_count, t.velocity), reverse=True)
    return topics[:top]


def team_of(text: str) -> str | None:
    """제목에서 구단을 추정 — 카드 팀 컬러에 쓴다."""
    for team, aliases in TEAMS.items():
        if any(a in text for a in aliases):
            return team
    return None
