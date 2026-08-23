"""Per-stat *weekly* projections from a CSV export.

The in-season sibling of ``projections_csv``. Same premise, same subscription, same
refusal to trust an exporter's point total -- a weekly projection is re-scored under your
league's modifiers for exactly the reason a season one is. Nearly every guard and alias
table is imported from there rather than copied, so a spelling learned about 4for4 in
August still holds in November.

Three things differ, and each is a real difference rather than a preference:

**A bye is not a missing projection.** In a season file an all-zero stat line means "this
export does not project him in scoreable categories" (kickers, defenses) and blanking it
lets interpolation rank him. In a weekly file it usually means his team is not playing.
Both are blanked here, but a row with neither stats nor a point total is *counted and
dropped* rather than raising -- byes are a normal fact about a week, and failing the whole
fetch over thirty of them would make the tool unusable in weeks 5 through 14. The count
comes back in ``notes`` so it stays visible.

**The file may carry more than one week.** Exports differ: some are one week per file,
some carry the season. A ``week`` column is filtered on when present, and its absence is
trusted rather than assumed -- if you exported week 7 into a file with no week column, the
filename is the only record of that, which is why ``resolve_path`` keys on the week too.

**Opponent rides alongside rather than inside.** ``SourceRow`` is the crosswalk's input and
is deliberately left unchanged, so the opponent travels in a ``WeeklyProjection`` wrapper
that the season layer re-attaches by name after matching.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ff_helper.rankings.players import SourceRow, normalize_position, normalize_team
from ff_helper.rankings.sources.projections_csv import (
    _ADP_COLUMNS,
    _PLAYER_COLUMNS,
    _POINTS_COLUMNS,
    _POSITION_COLUMNS,
    _POSITION_RANK_COLUMNS,
    _STAT_COLUMNS,
    _TEAM_COLUMNS,
    ProjectionsError,
    _column,
    _position_from_rank,
    _to_float,
)

SOURCE = "csv-weekly"

# Lower than the season file's threshold on purpose. A weekly export still covers the
# whole player pool, but byes and inactives legitimately thin it, and the failure this
# guards against -- a truncated or wrong file -- shows up long before twenty rows.
MIN_ROWS = 20

_WEEK_COLUMNS = ("week", "wk", "game_week")
_OPPONENT_COLUMNS = ("opp", "opponent", "vs", "matchup", "opp_team")


@dataclass(frozen=True)
class WeeklyProjection:
    """One weekly projection: the crosswalk's input, plus this week's context.

    ``row`` is what ``PlayerRegistry.crosswalk`` consumes. Everything else describes the
    *game*, which is a property of the week rather than of the player, and therefore has
    no business inside a ``SourceRow``.
    """

    row: SourceRow
    opponent: str = ""
    is_home: bool | None = None

    @property
    def name(self) -> str:
        return self.row.name


def resolve_path(
    data_dir: Path, league_key: str, week: int, explicit: Path | None = None
) -> tuple[Path | None, bool]:
    """Find the weekly projections file. Returns ``(path, is_league_specific)``.

    Looks in ``data/weekly/`` first and the flat ``data/`` second, so a season's worth of
    files can be kept together without breaking anyone who drops one where the season
    export goes. Per-league naming carries the same warning as ``projections_csv``: a file
    that only has a points total was scored before it reached us.
    """
    safe = league_key.replace("/", "_")
    candidates = (
        data_dir / "weekly" / f"weekly-{safe}-w{week:02d}.csv",
        data_dir / f"weekly-{safe}-w{week:02d}.csv",
    )
    shared = (
        data_dir / "weekly" / f"weekly-w{week:02d}.csv",
        data_dir / f"weekly-w{week:02d}.csv",
    )

    if explicit is not None:
        return explicit, True
    for path in candidates:
        if path.exists():
            return path, True
    for path in shared:
        if path.exists():
            return path, False
    return None, False


def _normalize_opponent(raw: str | None) -> tuple[str, bool | None]:
    """Split "@KC" or "vs. KC" into a team abbreviation and home/away.

    Home/away is returned as None when the file does not say, rather than defaulting to
    home -- a wrong home flag is worse than no flag, because it looks like information.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return "", None

    is_home: bool | None = None
    lowered = cleaned.lower()
    if lowered.startswith("@"):
        is_home = False
        cleaned = cleaned[1:]
    elif lowered.startswith("vs."):
        is_home = True
        cleaned = cleaned[3:]
    elif lowered.startswith("vs"):
        is_home = True
        cleaned = cleaned[2:]

    return normalize_team(cleaned.strip()), is_home


