"""On-disk snapshot of one week.

The in-season analog of ``rankings/cache.py``, and separate from it for the reason the
week dimension exists at all: a weekly projection and a season projection are different
claims about a player, and ``rankings/blend.py`` averages point totals across a player's
rows. Put both in one list and a Tuesday projection of 11 points quietly drags a season
projection of 190 toward it. Different files, different loaders, and the mistake becomes
impossible rather than merely discouraged.

**Every week's file is kept.** That is not archival tidiness -- it is the only thing that
makes any weekly model falsifiable. Week 7's projections, written on Tuesday, sit next to
week 7's realized stats, fetched the following Tuesday; the gap between them is the whole
input to calibration, the lineup counterfactual, and the question of whether the news
flags are earning their keep. Overwriting one file to save a few kilobytes would throw
that away permanently.

Versioned independently of the draft snapshot, and with the same hard gate: a mismatched
version returns None and the caller re-fetches, because a missing field reading as None is
exactly how a lineup gets picked from numbers nobody checked.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ff_helper.config import cache_dir
from ff_helper.rankings.players import SourceRow
from ff_helper.rankings.sources.weekly_csv import WeeklyProjection
from ff_helper.yahoo.models import (
    DraftAnalysis,
    League,
    LeagueSettings,
    Matchup,
    RosterEntry,
    RosterSlot,
    Transaction,
    YahooPlayer,
)

# Version 1: the first weekly snapshot format.
WEEK_SNAPSHOT_VERSION = 1


@dataclass
class WeekSnapshot:
    """Everything one week's decisions need, minus anything computed from it."""

    league_key: str
    season: str
    week: int
    fetched_at: float
    # Enough of the league to run without the network. Storing the settings here rather
    # than re-reading them is what makes the no-``--fetch`` path genuinely offline: a
    # digest that had to call Yahoo for scoring would not be reproducible, which is the
    # whole reason the fetch and the read are separate steps.
    league_name: str = ""
    num_teams: int = 0
    my_team_key: str = ""
    team_names: dict[str, str] = field(default_factory=dict)
    settings: LeagueSettings | None = None
    # The player pool as Yahoo sees it this week: status, status_full, percent_owned.
    players: list[YahooPlayer] = field(default_factory=list)
    projections: list[WeeklyProjection] = field(default_factory=list)
    # team_key -> that team's roster, with the slot each player was in.
    rosters: dict[str, list[RosterEntry]] = field(default_factory=dict)
    free_agent_keys: list[str] = field(default_factory=list)
    matchups: list[Matchup] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    # Realized stat lines, filled in by a later fetch once the week has completed. Empty
    # on a snapshot taken before kickoff, which is the normal case.
    actuals: dict[str, dict[str, float]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    version: int = WEEK_SNAPSHOT_VERSION

    @property
    def age_hours(self) -> float:
        return (time.time() - self.fetched_at) / 3600.0

    @property
    def has_actuals(self) -> bool:
        """True once the week has been scored, which is what makes it a backtest record."""
        return bool(self.actuals)

    def league(self) -> League:
        """Rebuild the league object this week was fetched under.

        ``draft_status`` is fixed at "postdraft" because a week snapshot only exists once
        the season is under way -- there is no in-season question that a draft status
        answers, and carrying one would invite something to branch on it.
        """
        return League(
            league_key=self.league_key,
            league_id="",
            name=self.league_name or self.league_key,
            num_teams=self.num_teams,
            season=self.season,
            draft_status="postdraft",
            scoring_type="head",
            settings=self.settings,
            current_week=self.week,
        )

    def roster_for(self, team_key: str) -> list[RosterEntry]:
        return self.rosters.get(team_key, [])

    def team_name(self, team_key: str) -> str:
        return self.team_names.get(team_key, team_key)

    def matchup_for(self, team_key: str) -> Matchup | None:
        """This team's matchup, or None -- a bye week or an unscheduled week has neither."""
        for matchup in self.matchups:
            if matchup.team_key == team_key:
                return matchup
        return None


def default_path(league_key: str, season: str, week: int) -> Path:
    safe = league_key.replace("/", "_")
    return cache_dir() / f"week-{safe}-{season}-w{week:02d}.json"


def save(snapshot: WeekSnapshot, path: Path | None = None) -> Path:
    target = path or default_path(snapshot.league_key, snapshot.season, snapshot.week)
    payload = {
        "version": snapshot.version,
        "league_key": snapshot.league_key,
        "season": snapshot.season,
        "week": snapshot.week,
        "fetched_at": snapshot.fetched_at,
        "notes": snapshot.notes,
        "league_name": snapshot.league_name,
        "num_teams": snapshot.num_teams,
        "my_team_key": snapshot.my_team_key,
        "team_names": snapshot.team_names,
        "settings": _settings_to_dict(snapshot.settings),
        "players": [_player_to_dict(player) for player in snapshot.players],
        "projections": [_projection_to_dict(entry) for entry in snapshot.projections],
        "rosters": {
            team_key: [asdict(entry) for entry in entries]
            for team_key, entries in snapshot.rosters.items()
        },
        "free_agent_keys": list(snapshot.free_agent_keys),
        "matchups": [asdict(matchup) for matchup in snapshot.matchups],
        "transactions": [_transaction_to_dict(entry) for entry in snapshot.transactions],
        "actuals": snapshot.actuals,
    }
    target.write_text(json.dumps(payload))
    return target


def load(league_key: str, season: str, week: int, path: Path | None = None) -> WeekSnapshot | None:
    target = path or default_path(league_key, season, week)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text())
    except json.JSONDecodeError:
        return None

    if payload.get("version") != WEEK_SNAPSHOT_VERSION:
        # Same policy as the draft snapshot: refuse rather than migrate. Re-fetching a
        # week costs a minute; a field that reads as None because the layout moved costs
        # a lineup.
        return None

    return WeekSnapshot(
        league_key=payload["league_key"],
        season=str(payload.get("season", "")),
        week=int(payload["week"]),
        fetched_at=payload["fetched_at"],
        league_name=payload.get("league_name", ""),
        num_teams=int(payload.get("num_teams") or 0),
        my_team_key=payload.get("my_team_key", ""),
        team_names=payload.get("team_names") or {},
        settings=_settings_from_dict(payload.get("settings")),
        players=[_player_from_dict(entry) for entry in payload.get("players", [])],
        projections=[_projection_from_dict(entry) for entry in payload.get("projections", [])],
        rosters={
            team_key: [RosterEntry(**entry) for entry in entries]
            for team_key, entries in (payload.get("rosters") or {}).items()
        },
        free_agent_keys=list(payload.get("free_agent_keys", [])),
        matchups=[Matchup(**entry) for entry in payload.get("matchups", [])],
        transactions=[_transaction_from_dict(entry) for entry in payload.get("transactions", [])],
        actuals=payload.get("actuals") or {},
        notes=payload.get("notes", []),
    )


