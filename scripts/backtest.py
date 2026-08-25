#!/usr/bin/env python3
"""Score the engine against a recorded draft: hits, calibration, counterfactual.

    uv run python scripts/backtest.py --file data/drafts/2025-league.json
    uv run python scripts/backtest.py --file data/drafts/2025-league.json --time
    uv run python scripts/backtest.py --file data/drafts/2025-league.json --predictor analytic

Runs entirely offline from a draft record (see ``scripts/replay.py --dump``) plus the
cached ranking snapshot. Three sections:

1. **Hits** -- at each of my turns, was my actual pick on the engine's short list?
   Low-stakes color; disagreement is expected.
2. **Calibration** -- Brier score and reliability table for the survival probabilities.
   This is the number that tunes the model. 0.25 is "always say fifty-fifty"; lower is
   better, and the reliability bins show *where* it is wrong. Snake only: "does he last
   until my next pick" presumes a pick order, and an auction has none.
3. **Counterfactual** -- the roster the engine would have drafted versus the one you
   actually did, and versus a naive baseline. The end-to-end answer. Runs for auctions
   too, at recorded prices; ``--follow-from N`` replays history through pick N and hands
   over after, which is how you ask whether the advice digs you out of a hole rather
   than watching a greedy policy spend everything on the first three sales.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ff_helper.assistant import Assistant  # noqa: E402
from ff_helper.backtest import calibration, counterfactual, nominations  # noqa: E402
from ff_helper.backtest.capture import DraftRecord, build_state, load_record  # noqa: E402
from ff_helper.rankings import cache  # noqa: E402
from ff_helper.rankings.cache import Snapshot  # noqa: E402

PREDICTORS: dict[str, calibration.Predictor] = {
    "analytic": calibration.analytic_predictor,
    "mc": calibration.mc_predictor,
}


def _fresh_assistant(record: DraftRecord, snapshot: Snapshot) -> Assistant:
    league, state = build_state(record)
    return Assistant.build(league, state, snapshot)


def run(
    record: DraftRecord,
    snapshot: Snapshot,
    *,
    limit: int,
    timing: bool,
    predictor: str,
    follow_from: int | None = None,
    stop_after: int | None = None,
    display_limit: int = 8,
    nominate: bool = False,
    nomination_limit: int = 5,
) -> None:
    my_team = record.my_team
    print(
        f"Backtesting {record.league.name} -- {len(record.picks)} picks, "
        f"{record.league.num_teams} teams, "
        f"{'auction' if record.is_auction else 'snake'}"
    )
    if my_team is None:
        print("Record does not identify my team; nothing to compare against.")
        return

    # -- hits ----------------------------------------------------------------------
    reports = calibration.turn_reports(
        _fresh_assistant(record, snapshot), list(record.picks), limit=limit
    )
    top_hits = sum(1 for r in reports if r.match_rank == 1)
    in_list = sum(1 for r in reports if r.match_rank is not None)
    print(
        f"\nHits: engine #1 matched {top_hits}/{len(reports)} of my picks; "
        f"{in_list}/{len(reports)} were in its top {limit}"
    )

    if timing and reports:
        times = sorted(r.elapsed for r in reports)
        mean = sum(times) / len(times)
        print(
            f"Timing: recommendations() mean {mean * 1000:.0f} ms, "
            f"max {times[-1] * 1000:.0f} ms over {len(times)} turns"
        )

    if record.is_auction:
        # Survival calibration has no auction meaning -- "does he last until my next
        # pick" presumes a pick order, and in an auction everyone is biddable always.
        # The counterfactual does translate, so run that and say what was skipped.
        print("\nSurvival calibration is snake-only (an auction has no 'next pick').")
        if follow_from is not None:
            print(f"  (history replayed verbatim through pick {follow_from}, policy after)")
        if stop_after is not None:
            print(
                f"  (compared through pick {stop_after}; every policy including 'actual' "
                "fills the rest from the leftovers)"
            )
        print("\nCounterfactual rosters (my buys made by each policy):")
        print(f"  (short list = {display_limit} rows, matching the app)")
        print(f"  {'policy':<10} {'lineup pts':>10} {'roster VOR':>11} {'spent':>7} {'slots':>6}")
        for policy in counterfactual.AUCTION_POLICIES:
            result = counterfactual.auction_counterfactual(
                record,
                snapshot,
                policy=policy,
                display_limit=display_limit,
                follow_from=follow_from,
                stop_after=stop_after,
            )
            print(
                f"  {policy:<10} {result.lineup_points:10.1f} "
                f"{result.total_vor:11.1f} {result.spent:7d} {len(result.players):6d}"
            )
        print("  (prices held at what they actually were -- see counterfactual.py)")
        if nominate:
            print()
            report = nominations.nomination_report(
                record, snapshot, limit=nomination_limit
            )
            print(nominations.format_report(report, limit=nomination_limit))
        return

    if stop_after is not None:
        print("\n--stop-after is auction-only; ignoring it for this snake record.")
    if nominate:
        print("\n--nominations is auction-only; a snake draft has no nominations.")

    # -- calibration ---------------------------------------------------------------
    report = calibration.survival_calibration(
        _fresh_assistant(record, snapshot),
        list(record.picks),
        predictor=PREDICTORS[predictor],
    )
    print(
        f"\nSurvival calibration ({predictor}): "
        f"Brier {report.brier:.4f} over {report.n} predictions"
    )
    print("  predicted   observed    n")
    for mean_predicted, observed, count in report.bins:
        print(f"    {mean_predicted:6.2f}     {observed:6.2f}   {count:5d}")
    print("  by position:")
    for position, (brier, count) in report.by_position.items():
        print(f"    {position:<4} Brier {brier:.4f}  (n={count})")

    # -- counterfactual ------------------------------------------------------------
    print("\nCounterfactual rosters (my picks made by each policy):")
    print(f"  {'policy':<10} {'lineup pts':>10} {'roster VOR':>11} {'roster pts':>11}")
    for policy in counterfactual.POLICIES:
        result = counterfactual.counterfactual(record, snapshot, policy=policy)
        print(
            f"  {policy:<10} {result.lineup_points:10.1f} "
            f"{result.total_vor:11.1f} {result.total_points:11.1f}"
        )
    print("  (lineup pts = best legal starting lineup; the number that decides games)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="Draft record written by replay.py --dump")
    parser.add_argument("--snapshot", help="Path to a ranking snapshot (default: cached by league)")
    parser.add_argument("--limit", type=int, default=3, help="Short-list size for the hit summary")
    parser.add_argument("--time", action="store_true", help="Report recommendation latency")
    parser.add_argument(
        "--display-limit",
        type=int,
        default=8,
        help=(
            "Auction only: how many rows the engine_list policy may buy from. Defaults to "
            "8 to match the app's own short list (Assistant.recommendations). Distinct "
            "from --limit, which sizes the snake hit summary -- reusing that here graded "
            "the ranking change against a 3-row display no user ever sees, and the "
            "engine_list verdict inverts between 3 and 8."
        ),
    )
    parser.add_argument(
        "--follow-from",
        type=int,
        help=(
            "Auction only: replay history verbatim through this pick, then hand over to "
            "the policy. Without it a greedy policy spends its whole budget on the first "
            "few sales and never reaches the endgame where advice matters."
        ),
    )
    parser.add_argument(
        "--stop-after",
        type=int,
        help=(
            "Auction only: stop bidding for me after this pick and fill the rest of the "
            "roster from the leftovers -- for 'actual' too. Use it to cut a record where "
            "the real drafter stopped deciding (autopick, walked away), whose buys past "
            "that point are not a decision any policy can be measured against."
        ),
    )
    parser.add_argument(
        "--nominations",
        action="store_true",
        help=(
            "Auction only: grade the nomination model's predictions against the record. "
            "Calibration, not a counterfactual -- the record has no nominator field and "
            "prices are frozen, so no nomination order has a modelled consequence."
        ),
    )
    parser.add_argument(
        "--nomination-limit",
        type=int,
        default=5,
        help="How many nominations to name before each sale when grading (default 5).",
    )
    parser.add_argument(
        "--predictor",
        choices=sorted(PREDICTORS),
        default="analytic",
        help="Survival model to calibrate",
    )
    args = parser.parse_args()

    # This script never calls load_settings(), so .env (FF_HELPER_HOME, FF_MC_ROLLOUTS)
    # would otherwise be silently ignored by the very tool that measures those knobs.
    load_dotenv()

    # One report, one engine. Every Assistant this run builds -- turn reports and the
    # three counterfactual replays included -- follows --predictor, instead of half the
    # report obeying whatever FF_MC_ROLLOUTS happened to be in the caller's shell.
    if args.predictor == "mc":
        from ff_helper import config

        rollouts = config.mc_rollouts() or 300
    else:
        rollouts = 0
    os.environ["FF_MC_ROLLOUTS"] = str(rollouts)
    print(f"Engine: {'Monte Carlo, ' + str(rollouts) + ' rollouts' if rollouts else 'analytic'}")

    record = load_record(Path(args.file))
    league_key = record.snapshot_ref or record.league.league_key
    snapshot = cache.load(league_key, path=Path(args.snapshot) if args.snapshot else None)
    if snapshot is None:
        print(
            f"No ranking snapshot found for {league_key}. Run scripts/fetch_rankings.py, "
            "or point --snapshot at one."
        )
        return 1

    run(
        record,
        snapshot,
        limit=args.limit,
        timing=args.time,
        predictor=args.predictor,
        follow_from=args.follow_from,
        stop_after=args.stop_after,
        display_limit=args.display_limit,
        nominate=args.nominations,
        nomination_limit=args.nomination_limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
