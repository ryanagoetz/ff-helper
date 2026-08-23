"""Lineup-slot accounting, shared by every engine above it.

Both draft formats ask the same underlying question -- "does this player fill a real slot
on my starting lineup, and if not, how much is depth worth?" -- and both used to answer it
with ``starters_at``, which counts a flex slot toward every position that can fill it. That
overcount makes a 2RB+1FLEX league look like a 3-RB league even when the flex is already
spoken for by a receiver. Here the players a team holds are assigned to actual slots,
dedicated first and then flex, and everything downstream reasons about what is truly open.

This module sits *beside* ``replacement.py`` rather than above it: the snake engine, the
auction engine, and the in-season engine all import it, and none of them import each other.

Two levels live here, and the difference matters:

- ``assign_lineup`` / ``need_factor`` work on **counts**. They answer "is there an open
  slot at this position", which is all a draft recommendation needs and is cheap enough to
  run inside a hot loop.
- ``optimal_lineup`` works on **players**, and returns the actual assignment. It is exact
  rather than greedy, which the counts version cannot be and does not need to be.

The greedy version this replaced claimed to be "exact for every layout Yahoo actually
offers". It is not. ``FLEX_ELIGIBILITY`` contains both ``W/R`` = {WR, RB} and ``W/T`` =
{WR, TE}, whose eligibility sets are not nested, and greedy fills flex slots in settings
order taking the best eligible player left each time::

    Slots: W/R x1, W/T x1.   Roster: WR-A 20.0, RB-A 12.0, TE-A 5.0
    greedy:   W/R takes WR-A (20), W/T is left TE-A  (5)  -> 25.0
    optimal:  W/R takes RB-A (12), W/T takes WR-A   (20)  -> 32.0

Greedy happens to be exact when the eligibility sets *are* nested, which the common Yahoo
layouts are -- but that was an unproven invariant defended by a comment, and it collapses
the moment a league uses two sibling flexes. Exactness also stops being optional in-season,
where the same assignment gets re-run under a modified objective and greedy has no
optimality argument left at all.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

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


# --------------------------------------------------------------------------------------
# The exact optimizer
# --------------------------------------------------------------------------------------


class LineupPlayer(Protocol):
    """What the optimizer needs from a player, and nothing more.

    A Protocol rather than a base class so ``PlayerValuation`` (season) and
    ``WeeklyValuation`` (in-season) both satisfy it without either learning about the
    other, and so a caller can pass its own type without an adapter.
    """

    player_key: str
    eligible_positions: tuple[str, ...]
    projected_points: float


@dataclass(frozen=True)
class LineupSlot:
    """One concrete starting slot. A ``RosterSlot`` with count 2 expands to two of these."""

    index: int
    position: str  # the label, e.g. "W/R/T"
    eligible: frozenset[str]

    @property
    def is_flex(self) -> bool:
        return len(self.eligible) > 1


@dataclass(frozen=True)
class Lineup:
    starters: tuple[tuple[LineupSlot, str], ...]  # (slot, player_key), slot order
    bench: tuple[str, ...]
    # Slots nothing was assigned to. Reported rather than silently scored 0, because "I
    # have no startable tight end" is a fact worth acting on and an invisible zero is not.
    empty: tuple[LineupSlot, ...]
    total: float

    @property
    def starter_keys(self) -> tuple[str, ...]:
        return tuple(player_key for _, player_key in self.starters)

    def slot_of(self, player_key: str) -> LineupSlot | None:
        for slot, key in self.starters:
            if key == player_key:
                return slot
        return None


def starting_slots(settings: LeagueSettings) -> tuple[LineupSlot, ...]:
    """Expand the league's starting slots into individually addressable ones."""
    slots: list[LineupSlot] = []
    for roster_slot in settings.starting_slots:
        for _ in range(roster_slot.count):
            slots.append(
                LineupSlot(
                    index=len(slots),
                    position=roster_slot.position,
                    eligible=roster_slot.eligible_positions,
                )
            )
    return tuple(slots)


# Cost of putting a player in a slot he cannot fill. Finite rather than infinite so the
# assignment arithmetic stays in real numbers -- inf - inf is nan, and one nan silently
# turns the whole matrix into garbage. Large enough that no real point total approaches it.
_INELIGIBLE = 1e9


def optimal_lineup(
    players: Sequence[LineupPlayer],
    settings: LeagueSettings,
    *,
    value_of: Callable[[LineupPlayer], float] | None = None,
    locked: Mapping[int, str] | None = None,
) -> Lineup:
    """The highest-scoring legal starting lineup, and the assignment that produces it.

    Exact, not greedy -- see the module docstring for the layout that breaks greedy.

    ``value_of`` defaults to projected points. Passing something else is how the in-season
    engine reuses this under a different objective without the optimizer knowing what the
    objective means.

    ``locked`` maps slot index to player key for slots that can no longer be changed --
    on Sunday afternoon, everyone whose game has kicked off. The remaining slots are then
    optimized *given* those, which is what makes a gameday recommendation actionable
    rather than a description of what you should have done at noon.

    A slot no eligible player can fill comes back in ``empty``. So does a slot whose only
    eligible players project negative, because an empty slot scores zero and that is
    genuinely better.
    """
    value = value_of or (lambda player: player.projected_points)
    slots = starting_slots(settings)
    by_key = {player.player_key: player for player in players}

    locked = dict(locked or {})
    locked_slots: dict[int, str] = {}
    for slot_index, player_key in locked.items():
        if player_key not in by_key:
            # A locked slot naming a player we do not have produces a lineup that cannot
            # actually be set. Refusing beats returning one that looks fine.
            raise ValueError(
                f"locked slot {slot_index} names {player_key!r}, who is not in the roster"
            )
        if not 0 <= slot_index < len(slots):
            raise ValueError(f"locked slot {slot_index} is not a slot in this league")
        locked_slots[slot_index] = player_key

    free_slots = [slot for slot in slots if slot.index not in locked_slots]
    taken = set(locked_slots.values())
    available = [player for player in players if player.player_key not in taken]

    assigned = _assign(free_slots, available, value)

    starters: list[tuple[LineupSlot, str]] = []
    empty: list[LineupSlot] = []
    used: set[str] = set(locked_slots.values())
    total = sum(value(by_key[key]) for key in locked_slots.values())

    for slot in slots:
        if slot.index in locked_slots:
            starters.append((slot, locked_slots[slot.index]))
            continue
        player_key = assigned.get(slot.index)
        if player_key is None:
            empty.append(slot)
            continue
        starters.append((slot, player_key))
        used.add(player_key)
        total += value(by_key[player_key])

    bench = tuple(p.player_key for p in players if p.player_key not in used)
    return Lineup(
        starters=tuple(starters), bench=bench, empty=tuple(empty), total=total
    )


