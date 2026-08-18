"""Monte Carlo market simulation -- survival by playing the draft out, not by formula.

The analytic model (``vona.survival_probability`` plus the pick-budget normalizer)
treats players as if they survive or fall independently, then squeezes the pool until
the removals add up. Good approximation, one blind spot: correlation. Whether the third
receiver survives depends on whether the first two did, because the teams picking in
between can each take only one player -- and which one they take depends on what their
roster already looks like.

So this module plays the next stretch of the draft out a few hundred times. Each rollout
walks the real pick order; at every opponent pick one player is sampled from the best
still available, weighted by his ADP hazard (how due he is at exactly this pick) times
whether that team still has a starting slot for his position. My own picks remove
nobody: the plan DP in ``vona.recommend`` owns my choices, and the simulator's job is to
model everyone else's.

The result answers the same questions the analytic model answers -- "what will the best
remaining player at each position be worth at my future picks?" and "will *he* still be
there?" -- but by counting rollouts instead of composing formulas, so every correlation
the pick order implies is priced in for free. It is opt-in (``FF_MC_ROLLOUTS``) until
backtests prove it earns its latency; when a question falls outside the simulated
window, consumers fall back to the analytic model.
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass, field
from math import exp

from ff_helper.engine.lineup import assign_lineup
from ff_helper.engine.replacement import ReplacementLevels
from ff_helper.engine.room import RoomTendencies

# Private by convention but shared on purpose: the simulator must price picks on the
# same logistic curve, bench appetite, and exclusion depth the analytic model uses, or
# the two disagree about the world instead of about the correlations.
from ff_helper.engine.vona import (
    _DEMAND_FLOOR,
    _EXCLUSION_DEPTH,
    _EXP_LIMIT,
    _LOGISTIC_SCALE,
)
from ff_helper.rankings.blend import PlayerValuation
from ff_helper.yahoo.models import LeagueSettings

# Weight multiplier for a player whose position the picking team has no starting slot
# for. Same constant as the analytic demand floor: teams draft bench players too.
_BENCH_WEIGHT = _DEMAND_FLOOR

# Windows up to this many picks run the full configured rollout count; longer windows
# scale the count down (never below ``min_rollouts``) so wall-clock stays roughly flat.
_FULL_SPEED_WINDOW = 30

# A tier-wipeout probability below this is not worth a line of the user's attention,
# and above the ceiling the tier is effectively gone already -- the cliff has happened,
# and VONA is the number that says so.
_TAIL_NOTE_MIN = 0.15
_TAIL_NOTE_MAX = 0.95


@dataclass(frozen=True)
class SimulationConfig:
    rollouts: int = 300
    # None derives a seed from the available-player set, so an unchanged board gives
    # identical numbers across polls instead of advice that flickers between refreshes.
    seed: int | None = None
    # Opponent picks are sampled from at most this many of the best remaining players.
    candidate_pool: int = 48
    # Deepest slot rank ``expected_at`` can answer for.
    max_rank: int = 8
    # How many of each position's top tiers get wipeout probabilities tracked.
    tail_tiers: int = 2
    # Targets beyond this many picks out are not simulated; the analytic model covers
    # them. Late-round expected values are dominated by replacement level anyway.
    window: int = 60
    min_rollouts: int = 150


@dataclass(frozen=True)
class SimulationResult:
    """What the rollouts said, queryable the same way the analytic model is.

    Every query returns ``None`` when this simulation cannot answer it -- a pick outside
    the simulated targets, a rank deeper than was recorded -- and the caller falls back
    to the analytic model. Absence of an answer must never masquerade as zero.
    """

    rollouts: int
    # The picks actually simulated to, ascending; () when nothing was in the window.
    targets: tuple[int, ...]
    first_target: int | None
    max_rank: int
    # (position, target pick) -> per-rollout tuple of surviving player keys, best first.
    survivors: dict[tuple[str, int], list[tuple[str, ...]]]
    vor_of: dict[str, float]
    # Rollouts in which each player was still available at ``first_target``.
    survival_counts: dict[str, int]
    # (position, tier, target pick) -> P(every available player of that tier is gone).
    tier_gone: dict[tuple[str, int, int], float]
    # target pick -> per-rollout count of players removed by then. Every entry must
    # equal the number of opponent picks in the window -- the invariant the analytic
    # normalizer only approximates -- and the tests hold it to that.
    removed_counts: dict[int, tuple[int, ...]]
    _expected_cache: dict[tuple, float] = field(default_factory=dict, repr=False)

    def expected_at(
        self, position: str, rank: int, pick: int, exclude: str | None = None
    ) -> float | None:
        """Mean VOR of the ``rank``-th best survivor at ``pick``, over the rollouts.

        A rollout with fewer than ``rank`` survivors contributes 0 -- the slot gets a
        replacement-level player, VOR 0 by definition, matching the analytic model's
        leftover-mass convention.
        """
        if self.rollouts <= 0 or rank > self.max_rank:
            return None
        rows = self.survivors.get((position, pick))
        if rows is None:
            return None
        key = (position, rank, pick, exclude)
        cached = self._expected_cache.get(key)
        if cached is None:
            total = 0.0
            for row in rows:
                seen = 0
                for player_key in row:
                    if player_key == exclude:
                        continue
                    seen += 1
                    if seen == rank:
                        total += self.vor_of[player_key]
                        break
            cached = total / len(rows)
            self._expected_cache[key] = cached
        return cached

    def survival(self, player_key: str, pick: int) -> float | None:
        """P(still available at ``pick``); only answered at the first target."""
        if self.rollouts <= 0 or pick != self.first_target:
            return None
        count = self.survival_counts.get(player_key)
        return None if count is None else count / self.rollouts

    def tail_note(self, position: str) -> str:
        """The one tier-wipeout worth telling the user about, or the empty string.

        Best (lowest) tier first, earliest pick first: "your tier-1 backs might all be
        gone next turn" beats every other message this method could produce.
        """
        for (pos, tier, target), probability in sorted(self.tier_gone.items()):
            if pos == position and _TAIL_NOTE_MIN <= probability < _TAIL_NOTE_MAX:
                return (
                    f"all tier-{tier} {position}s gone by your pick {target} "
                    f"in {probability:.0%} of sims"
                )
        return ""


def _pick_hazard(pick: int, adp: float, sigma: float) -> float:
    """P(drafted on exactly this pick | still available when it comes up).

    The mass is taken over [pick - 0.5, pick + 0.5) -- "his ADP is 24" means he goes
    *around* pick 24, not somewhere in [24, 25). Conditioning on being available is what
    makes fallers behave: their remaining mass is thin everywhere, but relative to what
    is left of it they stay every bit as draftable, and in the deep tail the logistic
    hazard settles at a constant rather than fading to zero.
    """
    scale = max(sigma, 0.5) * _LOGISTIC_SCALE
    step = 1.0 / scale
    z = (pick - 0.5 - adp) / scale
    if z > _EXP_LIMIT:
        return 1.0 - exp(-step)
    if z < -_EXP_LIMIT:
        return 0.0
    # 1 - S(pick + 0.5)/S(pick - 0.5) for the logistic survival S(x) = 1/(1 + e^z).
    return 1.0 - (1.0 + exp(z)) / (1.0 + exp(z + step))


def _empty_result(config: SimulationConfig) -> SimulationResult:
    return SimulationResult(
        rollouts=0,
        targets=(),
        first_target=None,
        max_rank=config.max_rank,
        survivors={},
        vor_of={},
        survival_counts={},
        tier_gone={},
        removed_counts={},
    )


def simulate_market(
    available: list[PlayerValuation],
    levels: ReplacementLevels,
    settings: LeagueSettings,
    *,
    current_pick: int,
    my_picks: list[int],
    targets: list[int],
    pick_owner: dict[int, str],
    team_rosters: dict[str, dict[str, int]],
    tendencies: RoomTendencies | None = None,
    config: SimulationConfig | None = None,
) -> SimulationResult:
    """Roll the draft forward to each of ``targets`` and tally what survived.

    ``pick_owner`` maps pick numbers to the team on the clock; a pick with no known
    owner is drafted by a generic team that needs everything. ``team_rosters`` is each
    team's current position counts -- copied per rollout and updated as the simulated
    team drafts, which is where positional demand becomes *endogenous*: a team stops
    chasing running backs the moment a rollout gives it enough of them.
    """
    config = config or SimulationConfig()
    covered = tuple(
        sorted(t for t in set(targets) if current_pick < t <= current_pick + config.window)
    )
    if not covered or not available or config.rollouts <= 0:
        return _empty_result(config)

    def shift(position: str) -> float:
        return tendencies.shift(position) if tendencies is not None else 0.0

    # ADP order (this room's, tendencies applied) decides who counts as "the best still
    # available" for the candidate pool; VOR order decides who counts as the best
    # survivor. They are different orders on purpose -- rooms draft by ADP, I value by VOR.
    pool = sorted(available, key=lambda v: v.adp + shift(v.position))
    npool = len(pool)
    keys = [v.player_key for v in pool]
    positions = [v.position for v in pool]

    by_position: dict[str, list[int]] = {}
    for index in range(npool):
        by_position.setdefault(positions[index], []).append(index)
    for indices in by_position.values():
        indices.sort(key=lambda i: -levels.vor(pool[i]))

    tier_members: dict[tuple[str, int], list[int]] = {}
    for position, indices in by_position.items():
        tiers = sorted({pool[i].tier for i in indices if pool[i].tier is not None})
        for tier in tiers[: config.tail_tiers]:
            tier_members[(position, tier)] = [i for i in indices if pool[i].tier == tier]

    last = covered[-1]
    my_set = set(my_picks)
    # Hazards depend only on the pick number and the player, so the whole table is
    # computed once; per rollout only the need multipliers and availability vary.
    hazard_rows = {
        n: [
            _pick_hazard(n, pool[i].adp + shift(positions[i]), pool[i].adp_stdev)
            for i in range(npool)
        ]
        for n in range(current_pick, last)
        if n not in my_set
    }

    need_cache: dict[tuple, frozenset[str]] = {}

    def needed_positions(counts: dict[str, int]) -> frozenset[str]:
        key = tuple(sorted(counts.items()))
        cached = need_cache.get(key)
        if cached is None:
            open_dedicated, open_flex, _ = assign_lineup(counts, settings)
            needed = {p for p, count in open_dedicated.items() if count > 0}
            for eligible, count in open_flex:
                if count > 0:
                    needed |= set(eligible)
            cached = frozenset(needed)
            need_cache[key] = cached
        return cached

    window_picks = max(1, last - current_pick)
    rollouts = min(
        config.rollouts,
        max(config.min_rollouts, config.rollouts * _FULL_SPEED_WINDOW // window_picks),
    )

    seed = config.seed
    if seed is None:
        seed = zlib.crc32(",".join(sorted(keys)).encode())
    rng = random.Random(seed)

    store_depth = config.max_rank + _EXCLUSION_DEPTH
    first = covered[0]
    target_set = set(covered)
    survivors: dict[tuple[str, int], list[tuple[str, ...]]] = {
        (position, target): [] for position in by_position for target in covered
    }
    survival_counts = dict.fromkeys(keys, 0)
    gone_counts = {(pos, tier, t): 0 for (pos, tier) in tier_members for t in covered}
    removed_lists: dict[int, list[int]] = {target: [] for target in covered}
    candidate_pool = config.candidate_pool

    for _ in range(rollouts):
        removed = bytearray(npool)
        removed_total = 0
        counts = {team: dict(held) for team, held in team_rosters.items()}
        for n in range(current_pick, last + 1):
            if n in target_set:
                removed_lists[n].append(removed_total)
                if n == first:
                    for i in range(npool):
                        if not removed[i]:
                            survival_counts[keys[i]] += 1
                for position, indices in by_position.items():
                    row: list[str] = []
                    for i in indices:
                        if not removed[i]:
                            row.append(keys[i])
                            if len(row) == store_depth:
                                break
                    survivors[(position, n)].append(tuple(row))
                for (position, tier), members in tier_members.items():
                    if all(removed[i] for i in members):
                        gone_counts[(position, tier, n)] += 1
                if n == last:
                    break
            if n in my_set:
                # The plan DP owns my choices; simulating them here would double-model
                # them and quietly fight whatever the plan decides.
                continue
            hazards = hazard_rows[n]
            owner = pick_owner.get(n)
            needed = needed_positions(counts[owner]) if owner in counts else None
            total = 0.0
            candidate_indices: list[int] = []
            weights: list[float] = []
            for i in range(npool):
                if removed[i]:
                    continue
                weight = hazards[i]
                if needed is not None and positions[i] not in needed:
                    weight *= _BENCH_WEIGHT
                candidate_indices.append(i)
                weights.append(weight)
                total += weight
                if len(candidate_indices) == candidate_pool:
                    break
            if not candidate_indices:
                break  # pool exhausted; nothing left for anyone to remove
            if total <= 0.0:
                # Nobody in range is due yet; the best remaining by ADP is the default.
                chosen = candidate_indices[0]
            else:
                x = rng.random() * total
                chosen = candidate_indices[-1]
                for i, weight in zip(candidate_indices, weights, strict=True):
                    x -= weight
                    if x <= 0.0:
                        chosen = i
                        break
            removed[chosen] = 1
            removed_total += 1
            if owner in counts:
                team = counts[owner]
                team[positions[chosen]] = team.get(positions[chosen], 0) + 1

    return SimulationResult(
        rollouts=rollouts,
        targets=covered,
        first_target=first,
        max_rank=config.max_rank,
        survivors=survivors,
        vor_of={keys[i]: max(0.0, levels.vor(pool[i])) for i in range(npool)},
        survival_counts=survival_counts,
        tier_gone={key: count / rollouts for key, count in gone_counts.items()},
        removed_counts={target: tuple(values) for target, values in removed_lists.items()},
    )
