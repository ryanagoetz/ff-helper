"""Normalizers for Yahoo's Fantasy API JSON.

Yahoo's JSON is a direct machine translation of their XML, and it shows. Two patterns
account for nearly all of the pain, and both are handled here so no caller ever has to:

1. **Collections are dicts keyed by stringified integers**, with a sibling ``count`` key::

       {"players": {"0": {...}, "1": {...}, "count": 2}}

2. **A single object is a list of fragments**, where each fragment is either a small dict
   or a *nested list* of small dicts, and the split point varies by endpoint and even by
   player (a multi-position player serializes differently than a single-position one)::

       {"player": [[{"player_key": "..."}, {"name": {...}}], {"draft_analysis": [...]}]}

Everything below is written to tolerate both shapes at every level, because assuming
either one specifically is exactly how this breaks silently mid-draft.
"""

from __future__ import annotations

from typing import Any

from ff_helper.yahoo.models import (
    DEFAULT_AUCTION_BUDGET,
    DraftAnalysis,
    DraftPick,
    KeptPlayer,
    League,
    LeagueSettings,
    RosterSlot,
    Team,
    YahooPlayer,
)


def collection_items(node: Any) -> list[Any]:
    """Yield the members of a Yahoo collection, whichever shape it arrived in."""
    if node is None:
        return []
    if isinstance(node, list):
        return [item for item in node if item not in (None, [], {})]
    if not isinstance(node, dict):
        return []
    items: list[Any] = []
    for key, value in node.items():
        if key == "count":
            continue
        # Real collections use stringified integer keys; anything else is a stray field.
        if isinstance(key, str) and key.isdigit():
            items.append(value)
    return items


def flatten(node: Any) -> dict[str, Any]:
    """Merge Yahoo's list-of-fragments representation of one object into a single dict.

    Later fragments win on key collisions, which matches how Yahoo orders overrides.
    """
    merged: dict[str, Any] = {}

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            for key, value in current.items():
                merged[key] = value
        elif isinstance(current, list):
            for item in current:
                walk(item)

    walk(node)
    return merged


def unwrap(node: Any, key: str) -> Any:
    """Pull ``key`` out of a node that may be a dict, a list of fragments, or absent."""
    if isinstance(node, dict) and key in node:
        return node[key]
    flat = flatten(node)
    return flat.get(key)


def _to_float(value: Any) -> float | None:
    """Yahoo returns numbers as strings, and empty/'-' for missing."""
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def content(payload: dict) -> Any:
    """Strip the ``fantasy_content`` envelope every response is wrapped in."""
    return payload.get("fantasy_content", payload)


# --------------------------------------------------------------------------------------
# Leagues
# --------------------------------------------------------------------------------------


def parse_league(node: Any) -> League:
    flat = flatten(node)
    settings_node = flat.get("settings")
    return League(
        league_key=flat.get("league_key", ""),
        league_id=str(flat.get("league_id", "")),
        name=flat.get("name", ""),
        num_teams=_to_int(flat.get("num_teams")) or 0,
        season=str(flat.get("season", "")),
        draft_status=flat.get("draft_status", ""),
        scoring_type=flat.get("scoring_type", ""),
        settings=parse_settings(settings_node) if settings_node else None,
    )


def parse_settings(node: Any) -> LeagueSettings:
    flat = flatten(node)

    slots: list[RosterSlot] = []
    for entry in collection_items(flat.get("roster_positions")) or (
        flat.get("roster_positions") if isinstance(flat.get("roster_positions"), list) else []
    ):
        position_node = unwrap(entry, "roster_position") or entry
        position_flat = flatten(position_node)
        position = position_flat.get("position")
        if not position:
            continue
        slots.append(RosterSlot(position=position, count=_to_int(position_flat.get("count")) or 0))

    modifiers: dict[int, float] = {}
    stats_node = unwrap(flat.get("stat_modifiers"), "stats")
    for entry in collection_items(stats_node) or (
        stats_node if isinstance(stats_node, list) else []
    ):
        stat_flat = flatten(unwrap(entry, "stat") or entry)
        stat_id = _to_int(stat_flat.get("stat_id"))
        value = _to_float(stat_flat.get("value"))
        if stat_id is not None and value is not None:
            modifiers[stat_id] = value

    return LeagueSettings(
        roster_slots=tuple(slots),
        stat_modifiers=modifiers,
        is_auction=str(flat.get("draft_type", "")).lower() == "auction",
        auction_budget=_parse_auction_budget(flat),
    )


# Yahoo is inconsistent about whether (and under what name) it publishes the auction
# budget, so try the plausible spellings before falling back to the platform default.
_BUDGET_KEYS = (
    "auction_budget_total",
    "auction_budget",
    "budget",
    "draft_budget",
    "salary_cap",
)


def _parse_auction_budget(flat: dict[str, Any]) -> int:
    for key in _BUDGET_KEYS:
        budget = _to_int(flat.get(key))
        if budget and budget > 0:
            return budget
    return DEFAULT_AUCTION_BUDGET


def parse_leagues(payload: dict) -> list[League]:
    """Parse the users;use_login=1/games/leagues response used to list your leagues."""
    leagues: list[League] = []
    users = unwrap(content(payload), "users")
    for user_entry in collection_items(users):
        user = unwrap(user_entry, "user")
        games = unwrap(user, "games")
        for game_entry in collection_items(games):
            game = unwrap(game_entry, "game")
            league_collection = unwrap(game, "leagues")
            for league_entry in collection_items(league_collection):
                league_node = unwrap(league_entry, "league")
                if league_node is not None:
                    leagues.append(parse_league(league_node))
    return leagues


