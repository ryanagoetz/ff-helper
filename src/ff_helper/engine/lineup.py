"""Lineup-slot accounting shared by both draft engines.

Both formats ask the same underlying question -- "does this player fill a real slot on my
starting lineup, and if not, how much is depth worth?" -- and both used to answer it with
``starters_at``, which counts a flex slot toward every position that can fill it. That
overcount makes a 2RB+1FLEX league look like a 3-RB league even when the flex is already
spoken for by a receiver. Here the players a team holds are assigned to actual slots,
dedicated first and then flex, and everything downstream reasons about what is truly open.
"""

from __future__ import annotations

from ff_helper.yahoo.models import LeagueSettings

# How much a marginal player at a position is worth once the starting slots are filled.
# Index 0 is the first backup -- still genuinely useful for bye weeks and injuries -- and
# it decays fast from there. A fourth running back in a 2-RB league is roster filler.
_DEPTH_DISCOUNT = (0.55, 0.30, 0.15, 0.08, 0.04)


def depth_multiplier(count_at_position: int, starters_needed: int) -> float:
    """How much a marginal player at this position is worth given what you already have.

    A third quarterback in a one-QB league is nearly worthless no matter how he grades.
    """
    if count_at_position < starters_needed:
        return 1.0
    # 0 = the first player past your starting requirement, i.e. the first backup.
    surplus = count_at_position - starters_needed
    return _DEPTH_DISCOUNT[min(surplus, len(_DEPTH_DISCOUNT) - 1)]


def assign_lineup(
    roster_counts: dict[str, int], settings: LeagueSettings
) -> tuple[dict[str, int], list[tuple[frozenset[str], int]], dict[str, int]]:
    """Greedily place the players a team holds into real lineup slots.

    Returns (open dedicated slots by position, open flex slots as (eligible, count),
    backups by position -- players holding no starting slot at all).
    """
    open_dedicated: dict[str, int] = {}
    flex: list[list] = []  # [eligible positions, slots left]
    for slot in settings.starting_slots:
        eligible = slot.eligible_positions
        if len(eligible) == 1:
            position = next(iter(eligible))
            open_dedicated[position] = open_dedicated.get(position, 0) + slot.count
        else:
            flex.append([eligible, slot.count])

    backups: dict[str, int] = {}
    for position, count in roster_counts.items():
        remaining = count
        used = min(remaining, open_dedicated.get(position, 0))
        if used:
            open_dedicated[position] -= used
            remaining -= used
        for entry in flex:
            if remaining <= 0:
                break
            if position in entry[0] and entry[1] > 0:
                used = min(remaining, entry[1])
                entry[1] -= used
                remaining -= used
        if remaining:
            backups[position] = backups.get(position, 0) + remaining

    open_flex = [(eligible, count) for eligible, count in flex]
    return open_dedicated, open_flex, backups


def need_factor(
    roster_counts: dict[str, int], position: str, settings: LeagueSettings
) -> float:
    """How much a marginal player at this position is worth given your open lineup slots.

    Any open starting slot he can fill is full value; a bench spot decays with how many
    backups you already hold at his position.
    """
    open_dedicated, open_flex, backups = assign_lineup(roster_counts, settings)
    if open_dedicated.get(position, 0) > 0:
        return 1.0
    if any(position in eligible and count > 0 for eligible, count in open_flex):
        return 1.0
    # depth_multiplier with one required starter maps "n backups held" onto the shared
    # decay table: the first backup is still bye-week insurance, the fourth is filler.
    return depth_multiplier(backups.get(position, 0) + 1, 1)
