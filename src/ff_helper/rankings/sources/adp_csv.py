"""Platform ADP from a rankings export.

Offline there is no Yahoo API, so there is normally no Yahoo ADP -- and Yahoo ADP is the
single most valuable market signal the app has, weighted 0.65 in ``blend`` because it
describes drafts *on the platform your league drafts on*, against opponents reading
Yahoo's rankings in Yahoo's UI. National ADP describes drafts in general, which is a
different and less useful question.

4for4's rankings export publishes per-platform ADP columns, including ``ADP (Y!)``. That
recovers the signal from a file rather than an API, which is the whole point of this
module: the value comes from *which room* the ADP describes, and that survives being
exported to CSV.

Kept separate from ``projections_csv`` deliberately. That module reads the projections
export, which is a different report with different columns; this one reads only ADP and
emits it under whichever source label the column belongs to, so ``blend`` weights it
honestly. Emitting a national ADP as ``yahoo`` would earn the 0.65 weight under false
pretences, which is exactly the mistake offline mode made before this existed.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ff_helper.rankings.players import SourceRow, normalize_position, normalize_team

# Which column to prefer, and what to call the source when we find it. Order matters:
# a platform-specific ADP beats a blended one, because the platform is the point.
PLATFORM_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("yahoo", ("y!", "yahoo")),
    ("ffc", ("ffc",)),
)

# The generic blended column, used only when no platform column is present. It is not
# labelled "yahoo", so it gets the ordinary 0.2 weight rather than Yahoo's 0.65.
AVERAGE_MARKERS = ("average", "avg", "consensus")

# Markers these exports use for "this platform has no ADP for him".
_NO_ADP = {"", "--", "---", "n/a", "na", "-"}

MIN_ROWS = 20


class AdpError(ValueError):
    """The file could not be read as ADP. Loud on purpose."""


def _clean(header: str) -> str:
    return header.strip().strip('"').lower()


def _is_adp_column(header: str) -> bool:
    cleaned = _clean(header)
    # "ADP Dif (Y!)" is the *difference* between a player's rank and his ADP, not an ADP.
    # Reading it would silently produce ADPs clustered around zero.
    return "adp" in cleaned and "dif" not in cleaned


def pick_column(headers: list[str]) -> tuple[int, str]:
    """Choose the ADP column and the source label it should be filed under."""
    adp_columns = [(index, _clean(h)) for index, h in enumerate(headers) if _is_adp_column(h)]
    if not adp_columns:
        raise AdpError(
            "No ADP column found. Expected a header containing 'ADP', e.g. 'ADP (Y!)'."
        )

    for label, markers in PLATFORM_COLUMNS:
        for index, header in adp_columns:
            if any(marker in header for marker in markers):
                return index, label

    for index, header in adp_columns:
        if any(marker in header for marker in AVERAGE_MARKERS):
            return index, "adp_csv"

    return adp_columns[0][0], "adp_csv"


def _find(headers: list[str], *names: str) -> int | None:
    for index, header in enumerate(headers):
        if _clean(header) in names:
            return index
    return None


def _position_from_rank(raw: str) -> str:
    head = raw.strip().replace("_", "-").split("-", 1)[0].strip()
    return head if head.isalpha() else ""


def load(path: Path) -> tuple[list[SourceRow], str]:
    """Read ADP rows from a rankings export. Returns ``(rows, source_label)``.

    Read positionally rather than by ``DictReader``: these exports repeat header names
    across stat groups, and a dict collapses the duplicates onto whichever came last.
    """
    if not path.exists():
        raise AdpError(f"ADP file not found: {path}")

    try:
        raw = list(csv.reader(path.open(newline="", encoding="utf-8-sig")))
    except (OSError, csv.Error) as exc:
        raise AdpError(f"Could not read {path}: {exc}") from exc

    if len(raw) < 2:
        raise AdpError(f"{path} has no data rows.")

    headers = raw[0]
    adp_index, label = pick_column(headers)
    name_index = _find(headers, "player", "name", "player name")
    if name_index is None:
        raise AdpError(f"{path}: no player column found.")
    team_index = _find(headers, "team", "tm")
    position_index = _find(headers, "pos", "position")
    rank_index = _find(headers, "position-rank", "position rank", "pos rank")

    rows: list[SourceRow] = []
    unreadable: list[str] = []
    for record in raw[1:]:
        if len(record) <= max(adp_index, name_index):
            continue
        name = record[name_index].strip()
        if not name:
            continue

        cell = record[adp_index].strip().strip("'\"")
        # 4for4 writes "--" (often escaped as "'--") when a platform has no ADP for a
        # player, and 0 for the same thing elsewhere in the same file. Both mean
        # undrafted, which is information, not an error -- but a 0 read as a number
        # would make him the first overall pick.
        if cell in _NO_ADP:
            continue
        try:
            adp = float(cell)
        except ValueError:
            unreadable.append(f"{name}: {record[adp_index]!r}")
            continue
        if adp <= 0:
            continue

        position = ""
        if position_index is not None and len(record) > position_index:
            position = record[position_index].strip()
        if not position and rank_index is not None and len(record) > rank_index:
            position = _position_from_rank(record[rank_index])

        team = ""
        if team_index is not None and len(record) > team_index:
            team = record[team_index].strip()

        rows.append(
            SourceRow(
                name=name,
                position=normalize_position(position),
                team=normalize_team(team),
                source=label,
                adp=adp,
            )
        )

    if unreadable:
        raise AdpError(
            f"{path}: {len(unreadable)} ADP values could not be read, e.g. "
            + "; ".join(unreadable[:5])
        )

    if len(rows) < MIN_ROWS:
        raise AdpError(
            f"{path} produced only {len(rows)} ADP rows from column "
            f"{headers[adp_index]!r}. Check the export is a full board and that the "
            "column is populated."
        )

    return rows, label
