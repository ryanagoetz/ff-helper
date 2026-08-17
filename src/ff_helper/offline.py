"""Run a draft with no Yahoo API access at all.

Yahoo gated the Fantasy Sports API behind an approval process (see the README), and
approval does not arrive on a schedule that cares about your draft date. This module is
the answer to "my draft is Saturday and I am still waiting": you describe the league in a
YAML file, the player pool is derived from your projections export, and picks are entered
by hand. Everything above that -- scoring, replacement level, VOR, VONA, auction par
values, inflation -- is the same code that runs online, because none of it was ever
Yahoo-specific.

What you give up, stated plainly:

* **Yahoo's own ADP**, normally weighted 0.65 because it describes drafts on the platform
  you are actually drafting on. Offline you are left with your export's ADP and FFC's.
* **Live draft sync.** No poller, so every pick is typed in. The manual entry path already
  exists for feed stalls; here it is the only path.
* **Automatic keepers.** Pre-draft rosters come from the API, so offline they come from
  the keeper CSV instead.
* **Market prices in auctions**, unless your export carries them. ``engine/auction.py``
  falls back to par value, so you still get what a player is *worth*, just not whether
  the room is likely to overpay for him.

Everything else is intact, including the parts that actually make this app worth running.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ff_helper.rankings.players import SourceRow, normalize_position, normalize_team
from ff_helper.yahoo.models import (
    PROJECTION_STAT_IDS,
    League,
    LeagueSettings,
    RosterSlot,
    Team,
    YahooPlayer,
)

# Offline keys are namespaced so a snapshot built offline can never be confused with one
# built from the API, and so cache files do not collide.
KEY_PREFIX = "offline"


class OfflineConfigError(ValueError):
    """The league description could not be read. Loud on purpose."""


@dataclass
class OfflineLeague:
    league: League
    teams: list[Team]
    notes: list[str]


def _require(config: dict, key: str, path: Path):
    if key not in config or config[key] in (None, ""):
        raise OfflineConfigError(f"{path}: missing required key {key!r}")
    return config[key]


def load_config(path: Path) -> OfflineLeague:
    """Read a league description written by hand.

    The scoring keys are the same names the projections CSV uses and the scoring engine
    reads, so there is one vocabulary to learn rather than three.
    """
    if not path.exists():
        raise OfflineConfigError(f"League config not found: {path}")

    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OfflineConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(config, dict):
        raise OfflineConfigError(f"{path} should contain a mapping of settings.")

    name = str(_require(config, "name", path))
    num_teams = int(_require(config, "num_teams", path))
    draft_type = str(config.get("draft_type", "snake")).strip().lower()
    if draft_type not in {"snake", "auction"}:
        raise OfflineConfigError(
            f"{path}: draft_type must be 'snake' or 'auction', not {draft_type!r}"
        )

    roster = _require(config, "roster", path)
    if not isinstance(roster, dict) or not roster:
        raise OfflineConfigError(f"{path}: 'roster' must map slot names to counts.")
    slots = tuple(
        RosterSlot(position=str(position).strip().upper(), count=int(count))
        for position, count in roster.items()
    )

    scoring = _require(config, "scoring", path)
    if not isinstance(scoring, dict) or not scoring:
        raise OfflineConfigError(f"{path}: 'scoring' must map stat names to values.")

    modifiers: dict[int, float] = {}
    unknown: list[str] = []
    for stat, value in scoring.items():
        key = str(stat).strip().lower()
        stat_id = PROJECTION_STAT_IDS.get(key)
        if stat_id is None:
            unknown.append(key)
            continue
        modifiers[stat_id] = float(value)

    if not modifiers:
        raise OfflineConfigError(
            f"{path}: none of the scoring keys are scoreable. "
            f"Valid keys: {', '.join(sorted(PROJECTION_STAT_IDS))}."
        )

    notes: list[str] = []
    if unknown:
        # Not fatal -- a league scoring return yards or first downs is still mostly
        # scoreable -- but silence here would hide points the board will never award.
        notes.append(
            f"{path.name}: {len(unknown)} scoring categories cannot be scored and are "
            f"ignored ({', '.join(sorted(unknown))}). Players earning most of their "
            "value there will be undervalued."
        )

    settings = LeagueSettings(
        roster_slots=slots,
        stat_modifiers=modifiers,
        is_auction=draft_type == "auction",
        auction_budget=int(config.get("auction_budget", 200)),
    )

    league_id = str(config.get("league_id", "offline"))
    league = League(
        league_key=f"{KEY_PREFIX}.l.{league_id}",
        league_id=league_id,
        name=name,
        num_teams=num_teams,
        season=str(config.get("season", "")),
        draft_status="predraft",
        scoring_type=str(config.get("scoring_type", "head")),
        settings=settings,
    )

    teams = _build_teams(config, league, num_teams, path)
    return OfflineLeague(league=league, teams=teams, notes=notes)


def _build_teams(config: dict, league: League, num_teams: int, path: Path) -> list[Team]:
    """Build the team list, and work out which one is yours.

    Knowing which team is yours is not cosmetic: roster needs, budget, and max bid are all
    computed against it, so a config that does not say is rejected rather than guessed.
    """
    names = config.get("teams") or [f"Team {index + 1}" for index in range(num_teams)]
    if len(names) != num_teams:
        raise OfflineConfigError(
            f"{path}: 'teams' lists {len(names)} names but num_teams is {num_teams}."
        )

    my_team = config.get("my_team")
    if my_team in (None, ""):
        raise OfflineConfigError(
            f"{path}: 'my_team' is required -- roster needs, budget, and max bid are all "
            "computed against your team, and guessing would silently misvalue every "
            "recommendation."
        )

    my_label = str(my_team).strip().lower()
    matched = [str(n).strip().lower() == my_label for n in names]
    if not any(matched):
        raise OfflineConfigError(
            f"{path}: my_team {my_team!r} is not in the teams list ({', '.join(map(str, names))})."
        )

    draft_position = config.get("draft_position")
    teams: list[Team] = []
    for index, label in enumerate(names):
        is_mine = matched[index]
        teams.append(
            Team(
                team_key=f"{league.league_key}.t.{index + 1}",
                team_id=str(index + 1),
                name=str(label),
                is_mine=is_mine,
                draft_position=int(draft_position) if is_mine and draft_position else None,
            )
        )
    return teams


def players_from_rows(rows: list[SourceRow], league_key: str) -> list[YahooPlayer]:
    """Build a player pool from projection rows, standing in for the Yahoo player pool.

    Online, Yahoo is the canonical registry and every other source is crosswalked onto its
    player keys. Offline there is no such registry, so the projections become it. That is
    the right choice by elimination: it is the only source carrying every position, and a
    player absent from it has no projection and could never have been valued anyway.

    Ordering matters. ``fetch_rankings`` measures coverage against the first 200 players,
    and ``web/app.py`` leans on the pool being roughly board-ordered, so rows are sorted by
    ADP where present and projected points otherwise.
    """
    ranked = sorted(rows, key=_board_order)

    players: list[YahooPlayer] = []
    seen: set[str] = set()
    for index, row in enumerate(ranked):
        player_id = _player_id(row, index)
        key = f"{league_key}.p.{player_id}"
        if key in seen:
            continue
        seen.add(key)

        position = normalize_position(row.position)
        players.append(
            YahooPlayer(
                player_key=key,
                player_id=player_id,
                full_name=row.name,
                team_abbr=normalize_team(row.team),
                display_position=position,
                eligible_positions=(position,) if position else (),
                # draft_analysis is left empty on purpose. ``yahoo_adp.from_players``
                # turns it into a source row weighted 0.65 as Yahoo's own ADP -- the
                # heaviest weight in the blend, justified online because Yahoo describes
                # drafts on the platform you are drafting on. Offline it would be this
                # same CSV's ADP wearing Yahoo's hat, counted a second time at more than
                # triple the weight of its own row. The export's ADP already reaches the
                # blend as the csv source; there is no Yahoo here to speak for.
            )
        )
    return players


def supplement_positions(
    players: list[YahooPlayer],
    rows: list[SourceRow],
    league_key: str,
    positions: frozenset[str] = frozenset({"DEF"}),
) -> tuple[list[YahooPlayer], list[str]]:
    """Add players at positions the projections do not cover at all.

    Projection exports do not carry team defenses -- there is no per-stat line to export,
    since DST scoring is sacks, turnovers, and points-allowed tiers rather than yardage.
    Left alone, that means a league with a DEF slot has a roster spot the app can never
    fill, and it would never say why.

    So positions absent from the pool entirely are seeded from the other sources, which do
    list them. They arrive with no projection and get ranked by consensus, which is what
    the online path does with defenses anyway.
    """
    present = {player.primary_position for player in players}
    wanted = {position for position in positions if position not in present}
    if not wanted:
        return players, []

    known_names = {player.full_name.strip().lower() for player in players}
    added: list[YahooPlayer] = []
    seen: set[str] = set()

    for row in rows:
        position = normalize_position(row.position)
        if position not in wanted:
            continue
        # Defenses are identified by team, not name: FFC calls one "Seattle Defense" and
        # FantasyPros calls it "Seattle Seahawks", and deduping on the name would put
        # both in the pool as separate draftable defenses.
        team = normalize_team(row.team)
        label = f"{position}:{team}" if team else row.name.strip().lower()
        if row.name.strip().lower() in known_names or label in seen:
            continue
        seen.add(label)
        player_id = _player_id(row, len(players) + len(added))
        added.append(
            YahooPlayer(
                player_key=f"{league_key}.p.{player_id}",
                player_id=player_id,
                full_name=row.name,
                team_abbr=normalize_team(row.team),
                display_position=position,
                eligible_positions=(position,),
                # Empty for the same reason as in players_from_rows: the source row this
                # was built from already carries its own ADP into the blend.
            )
        )

    notes = []
    if added:
        notes.append(
            f"{len(added)} players added at {', '.join(sorted(wanted))} from other "
            "sources, because the projections export does not cover those positions. "
            "They are ranked by consensus rather than projected."
        )
    return players + added, notes


def _board_order(row: SourceRow) -> tuple[int, float]:
    if row.adp is not None:
        return (0, row.adp)
    if row.projected_points is not None:
        return (1, -row.projected_points)
    return (2, 0.0)


def _player_id(row: SourceRow, index: int) -> str:
    """A stable id from the name, falling back to position for the rare collision."""
    slug = "".join(character for character in row.name.lower() if character.isalnum())
    return slug or f"player{index}"
