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
    PROJECTION_STAT_IDS,
    DraftAnalysis,
    DraftPick,
    KeptPlayer,
    League,
    LeagueSettings,
    Matchup,
    RosterEntry,
    RosterSlot,
    Team,
    Transaction,
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
        current_week=_to_int(flat.get("current_week")),
        start_week=_to_int(flat.get("start_week")),
        end_week=_to_int(flat.get("end_week")),
        is_finished=str(flat.get("is_finished", "0")) == "1",
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
        waiver_type=str(flat.get("waiver_type", "") or ""),
        waiver_rule=str(flat.get("waiver_rule", "") or ""),
        uses_faab=str(flat.get("uses_faab", "0")) == "1",
        faab_budget=_to_int(flat.get("faab_budget")),
        trade_end_date=str(flat.get("trade_end_date", "") or ""),
        playoff_start_week=_to_int(flat.get("playoff_start_week")),
        num_playoff_teams=_to_int(flat.get("num_playoff_teams")),
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


def _find_players_collection(node: Any, depth: int = 3) -> Any:
    """Locate the ``players`` collection inside a node that may bury it a level down.

    A roster wraps it as ``{"0": {"players": {...}}, "coverage_type": "week"}``, which is
    one level deeper than every other endpoint puts it, so a plain flatten misses it. The
    roster itself also arrives as a list of fragments in some responses, which puts the
    numeric wrapper another level down again -- hence the bounded recursion rather than a
    single hop, since stopping early here returns no keepers at all and says nothing.
    """
    if node is None or depth < 0:
        return None
    direct = unwrap(node, "players")
    if direct is not None:
        return direct
    for item in collection_items(node):
        found = _find_players_collection(item, depth - 1)
        if found is not None:
            return found
    return None


def parse_roster(payload: dict, team_key: str) -> list[KeptPlayer]:
    """Players currently rostered by a team.

    Called before the draft, every player this returns is a keeper.

    In-season, that premise is false and the slot a player sits in starts mattering --
    see ``parse_roster_entries``, which reads the same payload for the other purpose.
    Kept separate on purpose: widening this one would put a keeper-shaped assumption in
    front of every lineup decision.
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
        # Non-keepers carry `is_keeper: {"status": false, "cost": false, "kept": false}`,
        # and `false` would numify to 0 -- a $0 salary is a real auction price, so it must
        # not stand in for "Yahoo told us nothing".
        keeper_flat = flatten(flat.get("is_keeper")) if flat.get("is_keeper") else {}
        raw_cost = keeper_flat.get("cost")
        kept.append(
            KeptPlayer(
                player_key=player_key,
                team_key=team_key,
                cost=None if isinstance(raw_cost, bool) else _to_int(raw_cost),
                source="yahoo",
            )
        )
    return kept


def parse_roster_entries(payload: dict, team_key: str, week: int) -> list[RosterEntry]:
    """A team's roster for one week, with the slot each player is actually in.

    The in-season sibling of ``parse_roster``. Same payload, same collection-finding, but
    it keeps ``selected_position`` -- which is what makes "what did I actually start" a
    knowable fact, and therefore what makes the weekly lineup backtest possible at all.

    ``week`` is passed in rather than read from the payload: it is the week we *asked*
    for, and a roster echoing a different one is a bug we would rather surface upstream
    than silently adopt here.
    """
    team = unwrap(content(payload), "team")
    roster = unwrap(team, "roster")
    players_node = _find_players_collection(roster)

    entries: list[RosterEntry] = []
    for entry in collection_items(players_node):
        player_node = unwrap(entry, "player") or entry
        flat = flatten(player_node)
        player_key = flat.get("player_key")
        if not player_key:
            continue
        entries.append(
            RosterEntry(
                player_key=player_key,
                team_key=team_key,
                week=week,
                selected_position=_parse_selected_position(flat.get("selected_position")),
            )
        )
    return entries


def _parse_selected_position(node: Any) -> str:
    """Yahoo wraps the slot as ``[{"coverage_type": ...}, {"position": "W/R/T"}]``.

    An empty string means Yahoo told us nothing, which is different from "bench" -- a
    caller treating the two the same would score an unknown slot as a deliberate sit.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    return str(flatten(node).get("position", "") or "")


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
        status_full=str(flat.get("status_full", "") or ""),
        injury_note=str(flat.get("injury_note", "") or ""),
        percent_owned=_parse_percent_owned(flat.get("percent_owned")),
    )


