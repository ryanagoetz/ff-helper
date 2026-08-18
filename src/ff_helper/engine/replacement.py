"""Replacement level: the baseline that makes positions comparable.

A quarterback's 320 projected points and a running back's 210 are not comparable numbers.
What matters is the margin over what you could get *for free* at that position -- because
if every team starts one QB and the 13th-best QB scores 290, then the best QB's 320 is
worth 30 points, not 320.

Getting the baseline right is most of the battle, and the usual shortcut (a fixed rule
like "replacement RB is RB30") mishandles flex slots. Instead we fill the league's actual
starting lineups greedily from the top of the projection board and see where each position
runs out. Flex slots then land on whichever position genuinely wins them, rather than
being split by assumption.
"""

from __future__ import annotations

from dataclasses import dataclass

from ff_helper.rankings.blend import PlayerValuation
from ff_helper.yahoo.models import LeagueSettings


@dataclass(frozen=True)
class ReplacementLevels:
    points: dict[str, float]
    starters_drafted: dict[str, int]

    def vor(self, valuation: PlayerValuation) -> float:
        baseline = self.points.get(valuation.position, 0.0)
        return valuation.projected_points - baseline


def compute(
    valuations: list[PlayerValuation],
    settings: LeagueSettings,
    num_teams: int,
) -> ReplacementLevels:
    """Derive a replacement level per position for this specific league."""
    pools: dict[str, list[PlayerValuation]] = {}
    for valuation in valuations:
        pools.setdefault(valuation.position, []).append(valuation)
    for pool in pools.values():
        pool.sort(key=lambda v: -v.projected_points)

    # How many of each position the league starts, before flex.
    taken: dict[str, int] = dict.fromkeys(pools, 0)
    for slot in settings.starting_slots:
        eligible = slot.eligible_positions
        if len(eligible) != 1:
            continue
        position = next(iter(eligible))
        if position in taken:
            taken[position] += slot.count * num_teams

    # Flex slots go to whoever is actually best among the remaining players. This is the
    # part a fixed rule gets wrong.
    for slot in settings.starting_slots:
        eligible = slot.eligible_positions
        if len(eligible) <= 1:
            continue
        for _ in range(slot.count * num_teams):
            best_position: str | None = None
            best_points = float("-inf")
            for position in eligible:
                pool = pools.get(position)
                if not pool:
                    continue
                index = taken.get(position, 0)
                if index >= len(pool):
                    continue
                if pool[index].projected_points > best_points:
                    best_points = pool[index].projected_points
                    best_position = position
            if best_position is None:
                break
            taken[best_position] += 1

    points: dict[str, float] = {}
    for position, pool in pools.items():
        if not pool:
            continue
        # Replacement is the first player *past* the startable set -- the guy you could
        # still have if you ignored the position entirely.
        index = min(taken.get(position, 0), len(pool) - 1)
        points[position] = pool[index].projected_points

    # Onesie streaming. At a position where every team starts exactly one and no flex
    # can hold a second (QB and K always; TE and DEF in most leagues), rosters carry
    # about one apiece -- so a startable player sits on waivers all season, and weekly
    # matchup-picking makes him at least the num_teams-th best by season points. VOR
    # measured against the deeper draft-day baseline overstates what an early pick at
    # these positions actually buys; the streamer is the honest floor.
    dedicated: dict[str, int] = {}
    flexed: set[str] = set()
    for slot in settings.starting_slots:
        eligible = slot.eligible_positions
        if len(eligible) == 1:
            position = next(iter(eligible))
            dedicated[position] = dedicated.get(position, 0) + slot.count
        else:
            flexed |= eligible
    for position, pool in pools.items():
        onesie = dedicated.get(position) == 1 and position not in flexed
        if onesie and len(pool) >= num_teams:
            points[position] = max(points[position], pool[num_teams - 1].projected_points)

    return ReplacementLevels(points=points, starters_drafted=taken)