def load(
    path: Path, *, week: int | None = None, source: str = SOURCE
) -> tuple[list[WeeklyProjection], list[str]]:
    """Read weekly projections from a CSV export.

    Expected columns (case-insensitive, order-independent, extras ignored)::

        player,pos,team,opp,pass_yds,pass_td,int,rush_yds,rush_td,rec,rec_yds,rec_td
        Ja'Marr Chase,WR,CIN,@BAL,0,0,0,0,0,7,94,0.6

    Only ``player`` is required. When ``week`` is given and the file has a week column,
    other weeks are filtered out. Returns ``(projections, notes)`` -- the notes carry what
    was dropped, in the same spirit as the snapshot's own ``notes``.
    """
    if not path.exists():
        raise ProjectionsError(f"Weekly projections file not found: {path}")

    try:
        handle = path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ProjectionsError(f"Could not open {path}: {exc}") from exc

    projections: list[WeeklyProjection] = []
    problems: list[str] = []
    notes: list[str] = []
    data_rows = 0
    other_week_rows = 0
    unprojected: list[str] = []
    missing_player_column = False
    saw_week_column = False

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
                row_week = _to_float(_column(row, _WEEK_COLUMNS))
                stats: dict[str, float] = {}
                for key, candidates in _STAT_COLUMNS.items():
                    value = _to_float(_column(row, candidates))
                    if value is not None:
                        stats[key] = value
                points = _to_float(_column(row, _POINTS_COLUMNS))
                adp = _to_float(_column(row, _ADP_COLUMNS))
            except ValueError as exc:
                problems.append(f"line {line_number} ({name}): {exc}")
                continue

            if row_week is not None:
                saw_week_column = True
                if week is not None and int(row_week) != week:
                    other_week_rows += 1
                    continue

            # Same rule as the season file: an all-zero stat line is not a projection of
            # nothing. Weekly it is usually a bye, sometimes a kicker or defense whose
            # only columns the scoring engine has no stat IDs for.
            if stats and not any(stats.values()):
                stats = {}

            if not stats and points is None:
                # A bye, or a player this export does not project this week. Dropped and
                # counted rather than raised -- see the module docstring.
                unprojected.append(name)
                continue

            position = _column(row, _POSITION_COLUMNS)
            if not position:
                position = _position_from_rank(_column(row, _POSITION_RANK_COLUMNS))

            opponent, is_home = _normalize_opponent(_column(row, _OPPONENT_COLUMNS))
            projections.append(
                WeeklyProjection(
                    row=SourceRow(
                        name=name,
                        position=normalize_position(position or ""),
                        team=normalize_team(_column(row, _TEAM_COLUMNS) or ""),
                        source=source,
                        projected_points=points,
                        adp=adp,
                        stats=stats,
                    ),
                    opponent=opponent,
                    is_home=is_home,
                )
            )

    if missing_player_column or (not projections and not problems and not unprojected):
        raise ProjectionsError(
            f"{path} produced no weekly projections ({data_rows} data rows read).\n"
            f"Expected a player column named one of {', '.join(_PLAYER_COLUMNS)}, "
            f"plus either per-stat columns or one of {', '.join(_POINTS_COLUMNS)}."
        )

    if problems:
        raise ProjectionsError(
            f"{path} could not be fully read:\n  "
            + "\n  ".join(problems[:20])
            + (f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else "")
        )

    if projections and not any(p.row.stats for p in projections):
        # Identical reasoning to the season loader, and it matters more weekly: a start
        # or sit decision made on someone else's scoring is the whole failure this app
        # exists to avoid.
        raise ProjectionsError(
            f"{path} has points but no per-stat columns, and per-stat columns are what "
            "the app scores. A points total is discarded on purpose, because it carries "
            "the exporter's scoring rather than your league's.\nExport weekly "
            "projections with stat columns (passing yards, receptions, rushing TDs, and "
            "so on) rather than a rankings or points table."
        )

    if len(projections) < MIN_ROWS:
        raise ProjectionsError(
            f"{path} has only {len(projections)} weekly projections, which is too few to "
            f"pick a lineup or rank a waiver wire from (expected at least {MIN_ROWS}, and "
            "realistically 300+). Check the export covers every position and every team, "
            "and that it is the right week."
        )

    if week is not None and not saw_week_column:
        notes.append(
            f"{path.name} has no week column, so every row was taken as week {week} -- "
            "the filename is the only record of which week this is"
        )
    if other_week_rows:
        notes.append(f"{other_week_rows} rows in {path.name} were for other weeks")
    if unprojected:
        preview = ", ".join(sorted(unprojected)[:8])
        plural = "player" if len(unprojected) == 1 else "players"
        notes.append(
            f"{len(unprojected)} {plural} in {path.name} with no projection this week "
            f"(bye, inactive, or not covered): {preview}"
            + (" ..." if len(unprojected) > 8 else "")
        )

    return projections, notes
