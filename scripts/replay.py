#!/usr/bin/env python3
"""Replay a completed draft and show what the engine would have recommended.

    python scripts/replay.py                      # your league's current/last draft
    python scripts/replay.py --league 449.l.9999  # a specific (e.g. prior season) league
    python scripts/replay.py --dump data/drafts/2025.json   # also save a draft record
    python scripts/replay.py --from-file data/drafts/2025.json   # replay offline

This is the real test of the model, and it is worth running well before draft day. At each
of your turns it prints the engine's top recommendation alongside who you actually took and
who was still on the board. If it keeps recommending players who in fact went 40 picks
later, the survival model is miscalibrated and you want to know that in August.

``--dump`` writes the fetched draft to a record file (anonymized unless ``--keep-names``),
which is what ``scripts/backtest.py`` scores calibration and counterfactuals against --
make it a habit after every real and mock draft. ``--from-file`` replays such a record
with no network at all.

It runs entirely off the cached snapshot plus the draft results, so it needs no network
once ``scripts/fetch_rankings.py`` has been run (and none whatsoever with ``--from-file``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ff_helper.assistant import Assistant  # noqa: E402
from ff_helper.backtest.calibration import TurnReport, turn_reports  # noqa: E402
from ff_helper.backtest.capture import (  # noqa: E402
    build_state,
    load_record,
    record_from_live,
    save_record,
)
from ff_helper.config import load_settings  # noqa: E402
from ff_helper.draft.state import DraftState  # noqa: E402
from ff_helper.rankings import cache  # noqa: E402
from ff_helper.yahoo.client import YahooClient  # noqa: E402
from ff_helper.yahoo.models import DraftPick, League, Team  # noqa: E402


def replay(assistant: Assistant, picks: list[DraftPick], *, limit: int = 3) -> dict:
    """Walk the draft pick by pick, reporting at each of the user's turns."""

    def show(report: TurnReport) -> None:
        print(f"\n  Pick {report.pick} (round {report.round})")
        print(f"    you took:    {report.actual_name}")
        if report.recommendations:
            for index, rec in enumerate(report.recommendations, start=1):
                marker = "<-- match" if index == report.match_rank else ""
                print(
                    f"    engine #{index}:  {rec.name:<24} {rec.position:<4}"
                    f" VOR {rec.vor:6.1f}  VONA {rec.vona:6.1f}  {marker}"
                )
            if report.match_rank != 1:
                print(f"    reason:      {report.recommendations[0].reason}")
        else:
            print("    engine had no recommendation (player pool exhausted)")

    reports = turn_reports(assistant, picks, limit=limit, on_turn=show)
    return {
        "turns": len(reports),
        "top_matches": sum(1 for r in reports if r.match_rank == 1),
        "in_top_n": sum(1 for r in reports if r.match_rank is not None),
        "limit": limit,
    }


def _load_from_file(path: Path) -> tuple[League, list[Team], list[DraftPick], DraftState, str]:
    record = load_record(path)
    league, state = build_state(record)
    return league, list(record.teams), list(record.picks), state, (
        record.snapshot_ref or league.league_key
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", help="League key to replay (defaults to FF_LEAGUE_KEY)")
    parser.add_argument("--limit", type=int, default=3, help="How many recommendations to show")
    parser.add_argument("--dump", help="Write the fetched draft to this record file")
    parser.add_argument("--from-file", help="Replay a saved draft record instead of fetching")
    parser.add_argument(
        "--keep-names",
        action="store_true",
        help="Keep real team/league names in the dumped record (default anonymizes)",
    )
    args = parser.parse_args()

    if args.from_file:
        # The offline path skips load_settings(), which is the only place .env gets
        # loaded -- without this, FF_HELPER_HOME/FF_MC_ROLLOUTS set there are ignored
        # and cache.load looks in the wrong state directory.
        from dotenv import load_dotenv

        load_dotenv()
        league, teams, picks, state, snapshot_key = _load_from_file(Path(args.from_file))
        snapshot = cache.load(snapshot_key)
        if snapshot is None:
            print(
                f"No ranking snapshot found for {snapshot_key}. "
                "Run scripts/fetch_rankings.py first."
            )
            return 1
    else:
        settings = load_settings()
        league_key = args.league or settings.league_key
        if not league_key:
            print("No league key. Set FF_LEAGUE_KEY in .env or pass --league.")
            return 1

        # Snapshots are keyed by league, so replaying a different league must load that
        # league's snapshot -- not whichever one happens to be in .env.
        snapshot = cache.load(league_key)
        if snapshot is None:
            print("No ranking snapshot found. Run scripts/fetch_rankings.py first.")
            return 1

        with YahooClient(settings) as client:
            league = client.league(league_key)
            teams = client.teams(league_key)
            picks = client.draft_results(league_key)
        state = DraftState(league=league, teams=teams)

    if not picks:
        print(f"{league.name} has no draft results yet (status: {league.draft_status}).")
        return 1

    if args.dump:
        record = record_from_live(league, teams, picks, snapshot_ref=league.league_key)
        path = save_record(record, Path(args.dump), anonymize=not args.keep_names)
        print(f"Draft record written to {path}")

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
        "  actually lasted as long as it predicted.\n\n"
        "  For the quantitative version, run scripts/backtest.py against a record\n"
        "  written with --dump."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
