"""FantasyPros expert consensus rankings and projections.

Two things are pulled, and they serve different purposes:

* **Consensus rankings (ECR)** carry *tiers* and the spread of expert opinion. Tiers are
  the single most useful thing on the page -- knowing a positional cliff is two picks away
  matters more than the exact ordinal ranks either side of it.
* **Projections** are per-stat (passing yards, receptions, ...) rather than a single point
  total. That matters: we re-score them under *your* league's stat modifiers rather than
  inheriting FantasyPros' assumed scoring, which is what makes the value numbers actually
  yours.

Scraping is inherently fragile. Every function here raises a clear ``ScrapeError`` on an
unexpected page shape instead of returning a plausible-looking empty list, and the caller
caches results so a break on draft morning degrades to slightly stale data rather than
none.
"""

from __future__ import annotations

import json
import re

import httpx
from selectolax.parser import HTMLParser

from ff_helper.rankings.players import SourceRow, normalize_position, normalize_team

SOURCE = "fantasypros"
BASE = "https://www.fantasypros.com/nfl"

# The rankings pages embed their data as a JS object literal; parsing that is far more
# stable than walking a React-rendered table.
_ECR_PATTERN = re.compile(r"var\s+ecrData\s*=\s*(\{.*?\});", re.DOTALL)

# Yahoo scoring -> FantasyPros page slug for consensus rankings.
RANKING_PAGES = {
    "ppr": "consensus-cheatsheets",
    "half": "half-point-ppr-cheatsheets",
    "standard": "consensus-cheatsheets",
}

PROJECTION_POSITIONS = ("qb", "rb", "wr", "te")

# FantasyPros projection table columns, in order, per position. The tables repeat header
# names across stat groups (e.g. YDS appears under both passing and rushing), so position
# is the only reliable way to know what each column means.
_PROJECTION_COLUMNS: dict[str, tuple[str, ...]] = {
    "qb": (
        "pass_att",
        "pass_cmp",
        "pass_yds",
        "pass_td",
        "int",
        "rush_att",
        "rush_yds",
        "rush_td",
        "fum_lost",
        "fpts",
    ),
    "rb": (
        "rush_att",
        "rush_yds",
        "rush_td",
        "rec",
        "rec_yds",
        "rec_td",
        "fum_lost",
        "fpts",
    ),
    "wr": (
        "rec",
        "rec_yds",
        "rec_td",
        "rush_att",
        "rush_yds",
        "rush_td",
        "fum_lost",
        "fpts",
    ),
    "te": ("rec", "rec_yds", "rec_td", "fum_lost", "fpts"),
}


class ScrapeError(RuntimeError):
    """The page did not look the way we expect. Loud on purpose."""


def _get(url: str, client: httpx.Client | None = None, **params: object) -> str:
    owns_client = client is None
    http = client or httpx.Client(
        timeout=30.0,
        follow_redirects=True,
        # Default httpx UA gets served a challenge page.
        headers={"User-Agent": "Mozilla/5.0 (compatible; ff-helper/0.1)"},
    )
    try:
        response = http.get(url, params=params)
        response.raise_for_status()
        return response.text
    finally:
        if owns_client:
            http.close()


# -- consensus rankings ------------------------------------------------------------------


def fetch_rankings(*, scoring: str = "ppr", client: httpx.Client | None = None) -> list[SourceRow]:
    page = RANKING_PAGES.get(scoring, RANKING_PAGES["ppr"])
    html = _get(f"{BASE}/rankings/{page}.php", client=client)
    return parse_rankings(html)


def parse_rankings(html: str) -> list[SourceRow]:
    match = _ECR_PATTERN.search(html)
    if not match:
        raise ScrapeError(
            "Could not find the ecrData block on the FantasyPros rankings page. "
            "The page layout has probably changed."
        )
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"ecrData was not valid JSON: {exc}") from exc

    rows: list[SourceRow] = []
    for entry in data.get("players", []):
        name = (entry.get("player_name") or "").strip()
        if not name:
            continue
        rows.append(
            SourceRow(
                name=name,
                position=normalize_position(entry.get("player_position_id")),
                team=normalize_team(entry.get("player_team_id")),
                source=SOURCE,
                ecr=_as_float(entry.get("rank_ecr")),
                tier=_as_int(entry.get("tier")),
                # rank_std is the spread of expert opinion, not of draft position, so it
                # is deliberately not used as an ADP stdev.
            )
        )
    if not rows:
        raise ScrapeError("FantasyPros rankings parsed to zero players.")
    return rows


# -- projections -------------------------------------------------------------------------


def fetch_projections(
    *, scoring: str = "PPR", client: httpx.Client | None = None
) -> list[SourceRow]:
    """Per-stat season projections for QB/RB/WR/TE.

    A failure for one position is tolerated -- three positions of projections beats none.
    """
    rows: list[SourceRow] = []
    errors: list[str] = []
    for position in PROJECTION_POSITIONS:
        try:
            html = _get(
                f"{BASE}/projections/{position}.php",
                client=client,
                week="draft",
                scoring=scoring,
            )
            rows.extend(parse_projections(html, position))
        except (httpx.HTTPError, ScrapeError) as exc:
            errors.append(f"{position}: {exc}")

    if not rows:
        raise ScrapeError("All projection pages failed: " + "; ".join(errors))
    return rows


def parse_projections(html: str, position: str) -> list[SourceRow]:
    columns = _PROJECTION_COLUMNS.get(position)
    if columns is None:
        raise ScrapeError(f"No column map for position {position!r}")

    tree = HTMLParser(html)
    table = tree.css_first("table#data") or tree.css_first("table")
    if table is None:
        raise ScrapeError(f"No projections table found on the {position} page.")

    rows: list[SourceRow] = []
    for tr in table.css("tbody tr"):
        cells = tr.css("td")
        if len(cells) < 2:
            continue

        name, team = _split_player_cell(cells[0])
        if not name:
            continue

        values = [_as_float(cell.text(strip=True).replace(",", "")) for cell in cells[1:]]
        stats: dict[str, float] = {}
        for column, value in zip(columns, values, strict=False):
            if value is not None:
                stats[column] = value

        rows.append(
            SourceRow(
                name=name,
                position=normalize_position(position),
                team=normalize_team(team),
                source=SOURCE,
                projected_points=stats.pop("fpts", None),
                stats=stats,
            )
        )

    if not rows:
        raise ScrapeError(f"Projections table for {position} parsed to zero rows.")
    return rows


def _split_player_cell(cell) -> tuple[str, str]:
    """The player cell holds a name link plus a small team abbreviation."""
    link = cell.css_first("a")
    name = link.text(strip=True) if link else cell.text(strip=True)

    team = ""
    small = cell.css_first("small")
    if small:
        team = small.text(strip=True)
    elif not link:
        # Fallback shape: "Ja'Marr Chase CIN" in a single cell.
        parts = name.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].isupper() and len(parts[1]) <= 4:
            name, team = parts

    return name.strip(), team.strip()


def _as_float(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None
