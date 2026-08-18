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

from ff_helper.engine.lineup import assign_lineup
from ff_helper.engine.lineup import depth_multiplier as depth_multiplier
from ff_helper.engine.replacement import ReplacementLevels
from ff_helper.rankings.blend import PlayerValuation
from ff_helper.yahoo.models import LeagueSettings

# Below this, a survival probability is treated as zero to keep the conditioning stable.
_EPSILON = 1e-6

# Scale factor converting a standard deviation into a logistic scale parameter, so that a
# caller-supplied stdev keeps its usual meaning: s = sigma * sqrt(3) / pi.
_LOGISTIC_SCALE = math.sqrt(3.0) / math.pi

# Beyond this many scale units the logistic saturates; short-circuit to avoid exp overflow.
_EXP_LIMIT = 40.0

# How much of a player's drafting hazard remains when *no* intervening team needs his
# position as a starter. Never zero: teams draft bench and best-player-available too.
_DEMAND_FLOOR = 0.35

# The plan looks at most this many of my future picks ahead.
_MAX_PLAN_PICKS = 8

# A candidate is removed from his own position's fallback pool only when he is one of its
# top few by VOR -- below that his effect on the expected-best is noise, and skipping the
# recomputation keeps the plan cache small.
_EXCLUSION_DEPTH = 3


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
    demand: float = 1.0,
) -> float:
    """P(player is still on the board at ``target_pick``), given he is available now.

    The conditioning is what makes this usable mid-draft: without it, anyone who has
    already outlasted his ADP looks certain to be gone, which is absurd given that he is
    visibly still available.

    ``demand`` is the share of intervening picks belonging to teams that still need this
    player's position as a starter (1.0 when unknown). ADP is measured across rooms where
    everyone needs everything; in *this* room, if the six teams picking before your next
    turn all have their quarterback, a quarterback's hazard shrinks. It never vanishes --
    teams draft bench players and best-player-available too -- so the reduction is floored
    at ``_DEMAND_FLOOR`` of the ADP hazard.
    """
    if target_pick <= current_pick:
        return 1.0

    sigma = valuation.adp_stdev
    survives_to_now = survival_probability_at(current_pick, valuation.adp, sigma)
    survives_to_target = survival_probability_at(target_pick, valuation.adp, sigma)

    if survives_to_now <= _EPSILON:
        # Extreme faller: the model gives essentially zero mass to him lasting this long,
        # yet here he is. Use the unconditional tail rather than dividing by ~0.
        survival = min(1.0, max(survives_to_target, 0.0))
    else:
        survival = max(0.0, min(1.0, survives_to_target / survives_to_now))

    if demand >= 1.0:
        return survival
    adjust = _DEMAND_FLOOR + (1.0 - _DEMAND_FLOOR) * max(0.0, demand)
    return 1.0 - (1.0 - survival) * adjust


