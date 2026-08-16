"""Fantasy Football Calculator ADP.

A free, public, JSON REST API. Their terms ask for attribution and for callers not to hit
it aggressively -- we fetch once per run and cache to disk, which satisfies both.

Its real value is ``stdev``: most sources publish only a mean ADP, but the *spread* is
what turns "his ADP is 20" into "72% chance he is gone by pick 20", which is exactly what
the VONA survival model consumes.
"""

from __future__ import annotations

import httpx

from ff_helper.rankings.players import SourceRow, normalize_position, normalize_team

SOURCE = "ffc"
BASE_URL = "https://fantasyfootballcalculator.com/api/v1/adp"

# FFC's scoring slugs. Yahoo's scoring_type does not map perfectly, so callers pass this.
FORMATS = {"standard", "ppr", "half-ppr", "2qb", "dynasty", "rookie"}


def fetch(
    *,
    scoring: str = "ppr",
    teams: int = 12,
    year: int | None = None,
    client: httpx.Client | None = None,
) -> list[SourceRow]:
    """Fetch ADP for a given format. Raises on network/HTTP failure."""
    scoring = scoring if scoring in FORMATS else "ppr"
    params: dict[str, str | int] = {"teams": teams, "position": "all"}
    if year is not None:
        params["year"] = year

    owns_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        response = http.get(f"{BASE_URL}/{scoring}", params=params)
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http.close()

    return parse(payload)


def parse(payload: dict) -> list[SourceRow]:
    rows: list[SourceRow] = []
    for entry in payload.get("players", []):
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        rows.append(
            SourceRow(
                name=name,
                position=normalize_position(entry.get("position")),
                team=normalize_team(entry.get("team")),
                source=SOURCE,
                adp=_as_float(entry.get("adp")),
                adp_stdev=_as_float(entry.get("stdev")),
            )
        )
    return rows


def _as_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # FFC reports stdev 0 for players drafted only once; that would make the survival
    # model infinitely confident about a single observation.
    return number if number > 0 else None
