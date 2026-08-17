"""Per-stat season projections from a CSV export.

This exists because FantasyPros put full projections behind a registration fence: the
public page serves ten rows per position and a "create a free account to unlock" banner.
Ten running backs is not a projection set -- replacement level in a 12-team league sits
around RB24, so the whole VOR layer has nothing to stand on.

A CSV is the right shape for the replacement. Sites that sell projections (4for4, and
most of its competitors) let a subscriber export them, and an export you download once
before draft day involves no credentials in this app, no scraper to rot, and no terms to
argue about -- it is your data, from your subscription.

**Per-stat columns are strongly preferred over a points total.** The app re-scores stats
under your league's own modifiers, which is what makes the value numbers yours rather than
inherited from whatever scoring the exporter assumed. A points-only file still works and
is better than nothing, but it silently imports someone else's scoring assumptions. If
your provider lets you set league scoring before exporting, do that.

Unlike the keeper CSV, an unmatched name here is *not* a hard error. A keeper that fails
to load leaves a player in the pool who is not really available, which corrupts the draft;
a projection that fails to load drops one player from the recommendations, which the
coverage report in ``scripts/fetch_rankings.py`` already names out loud.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ff_helper.rankings.players import SourceRow, normalize_position, normalize_team

SOURCE = "csv"

# A file this short is a teaser, a header-only export, or the wrong file entirely.
MIN_ROWS = 20

_PLAYER_COLUMNS = ("player", "name", "player_name", "full_name")
_POSITION_COLUMNS = ("pos", "position", "player_position")
_TEAM_COLUMNS = ("team", "tm", "nfl_team", "player_team")
_POINTS_COLUMNS = ("fpts", "points", "proj_points", "projected_points", "fantasy_points")

# Maps onto PROJECTION_STAT_IDS in yahoo/models.py -- those keys are what the scoring
# engine reads, so anything not spelled that way here is simply never scored.
_STAT_COLUMNS: dict[str, tuple[str, ...]] = {
    "pass_yds": ("pass_yds", "passing_yards", "pass_yards", "py", "pyds"),
    "pass_td": ("pass_td", "passing_tds", "pass_tds", "ptd", "ptds"),
    "int": ("int", "ints", "interceptions", "picks"),
    "rush_yds": ("rush_yds", "rushing_yards", "rush_yards", "ry", "ryds"),
    "rush_td": ("rush_td", "rushing_tds", "rush_tds", "rtd", "rtds"),
    "rec": ("rec", "receptions", "catches", "recs"),
    "rec_yds": ("rec_yds", "receiving_yards", "rec_yards", "recy", "reyds"),
    "rec_td": ("rec_td", "receiving_tds", "rec_tds", "retd", "rectd"),
    "ret_td": ("ret_td", "return_tds", "ret_tds", "kr_td", "pr_td"),
    "two_pt": ("two_pt", "2pt", "two_point", "2pc", "twopt"),
    "fum_lost": ("fum_lost", "fumbles_lost", "fl", "fum", "fumbles"),
}


class ProjectionsError(ValueError):
    """The file could not be read as projections. Loud on purpose."""


def _normalize_header(raw: str | None) -> str:
    """Fold a header into a comparable key: lowercase, no spaces, dots, or slashes."""
    cleaned = (raw or "").strip().lower()
    for character in (" ", "-", ".", "/", "\\"):
        cleaned = cleaned.replace(character, "_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")


def _column(row: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    """First non-empty value among the accepted spellings of a column."""
    found: str | None = None
    for key, value in row.items():
        if key and _normalize_header(key) in candidates:
            cleaned = (value or "").strip()
            if cleaned:
                return cleaned
            found = ""
    return found


def _to_float(raw: str | None) -> float | None:
    """Parse a stat cell, raising on anything present but unreadable.

    Raising matters more than it looks. A projection cell that quietly becomes None drops
    that stat from the scoring sum, so a mis-typed "1,2 34" would not fail -- it would
    just make the player worth fewer points than the file says, for no visible reason.
    """
    if raw is None:
        return None
    cleaned = raw.strip().replace(",", "").replace("$", "").replace("%", "")
    if not cleaned or cleaned in {"-", "--", "N/A", "n/a"}:
        return None
    try:
        return float(cleaned)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{raw!r} is not a number") from exc


def load(path: Path, *, source: str = SOURCE) -> list[SourceRow]:
    """Read season projections from a CSV export.

    Expected columns (case-insensitive, order-independent, extras ignored)::

        player,pos,team,pass_yds,pass_td,int,rush_yds,rush_td,rec,rec_yds,rec_td,fum_lost
        Ja'Marr Chase,WR,CIN,0,0,0,4,28,1,112,1408,12,1

    Only ``player`` is required. A ``fpts`` column is used when no per-stat columns are
    present; when both exist the stats win, because they get re-scored under your league.
    """
    if not path.exists():
        raise ProjectionsError(f"Projections file not found: {path}")

    try:
        handle = path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ProjectionsError(f"Could not open {path}: {exc}") from exc

    rows: list[SourceRow] = []
    problems: list[str] = []
    data_rows = 0
    missing_player_column = False

    with handle:
        reader = csv.DictReader(handle)
        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except (UnicodeDecodeError, csv.Error) as exc:
                raise ProjectionsError(
                    f"{path} is not readable as UTF-8 CSV ({exc}).\n"
                    "Re-save it as CSV UTF-8 (Excel: File > Save As > 'CSV UTF-8')."
                ) from exc

            line_number = reader.line_num
            if not any((value or "").strip() for value in row.values()):
                continue
            data_rows += 1

            name = _column(row, _PLAYER_COLUMNS)
            if name is None:
                missing_player_column = True
                break
            if not name:
                problems.append(f"line {line_number}: no player name")
                continue

            try:
                stats: dict[str, float] = {}
                for key, candidates in _STAT_COLUMNS.items():
                    value = _to_float(_column(row, candidates))
                    if value is not None:
                        stats[key] = value
                points = _to_float(_column(row, _POINTS_COLUMNS))
            except ValueError as exc:
                problems.append(f"line {line_number} ({name}): {exc}")
                continue

            if not stats and points is None:
                problems.append(f"line {line_number} ({name}): no stats and no points")
                continue

            rows.append(
                SourceRow(
                    name=name,
                    position=normalize_position(_column(row, _POSITION_COLUMNS) or ""),
                    team=normalize_team(_column(row, _TEAM_COLUMNS) or ""),
                    source=source,
                    projected_points=points,
                    stats=stats,
                )
            )

    if missing_player_column or (not rows and not problems):
        raise ProjectionsError(
            f"{path} produced no projections ({data_rows} data rows read).\n"
            f"Expected a player column named one of {', '.join(_PLAYER_COLUMNS)}, "
            f"plus either per-stat columns or one of {', '.join(_POINTS_COLUMNS)}."
        )

    if problems:
        raise ProjectionsError(
            f"{path} could not be fully read:\n  "
            + "\n  ".join(problems[:20])
            + (f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else "")
        )

    if len(rows) < MIN_ROWS:
        # The failure this catches is a partial export -- the same shape of problem that
        # made FantasyPros unusable. Replacement level is derived from the depth of the
        # pool, so a short file does not degrade the model, it inverts it: with 15 players
        # the last startable running back looks like a replacement-level one.
        raise ProjectionsError(
            f"{path} has only {len(rows)} projections, which is too few to derive "
            f"replacement level from (expected at least {MIN_ROWS}, and realistically "
            "200+ for a full draft board). Check the export covers every position and "
            "was not truncated."
        )

    return rows
