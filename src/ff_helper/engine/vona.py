"""Value Over Next Available -- the scarcity model.

The question a draft board cannot answer is not "who is best?" but "who will still be here
when I pick again?". In a 12-team snake from slot 5, picks 5 and 20 are yours; between them
fourteen players come off the board. If the four best remaining tight ends will all survive
to pick 20 but the top running back will not, then taking the running back costs you
nothing at tight end and gains you everything at running back -- even if the tight end
grades out higher in isolation.

That is what VONA measures::

    VONA(player) = VOR(player) - E[VOR of the best player at his position at my next pick]

Two details matter more than the formula:

**Survival is conditional.** A player whose ADP says he should have gone at pick 10 but who
is sitting there at pick 30 is a faller. The unconditional probability that he lasts to
pick 45 is near zero, which is obviously wrong -- he is available *now*. So we condition on
that: P(available at N | available at now).

**Expected best-available is not the same as most-likely-best-available.** We sum over each
candidate's probability of being the best one left, which correctly accounts for a position
where five similar players make it very likely *someone* survives.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ff_helper.engine.replacement import ReplacementLevels
from ff_helper.rankings.blend import PlayerValuation
from ff_helper.yahoo.models import LeagueSettings

# Below this, a survival probability is treated as zero to keep the conditioning stable.
_EPSILON = 1e-6

# How much a marginal player at a position is worth once the starting slots are filled.
# Index 0 is the first backup -- still genuinely useful for bye weeks and injuries -- and
# it decays fast from there. A fourth running back in a 2-RB league is roster filler.
_DEPTH_DISCOUNT = (0.55, 0.30, 0.15, 0.08, 0.04)

# Scale factor converting a standard deviation into a logistic scale parameter, so that a
# caller-supplied stdev keeps its usual meaning: s = sigma * sqrt(3) / pi.
_LOGISTIC_SCALE = math.sqrt(3.0) / math.pi

# Beyond this many scale units the logistic saturates; short-circuit to avoid exp overflow.
_EXP_LIMIT = 40.0


def survival_probability_at(pick: int, adp: float, sigma: float) -> float:
    """P(a player with this ADP is undrafted when ``pick`` comes up.

    Draft position is modelled as **logistic**, not normal. Real ADP distributions have
    fat tails: players slide on injury news and get reached for on hype far more often
    than a Gaussian allows. A normal tail makes a player sitting 20 picks past his ADP
    mathematically impossible, which then makes every conditional probability about him
    garbage -- exactly the fallers you most want advice on.
    """
    scale = max(sigma, 0.5) * _LOGISTIC_SCALE
    z = (pick - adp) / scale
    if z > _EXP_LIMIT:
        return 0.0
    if z < -_EXP_LIMIT:
        return 1.0
    return 1.0 / (1.0 + math.exp(z))


def survival_probability(
    valuation: PlayerValuation,
    *,
    current_pick: int,
    target_pick: int,
) -> float:
    """P(player is still on the board at ``target_pick``), given he is available now.

    The conditioning is what makes this usable mid-draft: without it, anyone who has
    already outlasted his ADP looks certain to be gone, which is absurd given that he is
    visibly still available.
    """
    if target_pick <= current_pick:
        return 1.0

    sigma = valuation.adp_stdev
    survives_to_now = survival_probability_at(current_pick, valuation.adp, sigma)
    survives_to_target = survival_probability_at(target_pick, valuation.adp, sigma)

    if survives_to_now <= _EPSILON:
        # Extreme faller: the model gives essentially zero mass to him lasting this long,
        # yet here he is. Return the unconditional tail rather than dividing by ~0.
        return min(1.0, max(survives_to_target, 0.0))

    return max(0.0, min(1.0, survives_to_target / survives_to_now))


def expected_best_available(
    candidates: list[PlayerValuation],
    levels: ReplacementLevels,
    *,
    current_pick: int,
    target_pick: int,
) -> float:
    """Expected VOR of the best player at a position surviving to ``target_pick``.

    Candidates must be sorted best-first. Walking down the list, the probability that a
    given candidate is the best one left is (he survives) x (everyone better did not).
    """
    expected = 0.0
    none_better_survived = 1.0

    for candidate in candidates:
        probability = survival_probability(
            candidate, current_pick=current_pick, target_pick=target_pick
        )
        expected += none_better_survived * probability * max(0.0, levels.vor(candidate))
        none_better_survived *= 1.0 - probability
        if none_better_survived <= _EPSILON:
            break

    # Whatever probability mass is left over is the case where nobody startable survives,
    # which is worth replacement level -- VOR 0 by definition.
    return expected


def penalized(score: float, factor: float) -> float:
    """Apply a 0-1 penalty factor so it always lowers the score.

    A bare multiply flips its meaning on a negative score: halving -20 gives -10, which
    *promotes* the player being penalised. An overpriced player at a filled position must
    rank below the same player at an open one, so a penalty divides when the score is
    already negative -- monotone down in both halves.
    """
    return score * factor if score >= 0 else score / factor


def depth_multiplier(count_at_position: int, starters_needed: int) -> float:
    """How much a marginal player at this position is worth given what you already have.

    A third quarterback in a one-QB league is nearly worthless no matter how he grades.
    """
    if count_at_position < starters_needed:
        return 1.0
    # 0 = the first player past your starting requirement, i.e. the first backup.
    surplus = count_at_position - starters_needed
    return _DEPTH_DISCOUNT[min(surplus, len(_DEPTH_DISCOUNT) - 1)]


@dataclass(frozen=True)
class Recommendation:
    valuation: PlayerValuation
    vor: float
    vona: float
    score: float
    survival_to_next: float
    depth_factor: float
    reason: str

    @property
    def name(self) -> str:
        return self.valuation.name

    @property
    def position(self) -> str:
        return self.valuation.position


def recommend(
    available: list[PlayerValuation],
    levels: ReplacementLevels,
    settings: LeagueSettings,
    roster_counts: dict[str, int],
    *,
    current_pick: int,
    next_pick: int | None,
    limit: int = 8,
) -> list[Recommendation]:
    """Rank the available players by what it costs you to wait.

    ``roster_counts`` maps position -> how many you already hold.
    """
    by_position: dict[str, list[PlayerValuation]] = {}
    for valuation in available:
        by_position.setdefault(valuation.position, []).append(valuation)
    for pool in by_position.values():
        pool.sort(key=lambda v: -levels.vor(v))

    # If this is your last pick there is no "next available" -- fall back to raw value.
    horizon = next_pick if next_pick is not None else current_pick

    baseline: dict[str, float] = {}
    for position, pool in by_position.items():
        baseline[position] = expected_best_available(
            pool, levels, current_pick=current_pick, target_pick=horizon
        )

    recommendations: list[Recommendation] = []
    for valuation in available:
        position = valuation.position
        vor = levels.vor(valuation)
        vona = vor - baseline.get(position, 0.0)

        starters = max(1, settings.starters_at(position))
        held = roster_counts.get(position, 0)
        depth = depth_multiplier(held, starters)

        survival = survival_probability(valuation, current_pick=current_pick, target_pick=horizon)

        # Blend value and scarcity. VONA alone over-rewards a thin position where every
        # option is mediocre, so raw VOR keeps a floor under the ranking.
        score = penalized(0.65 * vona + 0.35 * vor, depth)
        if valuation.is_injured:
            score = penalized(score, 0.5)

        recommendations.append(
            Recommendation(
                valuation=valuation,
                vor=vor,
                vona=vona,
                score=score,
                survival_to_next=survival,
                depth_factor=depth,
                reason=_explain(valuation, vor, vona, survival, depth, held, starters, horizon),
            )
        )

    recommendations.sort(key=lambda r: -r.score)
    return recommendations[:limit]


def _explain(
    valuation: PlayerValuation,
    vor: float,
    vona: float,
    survival: float,
    depth: float,
    held: int,
    starters: int,
    horizon: int,
) -> str:
    """A one-line, human-checkable reason. You are the one making the pick."""
    gone = 1.0 - survival
    parts: list[str] = []

    if vona >= 12:
        parts.append(f"big drop-off at {valuation.position} after him")
    elif vona >= 5:
        parts.append(f"meaningful {valuation.position} gap if you wait")
    elif vona <= 1:
        parts.append(f"comparable {valuation.position}s should survive")

    if gone >= 0.75:
        parts.append(f"{gone:.0%} gone by pick {horizon}")
    elif gone <= 0.25:
        parts.append(f"likely still there at {horizon}")

    if valuation.tier is not None:
        parts.append(f"tier {valuation.tier}")

    if depth < 1.0:
        parts.append(f"you already hold {held} at {valuation.position} (start {starters})")

    if valuation.is_injured:
        parts.append(f"injury status {valuation.status}")
    if valuation.points_estimated:
        parts.append("projection interpolated")

    return "; ".join(parts) if parts else f"VOR {vor:.0f}"
