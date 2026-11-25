"""소재 승패 판정. 베이지안(Beta 사후분포) 몬테카를로로 '이 소재가 최고일 확률'을 계산.

단순히 CTR 높은 걸 고르면 노출 200회짜리 우연이 이긴다. 이 방식은 데이터가
적을수록 판정을 보류하고, 충분히 쌓였을 때만 패자를 확정한다.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class ArmResult:
    entity_id: str
    imp: int
    successes: float      # 목표 지표의 '성공' 수 (클릭 또는 전환)
    rate: float
    prob_best: float
    enough_data: bool


def prob_best(arms: dict[str, tuple[int, float]], draws: int = 4000, seed: int = 7,
              min_trials: int = 1000) -> list[ArmResult]:
    """arms: {id: (trials, successes)} -> 각 arm이 최고일 확률.

    Beta(1+s, 1+n-s) 균등 사전분포. 결정적 결과를 위해 seed 고정.
    """
    if not arms:
        return []
    rng = random.Random(seed)
    ids = list(arms)
    wins = dict.fromkeys(ids, 0)
    params = {i: (1 + s, 1 + max(n - s, 0)) for i, (n, s) in arms.items()}
    for _ in range(draws):
        best_id, best_v = None, -1.0
        for i in ids:
            a, b = params[i]
            v = rng.betavariate(a, b)
            if v > best_v:
                best_id, best_v = i, v
        wins[best_id] += 1  # type: ignore[index]
    out = []
    for i in ids:
        n, s = arms[i]
        out.append(ArmResult(i, n, s, (s / n) if n else 0.0, wins[i] / draws, n >= min_trials))
    return sorted(out, key=lambda r: -r.prob_best)


def choose_objective(total_clicks: int, total_conv: float, min_conv: int = 20) -> str:
    """전환이 충분하면 '노출당 전환'(클릭률×전환율을 함께 반영), 아니면 CTR."""
    return "conv_per_imp" if total_conv >= min_conv else "ctr"
