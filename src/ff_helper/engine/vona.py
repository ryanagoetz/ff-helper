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
from typing import TYPE_CHECKING

from ff_helper.engine.lineup import assign_lineup
from ff_helper.engine.lineup import depth_multiplier as depth_multiplier
from ff_helper.engine.replacement import ReplacementLevels
from ff_helper.engine.room import RoomTendencies
from ff_helper.engine.upside import upside_bonus
from ff_helper.rankings.blend import PlayerValuation
from ff_helper.yahoo.models import LeagueSettings

if TYPE_CHECKING:
    # Type-only: simulate.py imports this module's constants at runtime, so importing it
    # back here for real would be a cycle.
    from ff_helper.engine.simulate import SimulationResult

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

# Bounds on the pick-budget normalizer exponent. Survival probabilities are raised to
# this power so that the *expected number of players removed* between now and a target
# pick equals the number of picks that actually happen in between -- treating survivals
# as independent otherwise lets a deep position lose five players to a five-pick window
# that also has to cover every other position. The clamp keeps a strange pool (three
# players left, forty picks to cover) from producing absurd exponents.
_NORMALIZER_MIN = 0.25
_NORMALIZER_MAX = 4.0

# Bisection steps when solving for the normalizer; 24 halvings of [0.25, 4] pins the
# exponent far below any resolution that could change a ranking.
_NORMALIZER_ITERATIONS = 24

# What remains of an injured player's score after his projection was already cut for
# expected games missed (blend.availability_of). The absence itself is priced there;
# this covers what a status cannot: re-injury risk, rust, the chance "out" grows.
# Shared with the auction engine so both formats price the same risk the same way.
_INJURY_RESIDUAL = 0.85