def _assign(
    slots: list[LineupSlot],
    players: Sequence[LineupPlayer],
    value: Callable[[LineupPlayer], float],
) -> dict[int, str]:
    """Max-value assignment of players to slots. Returns {slot index: player key}."""
    if not slots or not players:
        return {}

    candidates = _candidates(slots, players, value)
    if not candidates:
        return {}

    # One dummy column per slot represents leaving that slot empty at zero value. With as
    # many dummies as slots, a solution never has to take an ineligible player, so the
    # _INELIGIBLE entries are unreachable rather than merely expensive.
    columns = len(candidates) + len(slots)
    cost = [[0.0] * columns for _ in slots]
    for row, slot in enumerate(slots):
        for column, player in enumerate(candidates):
            eligible = bool(slot.eligible & set(player.eligible_positions))
            cost[row][column] = -value(player) if eligible else _INELIGIBLE

    assignment = _hungarian(cost)

    result: dict[int, str] = {}
    for row, column in enumerate(assignment):
        if column < 0 or column >= len(candidates):
            continue  # a dummy column: this slot is better left empty
        if cost[row][column] >= _INELIGIBLE:
            continue  # unreachable in practice; refuse to emit an illegal lineup
        result[slots[row].index] = candidates[column].player_key
    return result


def _candidates(
    slots: list[LineupSlot],
    players: Sequence[LineupPlayer],
    value: Callable[[LineupPlayer], float],
) -> list[LineupPlayer]:
    """Trim the roster to the players that could possibly start.

    Only the top ``len(slots)`` players at any one position can ever be used: if a slot
    took the (n+1)-th best at a position while a better same-position player sat unused,
    swapping them is legal and strictly better. So everything past that rank is provably
    dead weight, and the matrix stays around 11 x 66 instead of 11 x (whole roster).
    """
    limit = len(slots)
    fillable = set().union(*(slot.eligible for slot in slots)) if slots else set()

    ranked = sorted(players, key=lambda player: -value(player))
    counts: dict[str, int] = {}
    kept: dict[str, LineupPlayer] = {}
    for player in ranked:
        for position in player.eligible_positions:
            if position not in fillable:
                continue
            if counts.get(position, 0) >= limit:
                continue
            counts[position] = counts.get(position, 0) + 1
            kept.setdefault(player.player_key, player)
    return list(kept.values())


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Minimum-cost assignment of every row to a distinct column, for rows <= columns.

    The standard O(n^2 m) shortest-augmenting-path formulation with potentials. Written
    out rather than pulled in because the dependency list here is five packages of real
    substance (httpx, fastapi, uvicorn, dotenv, selectolax) and adding scipy for sixty
    lines that run once a week is a poor trade.

    Returns ``assignment[row] = column``, or -1 for a row left unassigned (which cannot
    happen when rows <= columns, but is handled rather than assumed).
    """
    rows = len(cost)
    columns = len(cost[0]) if rows else 0
    if rows == 0 or columns == 0:
        return [-1] * rows
    if rows > columns:  # pragma: no cover - callers pad with dummy columns
        raise ValueError("the assignment needs at least as many columns as rows")

    infinity = float("inf")
    # 1-indexed working arrays; index 0 is the algorithm's virtual starting column.
    u = [0.0] * (rows + 1)
    v = [0.0] * (columns + 1)
    match = [0] * (columns + 1)  # match[column] = row currently assigned to it
    way = [0] * (columns + 1)

    for row in range(1, rows + 1):
        match[0] = row
        column = 0
        minimum = [infinity] * (columns + 1)
        used = [False] * (columns + 1)

        while True:
            used[column] = True
            current_row = match[column]
            delta = infinity
            next_column = 0
            for candidate in range(1, columns + 1):
                if used[candidate]:
                    continue
                reduced = cost[current_row - 1][candidate - 1] - u[current_row] - v[candidate]
                if reduced < minimum[candidate]:
                    minimum[candidate] = reduced
                    way[candidate] = column
                if minimum[candidate] < delta:
                    delta = minimum[candidate]
                    next_column = candidate
            for candidate in range(columns + 1):
                if used[candidate]:
                    u[match[candidate]] += delta
                    v[candidate] -= delta
                else:
                    minimum[candidate] -= delta
            column = next_column
            if match[column] == 0:
                break

        while column:
            previous = way[column]
            match[column] = match[previous]
            column = previous

    assignment = [-1] * rows
    for candidate in range(1, columns + 1):
        if match[candidate]:
            assignment[match[candidate] - 1] = candidate - 1
    return assignment
