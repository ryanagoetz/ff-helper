#!/usr/bin/env python3
"""Build the ranking snapshot. Run this the day before your draft.

    python scripts/fetch_rankings.py

Fetches the Yahoo player pool (with Yahoo's own ADP), FantasyFootballCalculator ADP, and
FantasyPros consensus rankings and projections, then writes everything to
~/.ff-helper/cache/ so draft day does not depend on the network.

The coverage report at the end is the important part. A player who fails to match across
sources is silently absent from every recommendation, and you would never notice -- so the
script prints exactly who did not match and exits non-zero if coverage of the top of the
board is poor. Better to see that now than at pick 3.04.

ADP data courtesy of Fantasy Football Calculator (fantasyfootballcalculator.com).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ff_helper.config import load_settings  # noqa: E402
from ff_helper.engine.scoring import scoring_slug  # noqa: E402
from ff_helper.rankings import cache  # noqa: E402
from ff_helper.rankings.players import PlayerRegistry, SourceRow  # noqa: E402
from ff_helper.rankings.sources import (  # noqa: E402
    fantasypros,
    ffc,
    yahoo_adp,  # noqa: E402
)
from ff_helper.yahoo.client import YahooClient  # noqa: E402

# Coverage below this among the top players means something is structurally wrong.
MIN_TOP_COVERAGE = 0.90
TOP_N = 200


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--league",
        help="League key to snapshot. Defaults to FF_LEAGUE_KEY. Snapshots are cached per "
        "league, so run this once per league if you are in more than one.",
    )
    args = parser.parse_args()

    settings = load_settings()
    league_key = args.league or settings.league_key
    if not league_key:
        print("No league key. Set FF_LEAGUE_KEY in .env or pass --league.")
        print("Run scripts/setup_auth.py to list your leagues.")
        return 1

    notes: list[str] = []
    rows: list[SourceRow] = []

    with YahooClient(settings) as client:
        print(f"Fetching league {league_key} ...")
        league = client.league(league_key)
        if league.settings is None:
            print("Could not read league settings; cannot score projections.")
            return 1

        slug = scoring_slug(league.settings)
        print(f"  {league.name}: {league.num_teams} teams, {slug} scoring")

        print("Fetching Yahoo player pool (this walks 25 players per request) ...")
        players = client.players(league_key, limit=600)
        print(f"  {len(players)} players")

    # -- external sources. One failing must not sink the run. -----------------------
    ffc_scoring = {"ppr": "ppr", "half-ppr": "half-ppr", "standard": "standard"}[slug]
    try:
        print("Fetching FantasyFootballCalculator ADP ...")
        adp_rows = ffc.fetch(scoring=ffc_scoring, teams=league.num_teams)
        rows.extend(adp_rows)
        print(f"  {len(adp_rows)} players")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"FantasyFootballCalculator ADP unavailable: {exc}")
        print(f"  FAILED: {exc}")

    try:
        print("Fetching FantasyPros consensus rankings ...")
        ecr_rows = fantasypros.fetch_rankings(scoring="ppr" if slug == "ppr" else "half")
        rows.extend(ecr_rows)
        print(f"  {len(ecr_rows)} players")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"FantasyPros rankings unavailable: {exc}")
        print(f"  FAILED: {exc}")

    try:
        print("Fetching FantasyPros projections ...")
        projection_rows = fantasypros.fetch_projections(
            scoring={"ppr": "PPR", "half-ppr": "HALF", "standard": "STD"}[slug]
        )
        rows.extend(projection_rows)
        print(f"  {len(projection_rows)} players")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"FantasyPros projections unavailable: {exc}")
        print(f"  FAILED: {exc}")

    if not rows:
        print("\nEvery external source failed. Nothing useful to cache.")
        return 1

    # -- coverage report -------------------------------------------------------------
    registry = PlayerRegistry(players)
    _, report = registry.crosswalk(rows + yahoo_adp.from_players(players))

    print(f"\nCrosswalk: {report.match_rate:.1%} of source rows matched a Yahoo player")
    if report.fuzzy:
        print(f"  {len(report.fuzzy)} matched only by similarity:")
        for source_name, matched_name, score in report.fuzzy[:15]:
            print(f"    {source_name:<28} -> {matched_name:<28} ({score:.2f})")

    if report.unmatched:
        print(f"  {len(report.unmatched)} unmatched source rows:")
        for row in report.unmatched[:25]:
            print(f"    {row.source:<14} {row.name} ({row.position} {row.team})")
        if len(report.unmatched) > 25:
            print(f"    ... and {len(report.unmatched) - 25} more")

    top_coverage = _top_coverage(registry, rows, players)
    print(f"\nTop-{TOP_N} coverage: {top_coverage:.1%}")

    snapshot = cache.Snapshot(
        league_key=league_key,
        fetched_at=time.time(),
        players=players,
        rows=rows,
        notes=notes,
    )
    path = cache.save(snapshot)
    print(f"Snapshot written to {path}")

    for note in notes:
        print(f"  note: {note}")

    if top_coverage < MIN_TOP_COVERAGE:
        print(
            f"\nCoverage of the top {TOP_N} players is below {MIN_TOP_COVERAGE:.0%}. "
            "Players missing here will never be recommended -- worth investigating "
            "before draft day."
        )
        return 2

    return 0


def _top_coverage(registry: PlayerRegistry, rows: list[SourceRow], players: list) -> float:
    """What fraction of the top Yahoo players got data from at least one external source."""
    grouped, _ = registry.crosswalk(rows)
    # Yahoo returns the pool sorted by rank, so the first TOP_N are the ones that matter.
    top = players[:TOP_N]
    if not top:
        return 1.0
    covered = sum(1 for player in top if player.player_key in grouped)
    return covered / len(top)


if __name__ == "__main__":
    raise SystemExit(main())