def stored_weeks(league_key: str, season: str) -> list[int]:
    """Every week already on disk for this league, ascending.

    This is the backtest's index: it answers "how many weeks of evidence do I have" without
    anything needing to track that separately.
    """
    safe = league_key.replace("/", "_")
    prefix = f"week-{safe}-{season}-w"
    weeks: list[int] = []
    for path in cache_dir().glob(f"{prefix}*.json"):
        stem = path.stem[len(prefix) :]
        if stem.isdigit():
            weeks.append(int(stem))
    return sorted(weeks)


def latest(league_key: str, season: str) -> WeekSnapshot | None:
    """The most recent week on disk, or None if this league has no weekly snapshots yet."""
    for week in reversed(stored_weeks(league_key, season)):
        snapshot = load(league_key, season, week)
        if snapshot is not None:
            return snapshot
    return None


# --------------------------------------------------------------------------------------
# Serialization
#
# Deliberately not shared with ``rankings/cache.py``. The two formats are versioned
# separately so either can change without invalidating the other's files, and a shared
# helper would quietly couple those version numbers together.
# --------------------------------------------------------------------------------------


def _settings_to_dict(settings: LeagueSettings | None) -> dict | None:
    if settings is None:
        return None
    data = asdict(settings)
    data["roster_slots"] = [
        {"position": slot.position, "count": slot.count} for slot in settings.roster_slots
    ]
    # JSON object keys are strings, so the stat ids come back as strings and have to be
    # put back to ints on load -- a modifier dict keyed by "11" scores nothing at all.
    data["stat_modifiers"] = {str(k): v for k, v in settings.stat_modifiers.items()}
    return data


def _settings_from_dict(entry: dict | None) -> LeagueSettings | None:
    if not entry:
        return None
    return LeagueSettings(
        roster_slots=tuple(
            RosterSlot(position=slot["position"], count=int(slot["count"]))
            for slot in entry.get("roster_slots", ())
        ),
        stat_modifiers={int(k): float(v) for k, v in (entry.get("stat_modifiers") or {}).items()},
        is_auction=bool(entry.get("is_auction", False)),
        auction_budget=int(entry.get("auction_budget") or 200),
        waiver_type=entry.get("waiver_type", ""),
        waiver_rule=entry.get("waiver_rule", ""),
        uses_faab=bool(entry.get("uses_faab", False)),
        faab_budget=entry.get("faab_budget"),
        trade_end_date=entry.get("trade_end_date", ""),
        playoff_start_week=entry.get("playoff_start_week"),
        num_playoff_teams=entry.get("num_playoff_teams"),
    )


def _player_to_dict(player: YahooPlayer) -> dict:
    data = asdict(player)
    data["eligible_positions"] = list(player.eligible_positions)
    return data


def _player_from_dict(entry: dict) -> YahooPlayer:
    analysis = entry.get("draft_analysis") or {}
    return YahooPlayer(
        player_key=entry["player_key"],
        player_id=entry.get("player_id", ""),
        full_name=entry.get("full_name", ""),
        team_abbr=entry.get("team_abbr", ""),
        display_position=entry.get("display_position", ""),
        eligible_positions=tuple(entry.get("eligible_positions", ())),
        bye_week=entry.get("bye_week"),
        status=entry.get("status", ""),
        draft_analysis=DraftAnalysis(**analysis),
        status_full=entry.get("status_full", ""),
        injury_note=entry.get("injury_note", ""),
        percent_owned=entry.get("percent_owned"),
    )


def _projection_to_dict(entry: WeeklyProjection) -> dict:
    return {
        "row": asdict(entry.row),
        "opponent": entry.opponent,
        "is_home": entry.is_home,
    }


def _projection_from_dict(entry: dict) -> WeeklyProjection:
    return WeeklyProjection(
        row=SourceRow(**entry["row"]),
        opponent=entry.get("opponent", ""),
        is_home=entry.get("is_home"),
    )


def _transaction_to_dict(entry: Transaction) -> dict:
    data = asdict(entry)
    data["added"] = list(entry.added)
    data["dropped"] = list(entry.dropped)
    return data


def _transaction_from_dict(entry: dict) -> Transaction:
    return Transaction(
        transaction_key=entry["transaction_key"],
        type=entry.get("type", ""),
        status=entry.get("status", ""),
        timestamp=entry.get("timestamp"),
        added=tuple(entry.get("added", ())),
        dropped=tuple(entry.get("dropped", ())),
        team_key=entry.get("team_key", ""),
        bid=entry.get("bid"),
        source_type=entry.get("source_type", ""),
    )