def expected_best_available(
    candidates: list[PlayerValuation],
    levels: ReplacementLevels,
    *,
    current_pick: int,
    target_pick: int,
    demand: float = 1.0,
    rank: int = 1,
) -> float:
    """Expected VOR of the ``rank``-th best player at a position surviving to ``target_pick``.

    Candidates must be sorted best-first. Walking down the list, the probability that a
    given candidate is the rank-th best one left is (he survives) x (exactly rank-1 better
    ones also did). Rank 1 is the classic "best available"; rank 2 prices a *second*
    starting slot at the same position, which cannot lean on the same fallback twice.
    """
    # survived[j] = P(exactly j better candidates survived so far); j capped below rank,
    # because once rank or more better players survive, nobody later can be rank-th best.
    survived = [1.0] + [0.0] * (rank - 1)
    expected = 0.0

    for candidate in candidates:
        probability = survival_probability(
            candidate, current_pick=current_pick, target_pick=target_pick, demand=demand
        )
        expected += survived[rank - 1] * probability * max(0.0, levels.vor(candidate))

        updated = [0.0] * rank
        for j in range(rank):
            updated[j] += survived[j] * (1.0 - probability)
            if j + 1 < rank:
                updated[j + 1] += survived[j] * probability
        survived = updated
        if sum(survived) <= _EPSILON:
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
    future_picks: list[int] | None = None,
    position_demand: dict[int, dict[str, float]] | None = None,
    limit: int = 8,
) -> list[Recommendation]:
    """Rank the available players by the best draft you can still have.

    Each candidate is scored as *his value now, plus the best assignment of your remaining
    starter needs to your remaining picks*. That is the question a one-pick VONA cannot
    answer: "RB now and WR at 40, or the reverse?" is a plan comparison, and the cost of a
    position's cliff shows up as the tiny expected value it contributes to every plan that
    defers it.

    ``roster_counts`` maps position -> how many you already hold. ``future_picks`` is the
    list of your remaining turns after this one (the assistant passes the real list; when
    absent it is extrapolated from ``next_pick``). ``position_demand`` maps each future
    pick to per-position shares of intervening picks made by teams still needing that
    position -- see ``survival_probability``.
    """
    by_position: dict[str, list[PlayerValuation]] = {}
    for valuation in available:
        by_position.setdefault(valuation.position, []).append(valuation)
    for pool in by_position.values():
        pool.sort(key=lambda v: -levels.vor(v))

    demand_by_pick = position_demand or {}

    def demand_at(pick: int, position: str) -> float:
        return demand_by_pick.get(pick, {}).get(position, 1.0)

    # My future turns. With only the next pick known, repeat its gap: the snake alternates
    # short and long gaps around exactly that average. No next pick -> no plan, raw value.
    if future_picks is not None:
        futures = sorted(pick for pick in future_picks if pick > current_pick)[:_MAX_PLAN_PICKS]
    elif next_pick is not None:
        gap = max(1, next_pick - current_pick)
        futures = [next_pick + index * gap for index in range(_MAX_PLAN_PICKS)]
    else:
        futures = []

    open_dedicated, open_flex, backups = assign_lineup(roster_counts, settings)

    # One entry per open starting slot: (eligible positions, rank when filled by each).
    # Rank counts same-position slots -- a second RB slot is priced at the expected
    # *second*-best survivor, because two slots cannot lean on the same fallback player.
    needs: list[tuple[frozenset[str], dict[str, int]]] = []
    dedicated_ranks: dict[str, int] = {}
    for position in sorted(open_dedicated):
        for _ in range(open_dedicated[position]):
            dedicated_ranks[position] = dedicated_ranks.get(position, 0) + 1
            needs.append((frozenset({position}), {position: dedicated_ranks[position]}))
    flex_seen = 0
    for eligible, count in open_flex:
        for _ in range(count):
            flex_seen += 1
            ranks = {p: dedicated_ranks.get(p, 0) + flex_seen for p in eligible}
            needs.append((eligible, ranks))
    needs = needs[:_MAX_PLAN_PICKS]

    e_cache: dict[tuple, float] = {}

    def expected_at(position: str, rank: int, pick: int, exclude: str | None) -> float:
        key = (position, rank, pick, exclude)
        if key not in e_cache:
            pool = by_position.get(position, [])
            if exclude is not None:
                pool = [c for c in pool if c.player_key != exclude]
            e_cache[key] = expected_best_available(
                pool,
                levels,
                current_pick=current_pick,
                target_pick=pick,
                demand=demand_at(pick, position),
                rank=rank,
            )
        return e_cache[key]

    plan_cache: dict[tuple, float] = {}

    def plan_value(
        released: int | None, exclude: str | None, exclude_position: str | None
    ) -> float:
        """Best assignment of the remaining needs to my future picks (exact, bitmask DP)."""
        key = (released, exclude)
        if key in plan_cache:
            return plan_cache[key]
        remaining = [need for index, need in enumerate(needs) if index != released]
        picks = futures[: len(remaining)]
        best = {0: 0.0}
        for pick in picks:
            reachable = dict(best)
            for mask, total in best.items():
                for index, (eligible, ranks) in enumerate(remaining):
                    if mask >> index & 1:
                        continue
                    value = max(
                        (
                            expected_at(
                                p, ranks[p], pick, exclude if p == exclude_position else None
                            )
                            for p in eligible
                            if p in by_position
                        ),
                        default=0.0,
                    )
                    if value <= 0.0:
                        continue
                    filled = mask | 1 << index
                    if total + value > reachable.get(filled, 0.0):
                        reachable[filled] = total + value
            best = reachable
        result = max(best.values()) if best else 0.0
        plan_cache[key] = result
        return result

    # If this is your last pick there is no "next available" -- fall back to raw value.
    horizon = next_pick if next_pick is not None else current_pick

    recommendations: list[Recommendation] = []
    for valuation in available:
        position = valuation.position
        vor = levels.vor(valuation)
        pool = by_position[position]
        held = roster_counts.get(position, 0)

        if open_dedicated.get(position, 0) > 0 or any(
            position in eligible and count > 0 for eligible, count in open_flex
        ):
            factor = 1.0
        else:
            factor = depth_multiplier(backups.get(position, 0) + 1, 1)

        # The slot he would fill releases its need from the plan: the highest-rank one at
        # his position, so the remaining slots keep ranks 1..m-1 against the thinner pool.
        dedicated_needs = [
            index for index, (eligible, _) in enumerate(needs) if eligible == frozenset({position})
        ]
        if dedicated_needs:
            released = max(dedicated_needs, key=lambda index: needs[index][1][position])
        else:
            flex_needs = [
                index for index, (eligible, _) in enumerate(needs) if position in eligible
            ]
            released = (
                max(flex_needs, key=lambda index: needs[index][1][position])
                if flex_needs
                else None
            )

        # Taking him means he is no longer his own position's fallback. Only worth
        # modelling for the top of the pool; below that the effect is noise.
        is_top = any(c.player_key == valuation.player_key for c in pool[:_EXCLUSION_DEPTH])
        exclude = valuation.player_key if is_top else None

        vona = vor - expected_at(position, 1, horizon, None)
        survival = survival_probability(
            valuation,
            current_pick=current_pick,
            target_pick=horizon,
            demand=demand_at(horizon, position),
        )

        own = penalized(vor, factor)
        if valuation.is_injured:
            own = penalized(own, 0.5)
        score = own + plan_value(released, exclude, position if exclude else None)

        recommendations.append(
            Recommendation(
                valuation=valuation,
                vor=vor,
                vona=vona,
                score=score,
                survival_to_next=survival,
                depth_factor=factor,
                reason=_explain(
                    valuation,
                    vor,
                    vona,
                    survival,
                    factor,
                    held,
                    horizon,
                    demand_at(horizon, position),
                    _wait_note(position, needs, futures, by_position, expected_at),
                ),
            )
        )

    recommendations.sort(key=lambda r: -r.score)
    return recommendations[:limit]