def _parse_percent_owned(node: Any) -> float | None:
    """Ownership arrives as ``{"coverage_type": "week", "value": 31, "delta": "+5"}``.

    Returns None rather than 0.0 when absent: "nobody rosters him" and "we did not ask
    for ownership" are different facts, and only one of them is a waiver signal.
    """
    if node is None:
        return None
    if isinstance(node, int | float):
        return float(node)
    return _to_float(flatten(node).get("value"))


def _parse_bye(node: Any) -> int | None:
    if node is None:
        return None
    flat = flatten(node)
    return _to_int(flat.get("week"))


# --------------------------------------------------------------------------------------
# In-season: scoreboard and transactions
# --------------------------------------------------------------------------------------


def parse_scoreboard(payload: dict) -> list[Matchup]:
    """One week's matchups, expanded to one ``Matchup`` per team.

    A Yahoo matchup carries exactly two teams, so each pairing yields two entries facing
    opposite directions. See ``Matchup`` for why that redundancy is deliberate.
    """
    league = unwrap(content(payload), "league")
    scoreboard = unwrap(league, "scoreboard")
    matchups_node = unwrap(scoreboard, "matchups")
    if matchups_node is None:
        # Some responses nest the matchups collection under a numeric wrapper, the same
        # shape ``_find_players_collection`` exists to survive on rosters.
        for item in collection_items(scoreboard):
            matchups_node = unwrap(item, "matchups")
            if matchups_node is not None:
                break

    results: list[Matchup] = []
    for entry in collection_items(matchups_node):
        flat = flatten(unwrap(entry, "matchup") or entry)
        week = _to_int(flat.get("week")) or 0
        status = str(flat.get("status", "") or "")
        is_playoffs = str(flat.get("is_playoffs", "0")) == "1"

        sides = _matchup_sides(_find_teams_collection(flat))
        if len(sides) != 2:
            # A bye week or a malformed matchup. Skipped rather than guessed at: half a
            # matchup would give one team an opponent that does not exist.
            continue
        for mine, theirs in (sides, sides[::-1]):
            results.append(
                Matchup(
                    week=week,
                    team_key=mine[0],
                    opponent_key=theirs[0],
                    points=mine[1],
                    opponent_points=theirs[1],
                    projected_points=mine[2],
                    opponent_projected=theirs[2],
                    is_playoffs=is_playoffs,
                    status=status,
                )
            )
    return results


def _find_teams_collection(flat: dict[str, Any]) -> Any:
    """A matchup's teams sit either directly on it or under a numeric wrapper.

    The same one-level-deeper problem ``_find_players_collection`` exists for, and it
    varies by endpoint the same way -- so look in both places rather than picking one and
    returning an empty matchup when Yahoo picks the other.
    """
    direct = flat.get("teams")
    if direct is not None:
        return direct
    for key, value in flat.items():
        if isinstance(key, str) and key.isdigit():
            found = unwrap(value, "teams")
            if found is not None:
                return found
    return None


def _matchup_sides(teams_node: Any) -> list[tuple[str, float | None, float | None]]:
    """(team_key, points, projected) for each team in a matchup."""
    sides: list[tuple[str, float | None, float | None]] = []
    for entry in collection_items(teams_node):
        flat = flatten(unwrap(entry, "team") or entry)
        team_key = flat.get("team_key")
        if not team_key:
            continue
        sides.append(
            (
                team_key,
                _to_float(flatten(flat.get("team_points")).get("total")),
                _to_float(flatten(flat.get("team_projected_points")).get("total")),
            )
        )
    return sides


