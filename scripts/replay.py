#!/usr/bin/env python3
"""Replay a completed draft and show what the engine would have recommended.

    python scripts/replay.py                      # your league's current/last draft
    python scripts/replay.py --league 449.l.9999  # a specific (e.g. prior season) league

This is the real test of the model, and it is worth running well before draft day. At each
of your turns it prints the engine's top recommendation alongside who you actually took and
who was still on the board. If it keeps recommending players who in fact went 40 picks
later, the survival model is miscalibrated and you want to know that in August.

It runs entirely off the cached snapshot plus the draft results, so it needs no network
once ``scripts/fetch_rankings.py`` has been run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ff_helper.assistant import Assistant  # noqa: E402
from ff_helper.config import load_settings  # noqa: E402
from ff_helper.draft.state import DraftState  # noqa: E402
from ff_helper.rankings import cache  # noqa: E402
from ff_helper.yahoo.client import YahooClient  # noqa: E402
from ff_helper.yahoo.models import DraftPick  # noqa: E402


def replay(assistant: Assistant, picks: list[DraftPick], *, limit: int = 3) -> dict:
    """Walk the draft pick by pick, reporting at each of the user's turns."""
    state = assistant.state
    my_team = state.my_team
    my_team_key = my_team.team_key if my_team else None

    hits = 0
    turns = 0
    agreement_ranks: list[int] = []

    ordered = sorted(picks)
    for pick in ordered:
        if pick.team_key == my_team_key:
            turns += 1
            recommendations = assistant.recommendations(limit=limit)
            actual_name = assistant._player_name(pick.player_key)

            print(f"\n  Pick {pick.pick} (round {pick.round})")
            print(f"    you took:    {actual_name}")
            if recommendations:
                for index, rec in enumerate(recommendations, start=1):
                    marker = "<-- match" if rec.valuation.player_key == pick.player_key else ""
                    print(
                        f"    engine #{index}:  {rec.name:<24} {rec.position:<4}"
                        f" VOR {rec.vor:6.1f}  VONA {rec.vona:6.1f}  {marker}"
                    )
                    if rec.valuation.player_key == pick.player_key:
                        agreement_ranks.append(index)
                        if index == 1:
                            hits += 1
                if recommendations[0].valuation.player_key != pick.player_key:
                    print(f"    reason:      {recommendations[0].reason}")
            else:
                print("    engine had no recommendation (player pool exhausted)")

        # Advance the board past this pick.
        state.apply_sync([pick], timestamp=0.0)

    return {
        "turns": turns,
        "top_matches": hits,
        "in_top_n": len(agreement_ranks),
        "limit": limit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", help="League key to replay (defaults to FF_LEAGUE_KEY)")
    parser.add_argument("--limit", type=int, default=3, help="How many recommendations to show")
    args = parser.parse_args()

    settings = load_settings()
    league_key = args.league or settings.league_key
    if not league_key:
        print("No league key. Set FF_LEAGUE_KEY in .env or pass --league.")
        return 1

    snapshot = cache.load(settings.league_key or league_key)
    if snapshot is None:
        print("No ranking snapshot found. Run scripts/fetch_rankings.py first.")
        return 1

    with YahooClient(settings) as client:
        league = client.league(league_key)
        teams = client.teams(league_key)
        picks = client.draft_results(league_key)

    if not picks:
        print(f"{league.name} has no draft results yet (status: {league.draft_status}).")
        return 1

    state = DraftState(league=league, teams=teams)
    assistant = Assistant.build(league, state, snapshot)

    print(f"Replaying {league.name} -- {len(picks)} picks, {league.num_teams} teams")
    if state.my_slot:
        print(f"Your slot: {state.my_slot}")
    else:
        print("Could not identify your team in this league; nothing to compare against.")
        return 1

    summary = replay(assistant, picks, limit=args.limit)

    print(f"\n{'=' * 62}")
    print(f"  Your turns:                {summary['turns']}")
    print(f"  Engine's #1 matched:       {summary['top_matches']}")
    print(f"  Your pick in engine's top {summary['limit']}: {summary['in_top_n']}")
    print(
        "\n  Disagreement is expected and not by itself a problem -- you have\n"
        "  information the model does not. What matters is whether the engine's\n"
        "  picks were plausible at that moment, and whether the players it wanted\n"
        "  actually lasted as long as it predicted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