def _wait_note(
    position: str,
    needs: list[tuple[frozenset[str], dict[str, int]]],
    futures: list[int],
    by_position: dict[str, list[PlayerValuation]],
    expected_at,
) -> str:
    """What taking this player leaves on the table at my next turn, when it is plenty."""
    if not futures:
        return ""
    best_value, best_position = 0.0, None
    for eligible, ranks in needs:
        for p in eligible:
            if p == position or p not in by_position:
                continue
            value = expected_at(p, ranks[p], futures[0], None)
            if value > best_value:
                best_value, best_position = value, p
    if best_position is not None and best_value >= 15:
        return f"{best_position} still deep at your pick {futures[0]}"
    return ""


def _explain(
    valuation: PlayerValuation,
    vor: float,
    vona: float,
    survival: float,
    factor: float,
    held: int,
    horizon: int,
    demand: float,
    wait_note: str,
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

    if demand <= 0.5:
        parts.append(f"few teams before pick {horizon} need a {valuation.position}")

    if wait_note:
        parts.append(wait_note)

    if valuation.tier is not None:
        parts.append(f"tier {valuation.tier}")

    if factor < 1.0:
        parts.append(f"no open slot for him; you hold {held} at {valuation.position}")

    if valuation.is_injured:
        parts.append(f"injury status {valuation.status}")
    if valuation.points_estimated:
        parts.append("projection interpolated")

    return "; ".join(parts) if parts else f"VOR {vor:.0f}"
