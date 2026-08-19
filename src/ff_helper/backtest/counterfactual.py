"""What roster would the engine have drafted? The end-to-end number.

Calibration says whether the probabilities were honest; this says whether following the
advice would have left you with a better team. A recorded draft is replayed with one
change: at my turns, a policy chooses the pick instead of history. Everyone else drafts
as they actually did, with one necessary adjustment -- when a policy takes a player an
opponent later took in real life, that opponent slides to their own next recorded pick
that is still available (and to best-remaining-by-ADP if their whole script is
exhausted). Displaced players stay in the pool, so the board never invents or loses
anyone.

Snake only: an auction counterfactual would need a model of how every *price* changes
once one sale differs, which is a different (and much harder) problem.
"""

from __future__ import annotations

from dataclasses import dataclass

from ff_helper.assistant import Assistant
from ff_helper.backtest.capture import DraftRecord, build_state
from ff_helper.rankings.blend import PlayerValuation
from ff_helper.rankings.cache import Snapshot
from ff_helper.yahoo.models import DraftPick, LeagueSettings

POLICIES = ("actual", "engine", "best_vor")


@dataclass(frozen=True)
class RosterResult:
    policy: str
    # Projected points of the best legal starting lineup this roster can field. THE
    # comparison number: raw roster sums reward hoarding a position no lineup can start.
    lineup_points: float
    total_vor: float
    total_points: float
    # (pick number, player name, position) for my final roster, in draft order.
    players: tuple[tuple[int, str, str], ...]


def counterfactual(record: DraftRecord, snapshot: Snapshot, *, policy: str) -> RosterResult:
    """Replay the record with ``policy`` making my picks; score my final roster."""
    if policy not in POLICIES:
        raise ValueError(f"Unknown policy {policy!r}; expected one of {POLICIES}.")
    if record.is_auction:
        raise ValueError("Counterfactual replay supports snake drafts only.")
    my_team = record.my_team
    if my_team is None:
        raise ValueError("Record does not identify my team; nothing to compare against.")

    league, state = build_state(record)
    assistant = Assistant.build(league, state, snapshot)

    # Each opponent's actual picks, in order -- their "script" for the replay.
    scripts: dict[str, list[str]] = {}
    for pick in record.picks:
        scripts.setdefault(pick.team_key, []).append(pick.player_key)

    drafted: set[str] = set()
    for pick in record.picks:
        if pick.team_key == my_team.team_key:
            chosen = _my_choice(assistant, policy, pick.player_key, drafted)
        else:
            chosen = _scripted_choice(assistant, scripts[pick.team_key], drafted)
        drafted.add(chosen)
        state.apply_sync(
            [
                DraftPick(
                    pick=pick.pick,
                    round=pick.round,
                    team_key=pick.team_key,
                    player_key=chosen,
                )
            ],
            timestamp=0.0,
        )

    players: list[tuple[int, str, str]] = []
    roster: list[PlayerValuation] = []
    total_vor = 0.0
    total_points = 0.0
    for pick in state.picks_by_team(my_team.team_key):
        valuation = assistant.valuations.valuations.get(pick.player_key)
        if valuation is not None:
            roster.append(valuation)
            total_vor += assistant.levels.vor(valuation)
            total_points += valuation.projected_points
        players.append(
            (
                pick.pick,
                assistant._player_name(pick.player_key),
                valuation.position if valuation else "?",
            )
        )
    settings = record.league.settings
    assert settings is not None  # enforced by DraftRecord construction
    return RosterResult(
        policy=policy,
        lineup_points=_lineup_points(roster, settings),
        total_vor=total_vor,
        total_points=total_points,
        players=tuple(players),
    )


def _lineup_points(roster: list[PlayerValuation], settings: LeagueSettings) -> float:
    """Projected points of the best legal starting lineup from this roster.

    Dedicated slots first, then flex from whoever is left -- greedy by points, the same
    fill order ``lineup.assign_lineup`` uses for counts. Not provably optimal against
    adversarial slot layouts, but exact for every layout Yahoo actually offers.
    """
    remaining = sorted(roster, key=lambda v: -v.projected_points)
    total = 0.0
    for slot in settings.starting_slots:
        if len(slot.eligible_positions) != 1:
            continue
        position = next(iter(slot.eligible_positions))
        for _ in range(slot.count):
            chosen = next((v for v in remaining if v.position == position), None)
            if chosen is not None:
                total += chosen.projected_points
                remaining.remove(chosen)
    for slot in settings.starting_slots:
        if len(slot.eligible_positions) == 1:
            continue
        for _ in range(slot.count):
            chosen = next((v for v in remaining if v.position in slot.eligible_positions), None)
            if chosen is not None:
                total += chosen.projected_points
                remaining.remove(chosen)
    return total


def _my_choice(assistant: Assistant, policy: str, actual_key: str, drafted: set[str]) -> str:
    if policy == "actual":
        return actual_key
    if policy == "best_vor":
        available = assistant.available()
        if available:
            return max(available, key=lambda v: assistant.levels.vor(v)).player_key
        # Same exhausted-pool rule as the engine branch: falling back to the actual
        # pick is only legal if this replay has not already seated him elsewhere --
        # a duplicate would inflate exactly the baseline the engine is compared to.
        return actual_key if actual_key not in drafted else _fallback_by_adp(assistant, drafted)
    # policy == "engine"
    recommendations = assistant.snake_recommendations(limit=1)
    if recommendations:
        return recommendations[0].valuation.player_key
    # Pool exhausted from the engine's point of view (players it cannot value); fall
    # back to history rather than skipping a turn.
    return actual_key if actual_key not in drafted else _fallback_by_adp(assistant, drafted)


def _scripted_choice(assistant: Assistant, script: list[str], drafted: set[str]) -> str:
    """The opponent's next actual pick that is still available."""
    while script:
        candidate = script.pop(0)
        if candidate not in drafted:
            return candidate
    return _fallback_by_adp(assistant, drafted)


def _fallback_by_adp(assistant: Assistant, drafted: set[str]) -> str:
    available = assistant.available()
    if not available:
        raise RuntimeError("Replay exhausted the valued player pool entirely.")
    return min(available, key=lambda v: v.adp).player_key
