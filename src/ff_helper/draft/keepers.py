"""Keeper resolution: Yahoo pre-draft rosters, with a CSV escape hatch.

**Yahoo is the primary source and needs no configuration.** Before a draft starts, the only
way a player is sitting on a team is if they were kept, so pre-draft rosters *are* the
keeper list. That works for both snake and auction leagues.

**The CSV exists because keeper rules are the least standardised thing in fantasy.** Some
leagues settle keepers outside Yahoo and enter them late; some assign salaries or forfeited
rounds the API does not expose; some run keeper logic Yahoo has no concept of. When Yahoo's
answer is wrong or missing, a CSV overrides it rather than leaving you to fight the tool.

Getting this wrong is expensive and quiet: an unlisted keeper stays in the pool and gets
recommended to you all draft, and you never find out why the advice felt off. So the count
and source are always surfaced, and a name that cannot be matched is an error, not a skip.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ff_helper.rankings.players import PlayerRegistry, SourceRow
from ff_helper.yahoo.models import KeptPlayer, Team

# Accepted column spellings, so a hand-made sheet does not have to match exactly.
_PLAYER_COLUMNS = ("player", "name", "player_name")
_TEAM_COLUMNS = ("team", "owner", "fantasy_team", "team_name")
_COST_COLUMNS = ("cost", "salary", "price", "auction_cost")
_ROUND_COLUMNS = ("round", "round_cost", "pick")


class KeeperError(ValueError):
    """A keeper could not be resolved. Loud on purpose -- see the module docstring."""


@dataclass
class KeeperSet:
    kept: list[KeptPlayer]
    notes: list[str]

    @property
    def by_team(self) -> dict[str, list[KeptPlayer]]:
        grouped: dict[str, list[KeptPlayer]] = {}
        for keeper in self.kept:
            grouped.setdefault(keeper.team_key, []).append(keeper)
        return grouped

    @property
    def player_keys(self) -> set[str]:
        return {keeper.player_key for keeper in self.kept}

    def count_for(self, team_key: str) -> int:
        return sum(1 for keeper in self.kept if keeper.team_key == team_key)


def _column(row: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for key, value in row.items():
        if key and key.strip().lower().replace(" ", "_") in candidates:
            return (value or "").strip()
    return None


def _to_int(raw: str | None) -> int | None:
    if not raw:
        return None
    cleaned = raw.strip().lstrip("$")
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def load_csv(
    path: Path,
    registry: PlayerRegistry,
    teams: list[Team],
) -> KeeperSet:
    """Read a keepers CSV.

    Expected columns (case-insensitive, extras ignored)::

        player,team,cost,round
        Ja'Marr Chase,Team Ryan,55,2
        Kenneth Walker III,Rival Squad,,4

    ``team`` matches a Yahoo team name or key. ``cost`` matters for auctions, ``round``
    for keeper-snake leagues that charge a pick. Both are optional.
    """
    if not path.exists():
        raise KeeperError(f"Keeper file not found: {path}")

    by_name = {team.name.strip().lower(): team for team in teams}
    by_key = {team.team_key: team for team in teams}

    kept: list[KeptPlayer] = []
    notes: list[str] = []
    unmatched_players: list[str] = []
    unmatched_teams: list[str] = []

    with path.open(newline="", encoding="utf-8-sig") as handle:
        for line_number, row in enumerate(csv.DictReader(handle), start=2):
            name = _column(row, _PLAYER_COLUMNS)
            if not name:
                continue

            team_label = _column(row, _TEAM_COLUMNS) or ""
            team = by_key.get(team_label) or by_name.get(team_label.strip().lower())
            if team is None:
                unmatched_teams.append(f"line {line_number}: {team_label!r}")
                continue

            player = registry.find(SourceRow(name=name, position="", team="", source="csv"))
            if player is None:
                player, _ = registry.find_fuzzy(
                    SourceRow(name=name, position="", team="", source="csv")
                )
            if player is None:
                unmatched_players.append(f"line {line_number}: {name!r}")
                continue

            kept.append(
                KeptPlayer(
                    player_key=player.player_key,
                    team_key=team.team_key,
                    cost=_to_int(_column(row, _COST_COLUMNS)),
                    round=_to_int(_column(row, _ROUND_COLUMNS)),
                    source="csv",
                )
            )

    if unmatched_players or unmatched_teams:
        # Never partially apply. A keeper file that half-loads leaves players in the pool
        # who are not really available, which is exactly the silent failure to avoid.
        problems = []
        if unmatched_players:
            problems.append("players not found: " + "; ".join(unmatched_players))
        if unmatched_teams:
            problems.append("teams not found: " + "; ".join(unmatched_teams))
        raise KeeperError(
            f"{path} could not be fully resolved. " + " | ".join(problems) + "\n"
            "Fix the spellings and re-run; a partly-applied keeper list is worse than none."
        )

    notes.append(f"{len(kept)} keepers loaded from {path.name}")
    return KeeperSet(kept=kept, notes=notes)


def from_yahoo(rostered: list[KeptPlayer], teams: list[Team]) -> KeeperSet:
    """Wrap pre-draft rosters as a keeper set."""
    notes: list[str] = []
    if rostered:
        counts = {}
        for keeper in rostered:
            counts[keeper.team_key] = counts.get(keeper.team_key, 0) + 1
        distinct = sorted(set(counts.values()))
        notes.append(
            f"{len(rostered)} keepers detected from Yahoo rosters "
            f"across {len(counts)} of {len(teams)} teams"
        )
        if len(distinct) > 1:
            # Uneven keeper counts change how many picks each team gets, which the snake
            # pick maths cannot infer. Say so rather than quietly guessing.
            notes.append(
                f"Teams keep different numbers of players ({distinct}); pick-number "
                "predictions may be off until the live feed confirms the real order."
            )
    return KeeperSet(kept=list(rostered), notes=notes)


def resolve(
    rostered: list[KeptPlayer],
    teams: list[Team],
    registry: PlayerRegistry,
    csv_path: Path | None = None,
) -> KeeperSet:
    """Pick the keeper source: an explicit CSV wins, otherwise Yahoo's rosters."""
    if csv_path is not None:
        result = load_csv(csv_path, registry, teams)
        if rostered:
            result.notes.append(
                f"Ignoring {len(rostered)} players found on Yahoo rosters in favour of "
                f"{csv_path.name}"
            )
        return result
    return from_yahoo(rostered, teams)
