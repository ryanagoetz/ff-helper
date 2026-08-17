"""Per-stat season projections from a CSV export.

This exists because FantasyPros put full projections behind a registration fence: the
public page serves ten rows per position and a "create a free account to unlock" banner.
Ten running backs is not a projection set -- replacement level in a 12-team league sits
around RB24, so the whole VOR layer has nothing to stand on.

A CSV is the right shape for the replacement. Sites that sell projections (4for4, and
most of its competitors) let a subscriber export them, and an export you download once
before draft day involves no credentials in this app, no scraper to rot, and no terms to
argue about -- it is your data, from your subscription.

**Per-stat columns are required, not preferred.** The app re-scores stats under your
league's own modifiers, which is what makes the value numbers yours rather than inherited
from whatever scoring the exporter assumed -- and ``rankings/blend.py`` discards a
source's own point total for exactly that reason. So a rankings table carrying only a
points column contributes nothing at all: every player falls through to interpolation,
every position reports "no stat projections available", and the board comes back 0.0 and
ranked by ADP. That is rejected here rather than allowed to look like data.

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
_POINTS_COLUMNS = (
    "fpts",
    "points",
    "proj_points",
    "projected_points",
    "fantasy_points",
    # 4for4 spells the total this way on its rankings export.
    "ff_pts",
    "ffpts",
    "fpts_proj",
    "proj_fpts",
)

# ADP matters far more offline than online: with no Yahoo API there is no Yahoo ADP, and
# this becomes one of only two market signals left (the other being FFC).
_ADP_COLUMNS = ("adp", "avg_pick", "average_pick", "adp_average", "adp_avg")

# The auction analog of ADP. Rarely present in a projections export, and its absence is
# survivable -- engine/auction.py falls back to par value when no market price exists.
_COST_COLUMNS = ("auction_value", "auction_cost", "avg_cost", "average_cost", "salary", "aav")

# Some exports carry no plain position column, only a combined positional rank such as
# 4for4's "Position-Rank" holding "RB-01". The prefix is the position.
_POSITION_RANK_COLUMNS = ("position_rank", "pos_rank", "positional_rank", "pos_rk")

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


def resolve_path(
    data_dir: Path, league_key: str, explicit: Path | None = None
) -> tuple[Path | None, bool]:
    """Find the projections file for a league. Returns ``(path, is_league_specific)``.

    Per-league naming is not tidiness. An export carrying only a points total was scored
    under one league's settings before it ever reached us, so pointing the auction league
    at the snake league's file produces numbers that are wrong in a way nothing
    downstream can detect -- not the crosswalk, not the coverage report, not the board.
    Keying the filename to the league key makes that mix-up impossible rather than merely
    discouraged.

    A per-stat export has no such problem, which is why the shared fallback exists at all.
    """
    if explicit is not None:
        return explicit, True

    keyed = data_dir / f"projections-{league_key.replace('/', '_')}.csv"
    if keyed.exists():
        return keyed, True

    shared = data_dir / "projections.csv"
    if shared.exists():
        return shared, False

    return None, False


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


def _position_from_rank(raw: str | None) -> str:
    """Pull "RB" out of a combined positional rank like "RB-01".

    Returning "" rather than guessing is safe: Yahoo is the canonical registry, so the
    crosswalk supplies the real position. This only sharpens the match.
    """
    if not raw:
        return ""
    head = raw.strip().replace("_", "-").split("-", 1)[0].strip()
    return head if head.isalpha() else ""


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
                adp = _to_float(_column(row, _ADP_COLUMNS))
                auction_cost = _to_float(_column(row, _COST_COLUMNS))
            except ValueError as exc:
                problems.append(f"line {line_number} ({name}): {exc}")
                continue

            # A row whose every stat is zero is not a projection of nothing, it is a
            # player this export does not project in scoreable categories -- kickers
            # carry FG and XP columns the scoring engine has no stat IDs for, so their
            # skill-stat cells are all zero. Keeping the zeros would score them as a real
            # 0.0 and suppress the interpolation that is supposed to rank them.
            if stats and not any(stats.values()):
                stats = {}

            if not stats and points is None:
                problems.append(f"line {line_number} ({name}): no stats and no points")
                continue

            position = _column(row, _POSITION_COLUMNS)
            if not position:
                position = _position_from_rank(_column(row, _POSITION_RANK_COLUMNS))

            rows.append(
                SourceRow(
                    name=name,
                    position=normalize_position(position or ""),
                    team=normalize_team(_column(row, _TEAM_COLUMNS) or ""),
                    source=source,
                    projected_points=points,
                    adp=adp,
                    auction_cost=auction_cost,
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

    if rows and not any(row.stats for row in rows):
        # Not a soft degradation. rankings/blend.py deliberately discards a source's own
        # point total, so a file with no stat lines contributes nothing: every player
        # falls through to _fill_missing_points, every position reports "no stat
        # projections available", and the whole board comes back 0.0 and ranked by ADP.
        # An error here beats a plausible-looking board with no values behind it.
        raise ProjectionsError(
            f"{path} has points but no per-stat columns, and per-stat columns are what "
            "the app scores. A points total is discarded on purpose, because it carries "
            "the exporter's scoring rather than your league's -- so this file would "
            "produce a board where every player is worth 0.0 and ranking falls back to "
            "ADP.\nExport projections with stat columns (passing yards, receptions, "
            "rushing TDs, and so on) rather than a rankings table."
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
