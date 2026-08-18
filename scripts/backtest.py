#!/usr/bin/env python3
"""Score the engine against a recorded draft: hits, calibration, counterfactual.

    python scripts/backtest.py --file data/drafts/2025-league.json
    python scripts/backtest.py --file data/drafts/2025-league.json --time
    python scripts/backtest.py --file data/drafts/2025-league.json --predictor analytic

Runs entirely offline from a draft record (see ``scripts/replay.py --dump``) plus the
cached ranking snapshot. Three sections:

1. **Hits** -- at each of my turns, was my actual pick on the engine's short list?
   Low-stakes color; disagreement is expected.
2. **Calibration** -- Brier score and reliability table for the survival probabilities.
   This is the number that tunes the model. 0.25 is "always say fifty-fifty"; lower is
   better, and the reliability bins show *where* it is wrong.
3. **Counterfactual** -- the roster the engine would have drafted versus the one you
   actually did, and versus naive best-VOR. The end-to-end answer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ff_helper.assistant import Assistant  # noqa: E402
from ff_helper.backtest import calibration, counterfactual  # noqa: E402
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
    record: DraftRecord, snapshot: Snapshot, *, limit: int, timing: bool, predictor: str
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
        print("\nCalibration and counterfactual replay are snake-only; done.")
        return

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
        "--predictor",
        choices=sorted(PREDICTORS),
        default="analytic",
        help="Survival model to calibrate",
    )
    args = parser.parse_args()

    record = load_record(Path(args.file))
    league_key = record.snapshot_ref or record.league.league_key
    snapshot = cache.load(league_key, path=Path(args.snapshot) if args.snapshot else None)
    if snapshot is None:
        print(
            f"No ranking snapshot found for {league_key}. Run scripts/fetch_rankings.py, "
            "or point --snapshot at one."
        )
        return 1

    run(record, snapshot, limit=args.limit, timing=args.time, predictor=args.predictor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
