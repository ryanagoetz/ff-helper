#!/usr/bin/env python3
"""Preflight check. Run this first, on your own machine, before anything else.

    python scripts/doctor.py                    # uses FF_LEAGUE_KEY
    python scripts/doctor.py --league 461.l.123 # a specific league
    python scripts/doctor.py --all              # every league on your account

Everything in this app was built and tested offline against recorded fixtures, because the
sandbox it was written in cannot reach Yahoo. So there is a short list of things that can
only be confirmed against your real leagues -- whether Yahoo publishes an auction budget,
whether it fills in keeper salaries, whether pre-draft rosters are populated at all. Rather
than have you run five commands and eyeball the output, this answers all of it in one pass
and tells you plainly what is missing and what to do about it.

Read-only. It fetches and reports; it changes nothing, on Yahoo or on disk.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ff_helper.config import load_settings  # noqa: E402
from ff_helper.engine.scoring import scoring_slug  # noqa: E402
from ff_helper.rankings import cache  # noqa: E402
from ff_helper.yahoo.client import YahooClient  # noqa: E402
from ff_helper.yahoo.models import DEFAULT_AUCTION_BUDGET  # noqa: E402

OK = "  ok   "
WARN = " warn  "
FAIL = " FAIL  "

# Collected across checks so the summary can tell the user what to actually do.
_actions: list[str] = []


def line(status: str, message: str) -> None:
    print(f"[{status}] {message}")


def action(message: str) -> None:
    _actions.append(message)


def check_auth(settings) -> YahooClient | None:
    print("\n-- Yahoo sign-in " + "-" * 56)
    try:
        client = YahooClient(settings)
    except Exception as exc:  # noqa: BLE001 - reporting beats a traceback here
        line(FAIL, f"Not signed in: {exc}")
        action("Run `python scripts/setup_auth.py` to sign in to Yahoo.")
        return None
    line(OK, "Token loaded and refreshable")
    return client


def check_leagues(client: YahooClient) -> list:
    try:
        leagues = client.my_leagues()
    except Exception as exc:  # noqa: BLE001
        line(FAIL, f"Could not list leagues: {exc}")
        return []

    if not leagues:
        line(WARN, "No NFL leagues found on this account for the current season")
        return []

    line(OK, f"{len(leagues)} NFL league(s) visible:")
    for league in leagues:
        print(f"         {league.league_key}  {league.name} ({league.num_teams} teams)")
    return leagues


def check_league(client: YahooClient, league_key: str, settings) -> None:
    print(f"\n-- {league_key} " + "-" * max(0, 68 - len(league_key)))

    try:
        league = client.league(league_key)
    except Exception as exc:  # noqa: BLE001
        line(FAIL, f"Could not read league: {exc}")
        return

    if league.settings is None:
        line(FAIL, "No settings returned; scoring and roster slots are unavailable")
        action(f"{league_key}: league settings did not parse -- send me the raw response.")
        return

    config = league.settings
    draft_type = "auction" if config.is_auction else "snake"
    line(
        OK,
        f"{league.name}: {league.num_teams} teams, {draft_type} draft, "
        f"status '{league.draft_status}'",
    )
    line(
        OK,
        f"Scoring reads as {scoring_slug(config)}; roster is {config.roster_size} spots "
        f"({config.bench_size} bench)",
    )

    # -- auction budget ------------------------------------------------------------
    if config.is_auction:
        if config.auction_budget == DEFAULT_AUCTION_BUDGET:
            line(
                WARN,
                f"Auction budget reads ${config.auction_budget} -- this may be Yahoo's "
                "value or just our fallback; the two are indistinguishable here",
            )
            action(
                f"{league_key}: confirm the real auction budget in Yahoo. If it is not "
                "$200, set FF_AUCTION_BUDGET -- every dollar value scales off it."
            )
        else:
            line(OK, f"Auction budget published by Yahoo: ${config.auction_budget}")

    # -- teams and my slot ---------------------------------------------------------
    try:
        teams = client.teams(league_key)
    except Exception as exc:  # noqa: BLE001
        line(FAIL, f"Could not read teams: {exc}")
        return

    mine = next((team for team in teams if team.is_mine), None)
    if mine is None:
        line(FAIL, "Could not identify which team is yours")
        action(f"{league_key}: your team was not flagged is_owned_by_current_login.")
    elif mine.draft_position:
        line(OK, f"Your team is '{mine.name}', draft slot {mine.draft_position}")
    else:
        line(WARN, f"Your team is '{mine.name}', but the draft order is not published yet")

    # -- keepers -------------------------------------------------------------------
    check_keepers(client, league, teams, league_key)

    # -- player data ---------------------------------------------------------------
    check_player_data(client, league_key, config.is_auction)

    # -- snapshot ------------------------------------------------------------------
    snapshot = cache.load(league_key)
    if snapshot is None:
        line(WARN, "No ranking snapshot cached yet")
        action(f"Run `python scripts/fetch_rankings.py --league {league_key}`.")
    else:
        stale = " (stale -- re-run before draft day)" if snapshot.age_hours > 48 else ""
        line(
            OK,
            f"Snapshot cached: {len(snapshot.players)} players, "
            f"{snapshot.age_hours:.0f}h old{stale}",
        )


def check_keepers(client: YahooClient, league, teams: list, league_key: str) -> None:
    """The keeper questions that could only be guessed at offline."""
    try:
        kept, failures = client.keepers(teams)
    except Exception as exc:  # noqa: BLE001
        line(FAIL, f"Roster fetch failed outright: {exc}")
        return

    if failures:
        line(WARN, f"{len(failures)} of {len(teams)} rosters failed to load")
        action(
            f"{league_key}: some rosters did not load; keepers on those teams would "
            "stay in the pool. Re-run to see if it was transient."
        )

    if not kept:
        if league.draft_status == "predraft":
            line(
                OK,
                "No players on any roster -- so this is not a keeper league, "
                "or keepers are not set yet",
            )
        else:
            line(
                WARN,
                "No keepers detected, but the draft is already underway so "
                "rosters cannot be told apart from drafted players",
            )
        return

    per_team = {}
    for keeper in kept:
        per_team[keeper.team_key] = per_team.get(keeper.team_key, 0) + 1
    counts = sorted(set(per_team.values()))

    line(OK, f"{len(kept)} keepers detected across {len(per_team)} of {len(teams)} teams")
    if len(counts) > 1 or len(per_team) < len(teams):
        line(
            WARN,
            f"Teams keep different numbers ({counts}) -- your own pick numbers stay "
            "exact, but a rival's countdown may drift",
        )

    if league.settings and league.settings.is_auction:
        priced = [keeper for keeper in kept if keeper.cost is not None]
        if not priced:
            line(FAIL, "No keeper has a salary from Yahoo -- all would be counted as $0")
            action(
                f"{league_key}: Yahoo published no keeper salaries. Supply them with a CSV "
                "(--keepers), or budgets and every auction price will be overstated."
            )
        elif len(priced) < len(kept):
            line(WARN, f"Only {len(priced)} of {len(kept)} keepers have a salary")
            action(
                f"{league_key}: {len(kept) - len(priced)} keeper salaries missing; "
                "supply them with --keepers."
            )
        else:
            line(OK, "Every keeper has a salary from Yahoo")


def check_player_data(client: YahooClient, league_key: str, is_auction: bool) -> None:
    """ADP drives the snake model; average_cost drives the auction mispricing edge."""
    try:
        players = client.players(league_key, limit=50)
    except Exception as exc:  # noqa: BLE001
        line(FAIL, f"Could not read the player pool: {exc}")
        return

    if not players:
        line(FAIL, "Player pool came back empty")
        return

    with_adp = [p for p in players if p.draft_analysis.average_pick is not None]
    line(
        OK if len(with_adp) > len(players) * 0.8 else WARN,
        f"Yahoo ADP present for {len(with_adp)}/{len(players)} of the top players",
    )
    if len(with_adp) <= len(players) * 0.8:
        action(
            f"{league_key}: Yahoo ADP is sparse; survival estimates lean on "
            "FantasyFootballCalculator instead."
        )

    if is_auction:
        with_cost = [p for p in players if p.draft_analysis.average_cost is not None]
        if not with_cost:
            line(FAIL, "No average_cost on any player -- the market-price signal is absent")
            action(
                f"{league_key}: Yahoo publishes no auction average_cost. Recommendations "
                "still work off value, but the 'what the room will pay' edge is lost."
            )
        else:
            line(
                OK if len(with_cost) > len(players) * 0.8 else WARN,
                f"Auction average_cost present for {len(with_cost)}/{len(players)}",
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", help="League key to check (defaults to FF_LEAGUE_KEY)")
    parser.add_argument("--all", action="store_true", help="Check every league on the account")
    args = parser.parse_args()

    settings = load_settings()
    client = check_auth(settings)
    if client is None:
        _summary()
        return 1

    with client:
        leagues = check_leagues(client)

        if args.all:
            targets = [league.league_key for league in leagues]
        elif args.league:
            targets = [args.league]
        elif settings.league_key:
            targets = [settings.league_key]
        else:
            targets = []
            line(WARN, "No league selected; pass --league or --all, or set FF_LEAGUE_KEY")

        for league_key in targets:
            check_league(client, league_key, settings)

    return _summary()


def _summary() -> int:
    print("\n" + "=" * 72)
    if not _actions:
        print("  Nothing to fix. Build the snapshot, then run a mock draft.")
        return 0
    print(f"  {len(_actions)} thing(s) to sort out before draft day:\n")
    for index, item in enumerate(_actions, start=1):
        print(f"   {index}. {item}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