# VOR-point cost of stacking a second bye on a position too thin to absorb it: one week
# where the slot scores zero, softened by waiver-wire patching.
_BYE_PENALTY = 3.0


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
    adp_shift: float = 0.0,
    normalizer: float = 1.0,
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

    ``adp_shift`` is this room's learned tendency for the player's position, in picks
    (``room.RoomTendencies.shift``): applied to the ADP itself, so both marginals move
    together and the conditioning stays coherent.

    ``normalizer`` is the pick-budget exponent from ``survival_normalizers``: applied
    last, so the whole pool's expected removals match the picks that actually happen.
    Composition order is fixed and deliberate: shifted marginal logistic, condition on
    available-now, demand adjustment, then normalization.
    """
    if target_pick <= current_pick:
        return 1.0

    sigma = valuation.adp_stdev
    adp = valuation.adp + adp_shift
    survives_to_now = survival_probability_at(current_pick, adp, sigma)
    survives_to_target = survival_probability_at(target_pick, adp, sigma)

    if survives_to_now <= _EPSILON:
        # Extreme faller: the model gives essentially zero mass to him lasting this long,
        # yet here he is. Use the unconditional tail rather than dividing by ~0.
        survival = min(1.0, max(survives_to_target, 0.0))
    else:
        survival = max(0.0, min(1.0, survives_to_target / survives_to_now))

    if demand < 1.0:
        adjust = _DEMAND_FLOOR + (1.0 - _DEMAND_FLOOR) * max(0.0, demand)
        survival = 1.0 - (1.0 - survival) * adjust
    if normalizer != 1.0:
        survival = survival**normalizer
    return survival


def _opponent_picks_between(current_pick: int, target_pick: int, my_picks: list[int]) -> int:
    """How many players *opponents* remove before ``target_pick`` comes up.

    Picks ``current_pick .. target_pick - 1`` all resolve first; mine among them are not
    opponent removals -- the plan chooses those players itself (and the candidate under
    consideration is excluded from his own pool separately).
    """
    window = target_pick - current_pick
    mine = sum(1 for pick in my_picks if current_pick <= pick < target_pick)
    return max(0, window - mine)


def _solve_normalizer(survivals: list[float], budget: int) -> float:
    """The exponent beta with sum(1 - s^beta) equal to ``budget`` removed players.

    ``sum(1 - s^beta)`` is monotone increasing in beta, so bisection converges; outside
    the clamp the pool is too small or too lopsided for the budget and the boundary is
    the honest answer.
    """
    if not survivals or budget <= 0:
        return _NORMALIZER_MIN

    def removed(beta: float) -> float:
        return sum(1.0 - s**beta for s in survivals)

    if removed(_NORMALIZER_MIN) >= budget:
        return _NORMALIZER_MIN
    if removed(_NORMALIZER_MAX) <= budget:
        return _NORMALIZER_MAX

    low, high = _NORMALIZER_MIN, _NORMALIZER_MAX
    for _ in range(_NORMALIZER_ITERATIONS):
        mid = (low + high) / 2.0
        if removed(mid) < budget:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def survival_normalizers(
    pool_survivals: dict[int, list[float]],
    *,
    current_pick: int,
    my_picks: list[int],
) -> dict[int, float]:
    """Per target pick: the exponent that makes the pool's removals add up.

    Independent survivals overdraw the board: if eight comparable receivers each carry a
    40% chance of being gone, "independence" quietly removes five of them from a window
    of twelve picks that also has to cover every other position. Normalizing the pool so
    expected removals equal actual picks fixes that -- and it is also what makes the
    demand adjustment two-sided: flooring quarterback hazards pushes the exponent above
    one, which lands the displaced removals on the positions teams actually still chase.

    ``pool_survivals`` maps each target pick to the demand-adjusted conditional survival
    of *every* available player (not one position's pool -- the budget is board-wide).
    """
    return {
        target: _solve_normalizer(
            survivals, _opponent_picks_between(current_pick, target, my_picks)
        )
        for target, survivals in pool_survivals.items()
    }


def expected_best_available(
    candidates: list[PlayerValuation],
    levels: ReplacementLevels,
    *,
    current_pick: int,
    target_pick: int,
    demand: float = 1.0,
    adp_shift: float = 0.0,
    normalizer: float = 1.0,
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
            candidate,
            current_pick=current_pick,
            target_pick=target_pick,
            demand=demand,
            adp_shift=adp_shift,
            normalizer=normalizer,
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


def extrapolated_picks(current_pick: int, next_pick: int, num_teams: int | None) -> list[int]:
    """Guess my future turns when only the next one is known.

    Snake gaps alternate: from one slot the gaps between consecutive turns are g and
    2N - g, summing to two full rounds. Knowing the team count lets the fallback mirror
    them properly; without it, repeating the observed gap is right on average and wrong
    pick to pick.
    """
    gap = max(1, next_pick - current_pick)
    picks = [next_pick]
    if num_teams is not None and 0 < gap < 2 * num_teams:
        mirrored = max(1, 2 * num_teams - gap)
        gaps = (mirrored, gap)  # after next_pick, the *other* side of the turn comes first
        for index in range(_MAX_PLAN_PICKS - 1):
            picks.append(picks[-1] + gaps[index % 2])
    else:
        for _ in range(_MAX_PLAN_PICKS - 1):
            picks.append(picks[-1] + gap)
    return picks


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
    my_picks: list[int] | None = None,
    tendencies: RoomTendencies | None = None,
    num_teams: int | None = None,
    market: SimulationResult | None = None,
    roster_byes: dict[str, list[int]] | None = None,
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
    position -- see ``survival_probability``. ``my_picks`` is every turn of mine, used to
    count how many intervening picks are actually opponents'; when absent, the current
    pick is assumed mine (the way the pure-function tests call this). ``tendencies`` is
    this room's learned drift from ADP (``room.room_tendencies``); absent means neutral.

    ``market`` is an optional Monte Carlo simulation of the same window
    (``simulate.simulate_market``). When it can answer a question -- an expected-best
    value at a simulated target, a survival at the horizon -- its answer wins, because
    it prices the correlations the analytic model assumes away; everything it cannot
    answer falls back to the analytic model. The plan DP is untouched either way: the
    market fixes the *marginals*, the DP keeps the assignment exact.
    """
    by_position: dict[str, list[PlayerValuation]] = {}
    for valuation in available:
        by_position.setdefault(valuation.position, []).append(valuation)
    for pool in by_position.values():
        pool.sort(key=lambda v: -levels.vor(v))

    demand_by_pick = position_demand or {}

    def demand_at(pick: int, position: str) -> float:
        return demand_by_pick.get(pick, {}).get(position, 1.0)

    def shift_of(position: str) -> float:
        return tendencies.shift(position) if tendencies is not None else 0.0

    # My future turns; extrapolated when only the next pick is known. No next pick -> no
    # plan, raw value.
    if future_picks is not None:
        futures = sorted(pick for pick in future_picks if pick > current_pick)[:_MAX_PLAN_PICKS]
    elif next_pick is not None:
        futures = extrapolated_picks(current_pick, next_pick, num_teams)
    else:
        futures = []

    # If this is your last pick there is no "next available" -- fall back to raw value.
    horizon = next_pick if next_pick is not None else current_pick

    # The pick-budget normalizer per target: over the whole board, expected removals
    # must equal the picks that will actually happen. Solved once per target on the
    # demand-adjusted survivals of every available player.
    known_my_picks = (
        sorted(set(my_picks)) if my_picks is not None else sorted({current_pick, *futures})
    )
    targets = {pick for pick in {horizon, *futures} if pick > current_pick}
    pool_survivals = {
        target: [
            survival_probability(
                valuation,
                current_pick=current_pick,
                target_pick=target,
                demand=demand_at(target, valuation.position),
                adp_shift=shift_of(valuation.position),
            )
            for valuation in available
        ]
        for target in targets
    }
    normalizers = survival_normalizers(
        pool_survivals, current_pick=current_pick, my_picks=known_my_picks
    )

    open_dedicated, open_flex, backups = assign_lineup(roster_counts, settings)

    # How full my roster is (for the upside phase-in) and how many starters each
    # position gets dedicated slots for (for the bye-stack thinness check).
    roster_size = settings.roster_size or 0
    fullness = sum(roster_counts.values()) / roster_size if roster_size else 0.0
    dedicated_starters: dict[str, int] = {}
    for slot in settings.starting_slots:
        if len(slot.eligible_positions) == 1:
            slot_position = next(iter(slot.eligible_positions))
            dedicated_starters[slot_position] = (
                dedicated_starters.get(slot_position, 0) + slot.count
            )

    # One entry per open starting slot: the positions that can fill it. Ranks are not
    # precomputed -- a slot's rank depends on how many same-position slots the plan has
    # already filled, which the DP tracks per assignment (a second RB, dedicated or
    # flex-routed, is priced at the expected *second*-best survivor because two slots
    # cannot lean on the same fallback player).
    needs: list[frozenset[str]] = []
    for position in sorted(open_dedicated):
        needs.extend([frozenset({position})] * open_dedicated[position])
    for eligible, count in open_flex:
        needs.extend([eligible] * count)
    needs = needs[:_MAX_PLAN_PICKS]

    e_cache: dict[tuple, float] = {}

    def expected_at(position: str, rank: int, pick: int, exclude: str | None) -> float:
        key = (position, rank, pick, exclude)
        if key not in e_cache:
            value = None if market is None else market.expected_at(position, rank, pick, exclude)
            if value is None:
                pool = by_position.get(position, [])
                if exclude is not None:
                    pool = [c for c in pool if c.player_key != exclude]
                value = expected_best_available(
                    pool,
                    levels,
                    current_pick=current_pick,
                    target_pick=pick,
                    demand=demand_at(pick, position),
                    adp_shift=shift_of(position),
                    normalizer=normalizers.get(pick, 1.0),
                    rank=rank,
                )
            e_cache[key] = value
        return e_cache[key]

    # Positions the plan can route more than one slot to need their assignment count in
    # the DP state -- the count is what prices the second slot at rank 2. Positions that
    # can only ever be assigned once are always rank 1 and stay out of the state.
    assignable: dict[str, int] = {}
    for eligible in needs:
        for position in eligible:
            if position in by_position:
                assignable[position] = assignable.get(position, 0) + 1
    tracked = sorted(position for position, count in assignable.items() if count > 1)
    counts_slot = {position: index for index, position in enumerate(tracked)}
    zero_counts = (0,) * len(tracked)

    plan_cache: dict[tuple, float] = {}

    def plan_value(
        released: int | None, exclude: str | None, exclude_position: str | None
    ) -> float:
        """Best assignment of the remaining needs to my future picks (exact DP).

        State is (filled-needs bitmask, per-position assignment counts). The counts make
        flex routing price exactly: a W/R/T sent to RB behind a dedicated RB slot is the
        *second* RB the plan buys and is valued at rank 2 -- but only when the plan
        actually routes it there, not because a precomputed label said so.
        """
        key = (released, exclude)
        if key in plan_cache:
            return plan_cache[key]
        remaining = [need for index, need in enumerate(needs) if index != released]
        picks = futures[: len(remaining)]
        best: dict[tuple[int, tuple[int, ...]], float] = {(0, zero_counts): 0.0}
        for pick in picks:
            reachable = dict(best)
            for (mask, counts), total in best.items():
                for index, eligible in enumerate(remaining):
                    if mask >> index & 1:
                        continue
                    for position in eligible:
                        if position not in by_position:
                            continue
                        slot = counts_slot.get(position)
                        rank = 1 if slot is None else counts[slot] + 1
                        value = expected_at(
                            position,
                            rank,
                            pick,
                            exclude if position == exclude_position else None,
                        )
                        if value <= 0.0:
                            continue
                        if slot is None:
                            next_counts = counts
                        else:
                            next_counts = (
                                counts[:slot] + (counts[slot] + 1,) + counts[slot + 1 :]
                            )
                        state = (mask | 1 << index, next_counts)
                        if total + value > reachable.get(state, 0.0):
                            reachable[state] = total + value
            best = reachable
        result = max(best.values()) if best else 0.0
        plan_cache[key] = result
        return result

    recommendations: list[Recommendation] = []
    # One tail note per position, computed lazily: the wipeout is a fact about the
    # position's tier, not about any one candidate.
    tail_notes: dict[str, str] = {}
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

        # The slot he would fill releases its need from the plan. Dedicated slots at his
        # position are interchangeable, so any one will do; when only flex slots can
        # take him, releasing different flexes constrains the remaining plan differently
        # (a W/T freed is not a W/R/T freed), so every option is tried and the best kept.
        dedicated_needs = [
            index for index, eligible in enumerate(needs) if eligible == frozenset({position})
        ]
        if dedicated_needs:
            released_options: list[int | None] = [dedicated_needs[0]]
        else:
            flex_needs = [index for index, eligible in enumerate(needs) if position in eligible]
            released_options = list(flex_needs) if flex_needs else [None]

        # Taking him means he is no longer his own position's fallback. Only worth
        # modelling for the top of the pool; below that the effect is noise.
        is_top = any(c.player_key == valuation.player_key for c in pool[:_EXCLUSION_DEPTH])
        exclude = valuation.player_key if is_top else None

        # "Next available" excludes the candidate himself: his VONA measures the cliff
        # *behind* him, not his own habit of surviving. (His own survival is reported
        # separately -- the two argue for opposite actions.)
        vona = vor - expected_at(position, 1, horizon, exclude)
        survival = None if market is None else market.survival(valuation.player_key, horizon)
        if survival is None:
            survival = survival_probability(
                valuation,
                current_pick=current_pick,
                target_pick=horizon,
                demand=demand_at(horizon, position),
                adp_shift=shift_of(position),
                normalizer=normalizers.get(horizon, 1.0),
            )

        own = penalized(vor, factor)
        if valuation.is_injured:
            own = penalized(own, _INJURY_RESIDUAL)

        # Bench rounds tilt toward volatility; a bye stacked on a thin position costs a
        # real lineup week. Both live in ``own``: they are about *this* player on *my*
        # roster, not about the market.
        dart = upside_bonus(valuation, roster_fullness=fullness)
        own += dart
        bye_clash = False
        if roster_byes and valuation.bye_week is not None:
            same_bye = roster_byes.get(position, [])
            thin = held <= dedicated_starters.get(position, 0) + 1
            if valuation.bye_week in same_bye and thin:
                own -= _BYE_PENALTY
                bye_clash = True

        score = own + max(
            plan_value(released, exclude, position if exclude else None)
            for released in released_options
        )

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
                    tail_notes.setdefault(
                        position, market.tail_note(position) if market is not None else ""
                    ),
                    dart=dart,
                    bye_clash=bye_clash,
                ),
            )
        )

    recommendations.sort(key=lambda r: -r.score)
    return recommendations[:limit]


