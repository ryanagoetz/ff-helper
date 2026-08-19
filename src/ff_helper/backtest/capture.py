"""A completed draft as a file.

The Yahoo API only matters once: the moment a draft's results are fetched. Everything a
backtest needs after that -- the league's shape, the team order, every pick -- fits in a
small JSON file, and keeping it as one means calibration can run on the couch, in CI, and
on drafts from leagues you no longer have API access to.

Records are written anonymized by default (team and league names replaced) so they can be
committed to a public repository; player keys are kept, because joining against a ranking
snapshot is the whole point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from ff_helper.draft.state import DraftState
from ff_helper.yahoo.models import (
    DraftPick,
    KeptPlayer,
    League,
    LeagueSettings,
    RosterSlot,
    Team,
)

RECORD_VERSION = 1


@dataclass(frozen=True)
class DraftRecord:
    """Everything about one completed draft, minus the ranking snapshot.

    ``league.settings`` is always present -- a record without settings cannot be
    replayed, so ``record_from_live`` refuses to build one.
    """

    league: League
    teams: tuple[Team, ...]
    picks: tuple[DraftPick, ...]
    # Players kept before the draft. Without them a keeper league round-trips into a
    # keeper-free board: wrong pick counts, invented rounds, and kept studs sitting
    # "available" all draft -- which shows up as impossible survivals in calibration.
    keepers: tuple[KeptPlayer, ...] = ()
    # Which ranking snapshot this draft should be evaluated against; a league key for
    # ``cache.load`` or None to fall back to the record's own league key.
    snapshot_ref: str | None = None
    version: int = RECORD_VERSION

    @property
    def my_team(self) -> Team | None:
        return next((team for team in self.teams if team.is_mine), None)

    @property
    def is_auction(self) -> bool:
        return bool(self.league.settings and self.league.settings.is_auction)


def record_from_live(
    league: League,
    teams: list[Team],
    picks: list[DraftPick],
    *,
    keepers: list[KeptPlayer] | None = None,
    snapshot_ref: str | None = None,
) -> DraftRecord:
    """Freeze a just-fetched draft into a record."""
    if league.settings is None:
        raise ValueError("Cannot record a draft without league settings.")
    return DraftRecord(
        league=league,
        teams=tuple(teams),
        picks=tuple(sorted(picks)),
        keepers=tuple(keepers or ()),
        snapshot_ref=snapshot_ref,
    )


def build_state(record: DraftRecord) -> tuple[League, DraftState]:
    """An empty draft board for this record's league, ready to replay picks into."""
    state = DraftState(league=record.league, teams=list(record.teams))
    if record.keepers:
        # Attach before any consumer computes pick math; Assistant.build re-applies
        # once it knows the true roster size, exactly as the live path does.
        state.apply_keepers(list(record.keepers))
    return record.league, state


def save_record(record: DraftRecord, path: Path, *, anonymize: bool = True) -> Path:
    """Write a record to disk. Anonymized unless told otherwise: these files are meant
    to be committable, and team names identify real people."""
    if anonymize:
        record = _anonymized(record)
    settings = record.league.settings
    assert settings is not None  # enforced at construction
    payload = {
        "version": record.version,
        "snapshot_ref": record.snapshot_ref,
        "league": {
            "league_key": record.league.league_key,
            "league_id": record.league.league_id,
            "name": record.league.name,
            "num_teams": record.league.num_teams,
            "season": record.league.season,
            "draft_status": record.league.draft_status,
            "scoring_type": record.league.scoring_type,
        },
        "settings": {
            "roster_slots": [[slot.position, slot.count] for slot in settings.roster_slots],
            # JSON keys are strings; stat IDs are restored to ints on load.
            "stat_modifiers": {str(k): v for k, v in settings.stat_modifiers.items()},
            "is_auction": settings.is_auction,
            "auction_budget": settings.auction_budget,
        },
        "teams": [
            {
                "team_key": team.team_key,
                "team_id": team.team_id,
                "name": team.name,
                "is_mine": team.is_mine,
                "draft_position": team.draft_position,
            }
            for team in record.teams
        ],
        "picks": [
            {
                "pick": pick.pick,
                "round": pick.round,
                "team_key": pick.team_key,
                "player_key": pick.player_key,
                "cost": pick.cost,
            }
            for pick in record.picks
        ],
        "keepers": [
            {
                "player_key": keeper.player_key,
                "team_key": keeper.team_key,
                "cost": keeper.cost,
                "round": keeper.round,
                "source": keeper.source,
            }
            for keeper in record.keepers
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1))
    return path


def load_record(path: Path) -> DraftRecord:
    payload = json.loads(Path(path).read_text())
    version = payload.get("version")
    if version != RECORD_VERSION:
        raise ValueError(f"Draft record {path} has version {version}; expected {RECORD_VERSION}.")

    raw_settings = payload["settings"]
    settings = LeagueSettings(
        roster_slots=tuple(
            RosterSlot(position, count) for position, count in raw_settings["roster_slots"]
        ),
        stat_modifiers={int(k): v for k, v in raw_settings["stat_modifiers"].items()},
        is_auction=raw_settings["is_auction"],
        auction_budget=raw_settings.get("auction_budget", 200),
    )
    raw_league = payload["league"]
    league = League(
        league_key=raw_league["league_key"],
        league_id=raw_league["league_id"],
        name=raw_league["name"],
        num_teams=raw_league["num_teams"],
        season=raw_league["season"],
        draft_status=raw_league["draft_status"],
        scoring_type=raw_league["scoring_type"],
        settings=settings,
    )
    teams = tuple(
        Team(
            team_key=entry["team_key"],
            team_id=entry["team_id"],
            name=entry["name"],
            is_mine=entry["is_mine"],
            draft_position=entry.get("draft_position"),
        )
        for entry in payload["teams"]
    )
    picks = tuple(
        DraftPick(
            pick=entry["pick"],
            round=entry["round"],
            team_key=entry["team_key"],
            player_key=entry["player_key"],
            cost=entry.get("cost"),
        )
        for entry in payload["picks"]
    )
    keepers = tuple(
        KeptPlayer(
            player_key=entry["player_key"],
            team_key=entry["team_key"],
            cost=entry.get("cost"),
            round=entry.get("round"),
            source=entry.get("source", "yahoo"),
        )
        # Older records predate the field; a missing list means "no keepers", which is
        # exactly what those records meant.
        for entry in payload.get("keepers", [])
    )
    return DraftRecord(
        league=league,
        teams=teams,
        picks=tuple(sorted(picks)),
        keepers=keepers,
        snapshot_ref=payload.get("snapshot_ref"),
        version=version,
    )


def _anonymized(record: DraftRecord) -> DraftRecord:
    league = replace(record.league, name=f"League {record.league.league_id}")
    teams = tuple(
        replace(team, name=f"Team {team.draft_position or team.team_id}")
        for team in record.teams
    )
    return replace(record, league=league, teams=teams)
