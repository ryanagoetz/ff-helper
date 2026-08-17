#!/usr/bin/env python3
"""Decide which players to keep.

    uv run python scripts/evaluate_keepers.py --offline data/league-bustamove.yaml \
        --candidates data/keeper-candidates.csv

A keeper decision is not "who is best". It is "who is worth more than he costs", and the
cost is denominated differently in each format:

**Auction.** Cost is a salary. Keeping a player at $12 who is worth $40 is $28 of surplus,
and that surplus is the whole reason to keep anyone -- a stud kept at market price is
worth exactly as much as buying him in the room, minus the flexibility you gave up.

**Snake.** Cost is a forfeited pick. Keeping a player in round 6 is only good if he is
better than whoever you could have taken at 6, so the comparison is against the player
his ADP says would still be there, not against the field.

The second-order effect matters too, and is why this reads the same par values the draft
board uses rather than a separate sheet. Every dollar spent on keepers leaves the room,
which deflates what everyone else costs. Keep $150 of salary across the league and the
players you did not keep get cheaper -- so an expensive keeper has to clear not just his
own price but the discount you forgo elsewhere.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ff_helper import offline  # noqa: E402
from ff_helper.config import load_settings  # noqa: E402
from ff_helper.engine import replacement  # noqa: E402
from ff_helper.engine.auction import compute_par_values  # noqa: E402
from ff_helper.rankings import cache  # noqa: E402
from ff_helper.rankings.blend import PlayerValuation, blend  # noqa: E402
from ff_helper.rankings.players import PlayerRegistry, SourceRow  # noqa: E402
from ff_helper.rankings.sources import yahoo_adp  # noqa: E402
from ff_helper.yahoo.models import League  # noqa: E402

_PLAYER_COLUMNS = ("player", "name", "player_name")
_COST_COLUMNS = ("cost", "salary", "price", "auction_cost", "keeper_cost")
_ROUND_COLUMNS = ("round", "round_cost", "pick")


class CandidateError(ValueError):
    pass


@dataclass
class Candidate:
    name: str
    cost: float | None
    round: int | None
    valuation: PlayerValuation | None = None
    par: float | None = None
    vor: float | None = None
    surplus: float | None = None
    benchmark: str = ""
    value_rank: int | None = None
    adp_rank: int | None = None

    @property
    def market_gap(self) -> int | None:
        """Places the room ranks him above your projections. Positive = they like him more."""
        if self.value_rank is None or self.adp_rank is None:
            return None
        return self.value_rank - self.adp_rank


def _column(row: dict, candidates: tuple[str, ...]) -> str | None:
    for key, value in row.items():
        if key and key.strip().lower().replace(" ", "_") in candidates:
            cleaned = (value or "").strip()
            if cleaned:
                return cleaned
    return None


def read_candidates(path: Path) -> list[Candidate]:
    if not path.exists():
        raise CandidateError(f"Candidate file not found: {path}")

    rows: list[Candidate] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if not any((value or "").strip() for value in row.values()):
                continue
            name = _column(row, _PLAYER_COLUMNS)
            if not name:
                continue
            raw_cost = _column(row, _COST_COLUMNS)
            raw_round = _column(row, _ROUND_COLUMNS)
            try:
                cost = float(raw_cost.replace("$", "").replace(",", "")) if raw_cost else None
                keeper_round = int(float(raw_round)) if raw_round else None
            except ValueError as exc:
                raise CandidateError(f"{name}: could not read cost/round ({exc})") from exc
            rows.append(Candidate(name=name, cost=cost, round=keeper_round))

    if not rows:
        raise CandidateError(
            f"{path} produced no candidates. Expected a header with one of "
            f"{', '.join(_PLAYER_COLUMNS)} plus {', '.join(_COST_COLUMNS)} or "
            f"{', '.join(_ROUND_COLUMNS)}."
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", type=Path, metavar="CONFIG", help="Offline league YAML.")
    parser.add_argument("--league", help="League key, when running against the Yahoo API.")
    parser.add_argument(
        "--candidates",
        type=Path,
        required=True,
        help="CSV of players you could keep. Columns: player,cost (auction) or "
        "player,round (snake).",
    )
    parser.add_argument(
        "--slots",
        type=int,
        help="How many players your league lets you keep. Without it, every player with "
        "positive surplus is treated as keepable.",
    )
    args = parser.parse_args()

    if args.offline:
        try:
            config = offline.load_config(args.offline)
        except offline.OfflineConfigError as exc:
            print(f"FAILED: {exc}")
            return 1
        league: League = config.league
    else:
        settings = load_settings()
        league_key = args.league or settings.league_key
        if not league_key:
            print("No league. Pass --offline CONFIG or --league KEY.")
            return 1
        from ff_helper.yahoo.client import YahooClient

        with YahooClient(settings) as client:
            league = client.league(league_key)

    if league.settings is None:
        print("League settings unavailable; cannot value keepers.")
        return 1

    snapshot = cache.load(league.league_key)
    if snapshot is None:
        print(f"No snapshot for {league.league_key}. Run fetch_rankings.py first.")
        return 1

    try:
        candidates = read_candidates(args.candidates)
    except CandidateError as exc:
        print(f"FAILED: {exc}")
        return 1

    registry = PlayerRegistry(snapshot.players)
    rows = list(snapshot.rows) + yahoo_adp.from_players(snapshot.players)
    grouped, _ = registry.crosswalk(rows)
    valuations = blend(registry, grouped, league.settings)
    all_values = list(valuations.valuations.values())
    levels = replacement.compute(all_values, league.settings, league.num_teams)

    # Par values with nobody kept: the baseline a keeper's price is judged against.
    dollars = compute_par_values(all_values, levels, league.settings, league.num_teams)

    # Where the room's opinion and your projections disagree, a keeper price can be a
    # bargain against the market even when it is not against your own numbers -- and the
    # room is who you would have to outbid to get him back.
    by_value = sorted(
        (v for v in all_values if v.position in {"QB", "RB", "WR", "TE"}),
        key=lambda v: -dollars.value_of(v.player_key),
    )
    value_rank = {v.player_key: index + 1 for index, v in enumerate(by_value)}
    by_adp = sorted(by_value, key=lambda v: v.adp)
    adp_rank = {v.player_key: index + 1 for index, v in enumerate(by_adp)}

    by_key = valuations.valuations
    for candidate in candidates:
        probe = SourceRow(name=candidate.name, position="", team="", source="csv")
        player = registry.find(probe) or registry.find_fuzzy(probe)[0]
        if player is None:
            continue
        candidate.valuation = by_key.get(player.player_key)
        if candidate.valuation is None:
            continue
        candidate.vor = levels.vor(candidate.valuation)
        candidate.par = dollars.value_of(player.player_key)
        candidate.value_rank = value_rank.get(player.player_key)
        candidate.adp_rank = adp_rank.get(player.player_key)

    unmatched = [c for c in candidates if c.valuation is None]

    if league.settings.is_auction:
        _report_auction(candidates, league, args.slots)
    else:
        _report_snake(candidates, all_values, levels, league, args.slots)

    if unmatched:
        print(f"\n{len(unmatched)} candidates could not be valued:")
        for candidate in unmatched:
            print(f"  {candidate.name}")
        print("  Check the spelling, or that the snapshot covers them.")

    return 0


def _report_auction(
    candidates: list[Candidate], league: League, slots: int | None = None
) -> None:
    priced = [c for c in candidates if c.valuation is not None and c.par is not None]
    for candidate in priced:
        if candidate.cost is not None:
            candidate.surplus = candidate.par - candidate.cost
    priced.sort(key=lambda c: -(c.surplus if c.surplus is not None else -1e9))

    print(f"\n{league.name} -- auction keepers, ${league.settings.auction_budget} budget\n")
    print(f"  {'Player':24s} {'Pos':4s} {'Worth':>8s} {'Cost':>7s} {'Surplus':>9s}  Verdict")
    print("  " + "-" * 72)
    for candidate in priced:
        value = candidate.valuation
        cost = f"${candidate.cost:,.0f}" if candidate.cost is not None else "--"
        if candidate.surplus is None:
            surplus, verdict = "--", "no keeper price given"
        else:
            surplus = f"${candidate.surplus:+,.0f}"
            verdict = _auction_verdict(candidate)
        estimated = " *" if value.points_estimated else ""
        print(
            f"  {value.name[:24]:24s} {value.position:4s} ${candidate.par:7,.0f} "
            f"{cost:>7s} {surplus:>9s}  {verdict}{estimated}"
        )

    positive = [c for c in priced if c.surplus is not None and c.surplus > 0]
    # Surplus is additive and the slots do not interact, so the best set of N is simply
    # the N largest surpluses. Budget is the only coupling, and it is reported below.
    keepers = positive[:slots] if slots else positive

    if keepers:
        spend = sum(c.cost for c in keepers)
        gained = sum(c.surplus for c in keepers)
        budget = league.settings.auction_budget
        spots_left = league.settings.roster_size - len(keepers)
        max_bid = budget - spend - (spots_left - 1)

        header = f"best {len(keepers)} of {len(positive)} with positive surplus"
        print(f"\n  Keep ({header}):")
        for candidate in keepers:
            print(
                f"    {candidate.valuation.name:24s} ${candidate.cost:,.0f} "
                f"-> ${candidate.par:,.0f}  (${candidate.surplus:+,.0f})"
            )
        print(
            f"\n  ${spend:,.0f} of ${budget} committed, ${budget - spend:,.0f} left for "
            f"{spots_left} spots.\n  Opening max bid ${max_bid:,.0f}. "
            f"${gained:,.0f} of value gained over buying them back at par."
        )

        if slots and len(positive) > slots:
            missed = positive[slots:]
            print(
                f"\n  Not kept, despite positive surplus (only {slots} slots): "
                + ", ".join(f"{c.valuation.name} (${c.surplus:+,.0f})" for c in missed[:5])
            )
        if spend > budget * 0.5:
            print(
                "\n  That is over half the budget on your keepers. Defensible when the "
                "surplus is\n  this large, but it leaves the rest of the roster to be "
                "bought cheaply -- and\n  everyone else's money is still chasing the "
                "players you did not keep."
            )

    unpriced = [c for c in priced if c.cost is None]
    if unpriced:
        print("\n  Worth pricing -- you did not give a salary, but they have value:")
        for candidate in sorted(unpriced, key=lambda c: -(c.par or 0)):
            print(f"    {candidate.valuation.name:24s} worth ${candidate.par:,.0f}")

    # A cheap keeper the room rates far above your projections is the one call this tool
    # cannot make for you: your numbers say let him go, and the draft room says you would
    # have to pay up to get him back.
    contested = [
        c
        for c in priced
        if c.cost is not None
        and (c.market_gap or 0) >= 40
        and c.surplus is not None
        and c.surplus <= 0
    ]
    if contested:
        print("\n  Your projections and the draft room disagree about these:")
        for candidate in sorted(contested, key=lambda c: -(c.market_gap or 0)):
            print(
                f"    {candidate.valuation.name:22s} ${candidate.cost:,.0f}  "
                f"your value rank {candidate.value_rank}, ADP rank {candidate.adp_rank}"
            )
        print(
            "    4for4 projects them below what the room will pay. If you trust the room\n"
            "    over the projection, the keeper price is a bargain; if you trust the\n"
            "    projection, letting them go is right. No auction values in the export, so\n"
            "    this is a rank comparison rather than a dollar one."
        )

    print("\n  * projection interpolated from consensus rank rather than a stat line.")


def _auction_verdict(candidate: Candidate) -> str:
    surplus = candidate.surplus or 0.0
    if surplus >= 20:
        return "keep -- large surplus"
    if surplus >= 8:
        return "keep"
    if surplus >= 0:
        return "marginal; buying him back is nearly as good"
    return "let him go -- costs more than he is worth"


def _report_snake(
    candidates: list[Candidate],
    all_values: list[PlayerValuation],
    levels: replacement.ReplacementLevels,
    league: League,
    slots: int | None = None,
) -> None:
    """Judge a keeper against the player his forfeited pick would otherwise buy."""
    ordered = sorted(all_values, key=lambda v: v.adp)
    matched = [c for c in candidates if c.valuation is not None]

    for candidate in matched:
        if candidate.round is None:
            continue
        pick = (candidate.round - 1) * league.num_teams + 1
        # The best VOR still on the board around that pick, by ADP.
        pool = [v for v in ordered if v.adp >= pick]
        alternative = max(pool, key=levels.vor) if pool else None
        if alternative is None:
            continue
        candidate.surplus = levels.vor(candidate.valuation) - levels.vor(alternative)
        candidate.benchmark = f"{alternative.name} (ADP {alternative.adp:.0f})"

    matched.sort(key=lambda c: -(c.surplus if c.surplus is not None else -1e9))

    print(f"\n{league.name} -- snake keepers, {league.num_teams} teams\n")
    print(f"  {'Player':22s} {'Pos':4s} {'Rd':>3s} {'VOR':>7s} {'Gain':>7s}  Instead of")
    print("  " + "-" * 76)
    for candidate in matched:
        value = candidate.valuation
        keeper_round = str(candidate.round) if candidate.round else "--"
        gain = f"{candidate.surplus:+.0f}" if candidate.surplus is not None else "--"
        print(
            f"  {value.name[:22]:22s} {value.position:4s} {keeper_round:>3s} "
            f"{candidate.vor:7.0f} {gain:>7s}  {candidate.benchmark}"
        )
    print(
        "\n  Gain is points over the best player his ADP says would still be there at "
        "that pick.\n  Positive means keeping him beats drafting the spot."
    )


if __name__ == "__main__":
    raise SystemExit(main())