def parse_transactions(payload: dict) -> list[Transaction]:
    """Completed adds, drops and trades -- what the room actually did.

    The winning FAAB bid is the number worth having: it is the in-season analog of an
    auction sale price, and the only honest evidence of what this specific league pays.
    """
    league = unwrap(content(payload), "league")
    node = unwrap(league, "transactions")

    results: list[Transaction] = []
    for entry in collection_items(node):
        flat = flatten(unwrap(entry, "transaction") or entry)
        transaction_key = flat.get("transaction_key")
        if not transaction_key:
            continue

        added, dropped, team_key, source_type = _transaction_players(flat.get("players"))
        results.append(
            Transaction(
                transaction_key=str(transaction_key),
                type=str(flat.get("type", "") or ""),
                status=str(flat.get("status", "") or ""),
                timestamp=_to_float(flat.get("timestamp")),
                added=added,
                dropped=dropped,
                team_key=team_key,
                bid=_to_int(flat.get("faab_bid")),
                source_type=source_type,
            )
        )
    return results


def _transaction_players(node: Any) -> tuple[tuple[str, ...], tuple[str, ...], str, str]:
    """Split a transaction's players into what came in and what went out."""
    added: list[str] = []
    dropped: list[str] = []
    team_key = ""
    source_type = ""

    for entry in collection_items(node):
        flat = flatten(unwrap(entry, "player") or entry)
        player_key = flat.get("player_key")
        if not player_key:
            continue
        # ``transaction_data`` is a bare dict on some responses and a one-element list on
        # others -- the same fragment-vs-object split the module header describes.
        data = flatten(flat.get("transaction_data"))
        movement = str(data.get("type", "") or "")
        if movement == "add":
            added.append(player_key)
            team_key = team_key or str(data.get("destination_team_key", "") or "")
            source_type = source_type or str(data.get("source_type", "") or "")
        elif movement == "drop":
            dropped.append(player_key)
            team_key = team_key or str(data.get("source_team_key", "") or "")

    return tuple(added), tuple(dropped), team_key, source_type


# Yahoo stat id -> the projection column name the rest of the app speaks. Realized stats
# have to land in the same vocabulary as projected ones or they cannot be compared, which
# is the entire point of fetching them.
_STAT_COLUMN_BY_ID: dict[int, str] = {
    stat_id: column for column, stat_id in PROJECTION_STAT_IDS.items()
}


def parse_player_stats(payload: dict) -> dict[str, dict[str, float]]:
    """Realized stat lines by player key, in the same column vocabulary as projections.

    Stat ids the app cannot score are dropped rather than carried: they would sit in the
    dict looking like data while ``engine.scoring`` ignored them, and a stat line that is
    *partly* understood is the kind of thing that reads as complete.
    """
    league = unwrap(content(payload), "league")
    players_node = unwrap(league, "players") if league is not None else None
    if players_node is None:
        players_node = unwrap(content(payload), "players")

    results: dict[str, dict[str, float]] = {}
    for entry in collection_items(players_node):
        flat = flatten(unwrap(entry, "player") or entry)
        player_key = flat.get("player_key")
        if not player_key:
            continue
        stats_node = unwrap(flat.get("player_stats"), "stats")
        line: dict[str, float] = {}
        for stat_entry in collection_items(stats_node):
            stat_flat = flatten(unwrap(stat_entry, "stat") or stat_entry)
            column = _STAT_COLUMN_BY_ID.get(_to_int(stat_flat.get("stat_id")) or -1)
            value = _to_float(stat_flat.get("value"))
            if column is not None and value is not None:
                line[column] = value
        if line:
            results[player_key] = line
    return results


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
