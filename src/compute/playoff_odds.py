"""가을야구(포스트시즌 진출) 확률 — 잔여 경기 몬테카를로 시뮬레이션.

KBO 정규시즌은 144경기, 상위 5개 팀이 포스트시즌에 진출한다(4·5위는 와일드카드전).
각 팀의 잔여 경기를 '현재 승률과 동일한 승리 확률을 가진 독립 베르누이 시행'으로
가정해 시뮬레이션한다. 실제로는 잔여 상대 전력·홈어웨이 등에 따라 다르지만,
이 카드는 '자체 산출' 기반의 재미용 지표로 설계됐으므로 이 단순화를 받아들인다
(카드 각주에 명시).

이 모듈은 새 크롤링 없이 이미 수집한 standings() 결과만 입력으로 받는다 —
추가 스크래핑 표면을 늘리지 않는다.
"""

from __future__ import annotations

from typing import Any

import numpy as np

SEASON_GAMES = 144   # KBO 정규시즌 총 경기 수
PLAYOFF_SLOTS = 5    # 포스트시즌 진출 팀 수 (와일드카드 포함)
DEFAULT_SIMS = 100_000


class SimulationError(RuntimeError):
    pass


def simulate(
    standings_rows: list[dict[str, Any]],
    *,
    sims: int = DEFAULT_SIMS,
    season_games: int = SEASON_GAMES,
    playoff_slots: int = PLAYOFF_SLOTS,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """standings() 의 rows(순위/팀명/경기/승/무/패/승률 포함)를 받아
    팀별 가을야구(포스트시즌) 진출 확률을 계산한다.

    반환: [{"팀명":..., "_team":..., "가을야구확률": "97.3%"}] — 확률 내림차순.
    """
    teams: list[str] = []
    wins: list[int] = []
    remaining: list[int] = []
    win_rate: list[float] = []

    for r in standings_rows:
        try:
            g = int(r["경기"])
            w = int(r["승"])
        except (KeyError, ValueError, TypeError):
            continue
        team = r.get("_team") or r.get("팀명")
        if not team:
            continue
        try:
            wr = float(r["승률"])
        except (KeyError, ValueError, TypeError):
            wr = (w / g) if g else 0.0

        teams.append(team)
        wins.append(w)
        remaining.append(max(season_games - g, 0))
        win_rate.append(wr)

    n = len(teams)
    if n != 10:
        raise SimulationError(f"팀 수가 10개가 아닙니다: {n}개 — standings 데이터 확인 필요")

    rng = np.random.default_rng(seed)
    final_wins = np.empty((sims, n), dtype=np.int32)
    for i in range(n):
        rem, wr = remaining[i], win_rate[i]
        extra = (
            rng.binomial(rem, wr, size=sims).astype(np.int32)
            if rem > 0
            else np.zeros(sims, dtype=np.int32)
        )
        final_wins[:, i] = wins[i] + extra

    # 시뮬레이션마다 승수 내림차순 정렬 → 상위 playoff_slots 안에 든 팀 집계.
    # 동률 처리는 인덱스 순서로 근사(자체 산출 지표이므로 완전한 타이브레이커 룰까지는 반영 안 함).
    rank_idx = np.argsort(-final_wins, axis=1)
    top = rank_idx[:, :playoff_slots]
    made_it = np.array([(top == i).sum() for i in range(n)])
    prob = made_it / sims

    out = [
        {
            "팀명": team,
            "_team": team,
            "가을야구확률": f"{p * 100:.1f}%",
            "_sort": p,
        }
        for team, p in zip(teams, prob)
    ]
    out.sort(key=lambda r: -r["_sort"])
    for r in out:
        del r["_sort"]
    return out