# --------------------------------------------------------------------------------------
# Teams, draft results, players
# --------------------------------------------------------------------------------------


def parse_teams(payload: dict) -> list[Team]:
    league = unwrap(content(payload), "league")
    teams_node = unwrap(league, "teams")
    teams: list[Team] = []
    for entry in collection_items(teams_node):
        flat = flatten(unwrap(entry, "team") or entry)
        team_key = flat.get("team_key")
        if not team_key:
            continue
        teams.append(
            Team(
                team_key=team_key,
                team_id=str(flat.get("team_id", "")),
                name=flat.get("name", ""),
                is_mine=str(flat.get("is_owned_by_current_login", "0")) == "1",
                draft_position=_to_int(flat.get("draft_position")),
            )
        )
    return teams


def parse_draft_results(payload: dict) -> list[DraftPick]:
    league = unwrap(content(payload), "league")
    results = unwrap(league, "draft_results")
    picks: list[DraftPick] = []
    for entry in collection_items(results):
        flat = flatten(unwrap(entry, "draft_result") or entry)
        pick = _to_int(flat.get("pick"))
        player_key = flat.get("player_key")
        # An un-made pick can appear with an empty player_key; it is not a selection yet.
        if pick is None or not player_key:
            continue
        picks.append(
            DraftPick(
                pick=pick,
                round=_to_int(flat.get("round")) or 0,
                team_key=flat.get("team_key", ""),
                player_key=player_key,
                cost=_to_int(flat.get("cost")),
            )
        )
    return sorted(picks)


def _find_players_collection(node: Any) -> Any:
    """Locate the ``players`` collection inside a node that may bury it a level down.

    A roster wraps it as ``{"0": {"players": {...}}, "coverage_type": "week"}``, which is
    one level deeper than every other endpoint puts it, so a plain flatten misses it.
    """
    direct = unwrap(node, "players")
    if direct is not None:
        return direct
    for item in collection_items(node):
        found = unwrap(item, "players")
        if found is not None:
            return found
    return None


def parse_roster(payload: dict, team_key: str) -> list[KeptPlayer]:
    """Players currently rostered by a team.

    Called before the draft, every player this returns is a keeper.
    """
    team = unwrap(content(payload), "team")
    roster = unwrap(team, "roster")
    players_node = _find_players_collection(roster)

    kept: list[KeptPlayer] = []
    for entry in collection_items(players_node):
        player_node = unwrap(entry, "player") or entry
        flat = flatten(player_node)
        player_key = flat.get("player_key")
        if not player_key:
            continue

        # Yahoo sometimes attaches keeper metadata; take the cost when it is there.
        keeper_flat = flatten(flat.get("is_keeper")) if flat.get("is_keeper") else {}
        kept.append(
            KeptPlayer(
                player_key=player_key,
                team_key=team_key,
                cost=_to_int(keeper_flat.get("cost")),
                source="yahoo",
            )
        )
    return kept


def parse_draft_analysis(node: Any) -> DraftAnalysis:
    flat = flatten(node)
    percent = _to_float(flat.get("percent_drafted"))
    return DraftAnalysis(
        average_pick=_to_float(flat.get("average_pick")),
        average_round=_to_float(flat.get("average_round")),
        average_cost=_to_float(flat.get("average_cost")),
        percent_drafted=percent,
    )


def parse_player(node: Any) -> YahooPlayer | None:
    flat = flatten(node)
    player_key = flat.get("player_key")
    if not player_key:
        return None

    name_node = flat.get("name")
    if isinstance(name_node, dict):
        full_name = name_node.get("full") or name_node.get("ascii_full") or ""
    else:
        full_name = str(name_node or "")

    positions: list[str] = []
    eligible = flat.get("eligible_positions")
    for entry in collection_items(eligible) or (eligible if isinstance(eligible, list) else []):
        position = entry.get("position") if isinstance(entry, dict) else entry
        if position:
            positions.append(str(position))

    analysis_node = flat.get("draft_analysis")
    return YahooPlayer(
        player_key=player_key,
        player_id=str(flat.get("player_id", "")),
        full_name=full_name,
        team_abbr=str(flat.get("editorial_team_abbr", "") or ""),
        display_position=str(flat.get("display_position", "") or ""),
        eligible_positions=tuple(positions),
        bye_week=_parse_bye(flat.get("bye_weeks")),
        status=str(flat.get("status", "") or ""),
        draft_analysis=parse_draft_analysis(analysis_node) if analysis_node else DraftAnalysis(),
    )


def _parse_bye(node: Any) -> int | None:
    if node is None:
        return None
    flat = flatten(node)
    return _to_int(flat.get("week"))


def parse_players(payload: dict) -> list[YahooPlayer]:
    league = unwrap(content(payload), "league")
    # The players collection hangs off a league for league-scoped queries, but off the
    # root for game-scoped ones.
    players_node = unwrap(league, "players") if league is not None else None
    if players_node is None:
        players_node = unwrap(content(payload), "players")

    players: list[YahooPlayer] = []
    for entry in collection_items(players_node):
        player = parse_player(unwrap(entry, "player") or entry)
        if player is not None:
            players.append(player)
    return players