def _wait_note(
    position: str,
    needs: list[frozenset[str]],
    futures: list[int],
    by_position: dict[str, list[PlayerValuation]],
    expected_at,
) -> str:
    """What taking this player leaves on the table at my next turn, when it is plenty."""
    if not futures:
        return ""
    best_value, best_position = 0.0, None
    seen: set[str] = set()
    for eligible in needs:
        for p in eligible:
            if p == position or p not in by_position or p in seen:
                continue
            seen.add(p)
            value = expected_at(p, 1, futures[0], None)
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
    tail_note: str = "",
    *,
    dart: float = 0.0,
    bye_clash: bool = False,
) -> str:
    """A one-line, human-checkable reason. You are the one making the pick."""
    gone = 1.0 - survival
    parts: list[str] = []

    # "He will still be there" and "others like him will be" argue for opposite actions;
    # VONA excludes the player himself, so the two messages can be told apart.
    can_wait_on_him = vona <= 1 and survival >= 0.6

    if vona >= 12:
        parts.append(f"big drop-off at {valuation.position} after him")
    elif vona >= 5:
        parts.append(f"meaningful {valuation.position} gap if you wait")
    elif vona <= 1:
        if can_wait_on_him:
            parts.append(f"he should still be there at pick {horizon} ({survival:.0%})")
        else:
            parts.append(f"comparable {valuation.position}s should survive")

    if gone >= 0.75:
        parts.append(f"{gone:.0%} gone by pick {horizon}")
    elif gone <= 0.25 and not can_wait_on_him:
        parts.append(f"likely still there at {horizon}")

    if demand <= 0.5:
        parts.append(f"few teams before pick {horizon} need a {valuation.position}")

    if wait_note:
        parts.append(wait_note)
    if tail_note:
        parts.append(tail_note)

    if valuation.tier is not None:
        parts.append(f"tier {valuation.tier}")

    if bye_clash:
        parts.append(f"shares week {valuation.bye_week} bye with your {valuation.position}")
    if dart >= 1.0:
        parts.append("high-variance upside; late-round dart")

    if factor < 1.0:
        parts.append(f"no open slot for him; you hold {held} at {valuation.position}")

    if valuation.is_injured:
        if valuation.availability < 1.0:
            parts.append(
                f"projection cut {1 - valuation.availability:.0%} for {valuation.status}"
            )
        else:
            parts.append(f"injury status {valuation.status}")
    if valuation.points_estimated:
        parts.append("projection interpolated")

    return "; ".join(parts) if parts else f"VOR {vor:.0f}"
