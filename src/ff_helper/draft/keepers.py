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
from collections import Counter
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
    def player_keys(self) -> set[str]:
        return {keeper.player_key for keeper in self.kept}


def _column(row: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    """First non-empty value among the accepted spellings of a column.

    Non-empty rather than first-match: a sheet carrying both a blank ``player`` column
    and a filled ``name`` column would otherwise read as a nameless row and be skipped.
    """
    found: str | None = None
    for key, value in row.items():
        if key and key.strip().lower().replace(" ", "_") in candidates:
            cleaned = (value or "").strip()
            if cleaned:
                return cleaned
            found = ""
    return found


def _to_int(raw: str | None) -> int | None:
    """Parse a money/round cell, raising on anything present but unreadable.

    Raising rather than returning None is the whole point: a salary that quietly becomes
    None is spent as $0, so a fat-fingered "5 5" would hand that team an extra $55 of
    apparent budget and inflate every price in the room. Blank stays None -- "no salary
    recorded" is a legitimate answer, "I could not read this" is not.
    """
    if raw is None:
        return None
    cleaned = raw.strip().strip("$").replace(",", "").replace("_", "")
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except (TypeError, ValueError, OverflowError) as exc:
        # OverflowError covers "inf"/"1e400", which float() accepts and int() will not.
        raise ValueError(f"{raw!r} is not a number") from exc


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
    problems: list[str] = []
    data_rows = 0
    missing_player_column = False

    # A sheet saved out of Excel is routinely cp1252, and an accented team name would
    # otherwise abort startup with a raw UnicodeDecodeError traceback minutes before a
    # draft. Every read failure comes back as the same advice-bearing KeeperError.
    try:
        handle = path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise KeeperError(f"Could not open {path}: {exc}") from exc

    with handle:
        reader = csv.DictReader(handle)
        # Streamed, not list()ed, so `reader.line_num` still points at the row in hand.
        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except (UnicodeDecodeError, csv.Error) as exc:
                raise KeeperError(
                    f"{path} is not readable as UTF-8 CSV ({exc}).\n"
                    "Re-save it as CSV UTF-8 (Excel: File > Save As > 'CSV UTF-8')."
                ) from exc

            # reader.line_num, not an enumerate counter: a quoted field may contain
            # newlines, and an error pointing at the wrong line is worse than no line.
            line_number = reader.line_num
            if not any((value or "").strip() for value in row.values()):
                continue
            data_rows += 1

            name = _column(row, _PLAYER_COLUMNS)
            if name is None:
                # No such column at all, as opposed to an empty cell. Naming the header
                # the file is missing beats repeating "no player name" once per row.
                missing_player_column = True
                break
            if not name:
                problems.append(f"line {line_number}: no player name")
                continue

            team_label = _column(row, _TEAM_COLUMNS) or ""
            team = by_key.get(team_label) or by_name.get(team_label.strip().lower())
            if team is None:
                problems.append(f"line {line_number}: team {team_label!r} not found")
                continue

            source_row = SourceRow(name=name, position="", team="", source="csv")
            player = registry.find(source_row) or registry.find_fuzzy(source_row)[0]
            if player is None:
                problems.append(f"line {line_number}: player {name!r} not found")
                continue

            try:
                cost = _to_int(_column(row, _COST_COLUMNS))
                round_cost = _to_int(_column(row, _ROUND_COLUMNS))
            except ValueError as exc:
                problems.append(f"line {line_number}: {exc}")
                continue

            kept.append(
                KeptPlayer(
                    player_key=player.player_key,
                    team_key=team.team_key,
                    cost=cost,
                    round=round_cost,
                    source="csv",
                )
            )

    # One player cannot be kept twice, and two teams cannot keep the same player. Either
    # means the file disagrees with itself, and guessing which row wins is exactly the
    # quiet wrongness this module exists to prevent.
    for player_key, count in Counter(keeper.player_key for keeper in kept).items():
        if count > 1:
            problems.append(f"{player_key} is listed {count} times")

    # A file that resolves to nothing is the total-loss case, and it is worse than the
    # half-load below: with no error, resolve() would hand back an empty keeper set that
    # still overrides a perfectly good Yahoo roster read.
    if missing_player_column or (not problems and not kept):
        raise KeeperError(
            f"{path} produced no keepers ({data_rows} data rows read).\n"
            f"Expected a player column named one of {', '.join(_PLAYER_COLUMNS)} "
            f"and a team column named one of {', '.join(_TEAM_COLUMNS)}."
        )

    if problems:
        # Never partially apply. A keeper file that half-loads leaves players in the pool
        # who are not really available, which is exactly the silent failure to avoid.
        raise KeeperError(
            f"{path} could not be fully resolved:\n  "
            + "\n  ".join(problems)
            + "\nFix these and re-run; a partly-applied keeper list is worse than none."
        )

    notes.append(f"{len(kept)} keepers loaded from {path.name}")
    return KeeperSet(kept=kept, notes=notes)


def from_yahoo(rostered: list[KeptPlayer], teams: list[Team]) -> KeeperSet:
    """Wrap pre-draft rosters as a keeper set."""
    notes: list[str] = []
    if rostered:
        held = Counter(keeper.team_key for keeper in rostered)
        # Count every team, not just the ones holding keepers: a league where ten teams
        # kept two and two kept none is uneven, and tallying only the ten would report it
        # as uniform and skip the warning entirely.
        counts = [held.get(team.team_key, 0) for team in teams] or list(held.values())
        distinct = sorted(set(counts))
        notes.append(
            f"{len(rostered)} keepers detected from Yahoo rosters "
            f"across {len(held)} of {len(teams)} teams"
        )
        if len(distinct) > 1:
            # Uneven keeper counts change how many picks each team gets, which the snake
            # pick maths cannot infer. Say so rather than quietly guessing.
            notes.append(
                f"Teams keep different numbers of players ({distinct}); your own pick "
                "numbers are exact, but the countdown to a rival's turn may be off."
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
