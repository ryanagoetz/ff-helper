"""Is the nomination model's picture of the room true? Calibration, never a counterfactual.

``counterfactual.py`` asks what roster the engine would have drafted. That question cannot
be asked of a nomination policy, and the reason is structural rather than a gap somebody
forgot to fill:

* the draft record carries a **buyer**, a price and a sale ordinal, and no nominator --
  Yahoo publishes completed sales, not who put a name on the block, so there is no recorded
  decision to compare a policy against; and
* ``auction_counterfactual`` **holds prices at what they actually were**, by design. A
  different nomination order changes the whole price surface, which is exactly the thing it
  declines to model -- so even with a nominator field, reordering nominations would have no
  modelled consequence to measure.

Bumping ``RECORD_VERSION`` to add a nominator would not fix either half. Nothing publishes
the field, no record on disk could be backfilled, and the bump would refuse every record
this repo has -- including the only auction. If the draft-room bridge ever learns to read
the nomination banner, that is when it earns itself.

So this grades something narrower and real: the model makes **falsifiable claims about the
room**, and the record can check them. Sales are replayed in recorded order at recorded
prices, exactly as ``policy="actual"`` does, so my roster and everyone's budgets follow
history and no counterfactual is asserted anywhere. Before each sale the live board
produces a nomination list, and each named candidate is scored against what the record
shows actually happened to him:

``rival_bought``
    A *drain* candidate should be bought by somebody else. If I end up owning him, the
    model told me to drain a player I actually wanted.
``price_held``
    His price should reach what I would have paid. If he went for less, the nomination
    handed a rival a bargain.
``buyer_named``
    The recorded buyer should be in the ``live_bidders`` set. This is the sharp one: the
    set is a named list of teams derived from per-team rosters and budgets, and the record
    names the buyer, so it grades the machinery this whole model adds and nothing else.
``bargain_held``
    A *bargain* candidate should sell at or below my bid. Above it, the claim that nobody
    could reach him was wrong.

Every rate is printed beside a null baseline, because a good-looking number means nothing
without one -- ``buyer_named`` against "any team with a hole at that position" and against
"the deepest pocket in the room", ``price_held`` against the same statistic over every
available player at that moment. A model that cannot beat both is not carrying information,
and the report says so in those words.

It does **not** fit ``_STUCK_DECAY``, and cannot be made to. That constant is the chance
*nobody bids at all*, and a draft record holds only completed sales -- every player in one
was sold to somebody, so the event never appears. The report prints how often I turned out
to be the buyer, bucketed by live bidders named, which is a different question, and says so
where it prints it.

Each player is graded **once**, on the first list that names him. A candidate can sit in the
top five for dozens of consecutive sales against a single fixed outcome, and re-grading him
each time turns one fact into dozens of identical trials: measured before the guard, 417
rows came from 34 distinct players, one of them 27 times, so every rate was really measuring
how long a player lingered.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ff_helper.assistant import Assistant
from ff_helper.backtest.capture import DraftRecord, build_state

# Shared rather than restated: a constant whose comment says "must match that other
# constant" is the case an import solves.
from ff_helper.backtest.counterfactual import _WHOLE_POOL  # noqa: E402
from ff_helper.engine.auction import _dollars
from ff_helper.engine.nomination import (
    _MIN_DRAIN_PRICE,
    MOTIVE_BARGAIN,
    MOTIVE_DRAIN,
    open_positions,
)
from ff_helper.rankings.cache import Snapshot
from ff_helper.yahoo.models import DraftPick

# How much recall a tighter bidder set may give up and still count as the better answer.
# Judgement, like everything else in this model, but a bounded one: the alternative is
# demanding exact equality, which no real record hits.
_RECALL_TOLERANCE = 0.05


@dataclass
class Tally:
    """A yes/no prediction and how often it held."""

    hits: int = 0
    total: int = 0

    def add(self, held: bool) -> None:
        self.total += 1
        self.hits += 1 if held else 0

    @property
    def rate(self) -> float:
        return self.hits / self.total if self.total else 0.0

    def __str__(self) -> str:
        return f"{self.hits:4d}/{self.total:<4d} {self.rate * 100:5.1f}%"


@dataclass
class NominationReport:
    named: int = 0
    drains: int = 0
    bargains: int = 0
    rival_bought: Tally = field(default_factory=Tally)
    price_held: Tally = field(default_factory=Tally)
    buyer_named: Tally = field(default_factory=Tally)
    bargain_held: Tally = field(default_factory=Tally)
    # Baselines, same denominators as the predictions they shadow.
    baseline_any_open: Tally = field(default_factory=Tally)
    baseline_deepest: Tally = field(default_factory=Tally)
    baseline_price: Tally = field(default_factory=Tally)
    # Mean size of the named set against the position-only set. Recall alone cannot grade
    # the money half of the gate, because ``live_bidders`` is a *subset* of "anyone with a
    # hole there" -- adding a constraint can only ever hold recall level or lose it. What
    # it buys is precision, so the sizes have to be reported next to the rates or the
    # comparison is rigged against the thing being measured.
    named_size: int = 0
    open_size: int = 0
    # How often I turned out to be the buyer, bucketed by live bidders named.
    mine_by_bidders: dict[int, Tally] = field(default_factory=dict)

    @property
    def informative(self) -> bool:
        """Whether the named bidder set carries information the naive answers do not.

        The comparison that is *not* here is recall against the position-only set. That set
        is a superset by construction, so the money gate can only hold recall or lose it,
        and demanding ``>=`` made exact equality the sole passing case -- one dropped buyer
        in 417 flipped the report to "rework 'aim' and the max_bid filter", printed three
        lines under the text saying a smaller set at the same recall is a better one.

        What the gate actually buys is precision, so that is what is tested: the named set
        must keep most of the recall while being genuinely smaller, and it must beat naming
        the single richest team. ``_RECALL_TOLERANCE`` is how much recall a tighter set may
        trade away before it stops being a better answer.
        """
        if self.buyer_named.total <= 0 or self.named_size <= 0:
            return False
        tighter = self.named_size < self.open_size
        keeps_recall = self.buyer_named.rate >= self.baseline_any_open.rate - _RECALL_TOLERANCE
        return (
            keeps_recall
            and (tighter or self.buyer_named.rate >= self.baseline_any_open.rate)
            and self.buyer_named.rate > self.baseline_deepest.rate
        )


def _open_positions(state, team_key: str, position_of: dict[str, str], settings) -> set[str]:
    """Positions a team could still start someone at -- the ``any open slot`` baseline.

    Delegates to the engine's ``open_positions`` rather than repeating the
    ``assign_lineup``-to-needs-set derivation. The baseline has to be computed exactly the
    way the model computes it, or the comparison grades two different definitions of "open"
    against each other and the difference reads as signal.
    """
    return open_positions(state.roster_counts(team_key, position_of), settings)


def nomination_report(
    record: DraftRecord,
    snapshot: Snapshot,
    *,
    limit: int = 5,
) -> NominationReport:
    """Replay the record and grade every nomination the model would have named.

    Nothing here changes a sale. The board is rebuilt exactly as it happened and the
    nomination list is consulted as a *bystander* before each one, which is what keeps this
    honest: there is no policy, no interception, and no claim about a draft that did not
    occur.
    """
    my_team = record.my_team
    if my_team is None:
        raise ValueError("Record does not identify my team; nothing to grade.")
    if not record.is_auction:
        raise ValueError("Nomination grading is for auctions; a snake draft has no nominations.")
    settings = record.league.settings
    assert settings is not None  # enforced by DraftRecord construction

    league, state = build_state(record)
    assistant = Assistant.build(league, state, snapshot)
    position_of = assistant.position_of

    # Where each player ended up, so a candidate named at sale 30 can be checked against
    # the sale that eventually resolved him.
    outcome: dict[str, DraftPick] = {
        pick.player_key: pick for pick in record.picks if pick.cost is not None
    }

    report = NominationReport()
    # A candidate can sit on the list for dozens of consecutive sales, and his outcome is
    # fixed -- so re-grading him each time turns one fact into dozens of identical "trials".
    # Measured before this guard: 417 graded rows came from 34 distinct players, one of them
    # 27 times, and every rate in the report was really a measure of how long a player
    # lingered. Each player is graded once, on the first list that names him.
    graded: set[str] = set()
    # The baseline needs its own first-appearance set for the same reason the model does:
    # printed side by side, one deduped rate and one lingering-weighted rate are not the
    # same statistic. Measured before this guard: price_held 21/31 against baseline_price
    # 1788/2497, the baseline inflated because the players who survive many sales are cheap
    # and clear a low bid_to almost always -- so the model appeared to lose to its own null.
    baselined: set[str] = set()
    for pick in sorted(record.picks):
        candidates = assistant.nomination_list(limit=limit)
        if candidates:
            # The price baseline, over every available player rather than the named few.
            # "His price reached what I would have paid" is only evidence about the
            # *nomination* if it beats how often that is true of the board at large.
            #
            # Restricted to the same price floor the candidates clear. Without that the
            # baseline is dominated by dollar scrubs, whose recorded price clears a
            # ``bid_to`` of $1 essentially always -- it measured 93.2% against the named
            # drains' 83.6% and read as the model doing badly, when the two statistics were
            # not about the same population at all.
            for recommendation in assistant.auction_recommendations(limit=_WHOLE_POOL):
                key = recommendation.valuation.player_key
                price = recommendation.budget_price
                if price is None or key in baselined:
                    continue
                if max(_dollars(price), recommendation.bid_to) < _MIN_DRAIN_PRICE:
                    continue
                sale = outcome.get(key)
                if sale is not None:
                    baselined.add(key)
                    report.baseline_price.add(sale.cost >= recommendation.bid_to)
        for candidate in candidates:
            if candidate.player_key in graded:
                continue
            sale = outcome.get(candidate.player_key)
            if sale is None:
                # Never sold at a recorded price -- a keeper folded into draftresults, or a
                # player who went undrafted. Neither is evidence about a nomination.
                continue
            graded.add(candidate.player_key)
            _grade(report, candidate, sale, my_team.team_key, state, position_of, settings)
        state.apply_sync([pick], timestamp=0.0)

    return report


def _grade(report, candidate, sale, my_key, state, position_of, settings) -> None:
    report.named += 1
    bought_by_rival = sale.team_key != my_key
    bidders = len(candidate.live_bidders)

    report.mine_by_bidders.setdefault(bidders, Tally()).add(not bought_by_rival)

    if candidate.motive == MOTIVE_DRAIN:
        report.drains += 1
        report.rival_bought.add(bought_by_rival)
        report.price_held.add(sale.cost >= candidate.recommendation.bid_to)
    elif candidate.motive == MOTIVE_BARGAIN:
        report.bargains += 1
        report.bargain_held.add(sale.cost <= candidate.recommendation.bid_to)

    # The sharp prediction: the model named a set of teams, and the record names the buyer.
    #
    # Compared by ``team_key``. Display names are opponent-authored, Yahoo does not make
    # them unique, and the old reverse-map fell back to a team *key* on an unresolvable
    # buyer and then tested it against a set of *names* -- a guaranteed silent miss that
    # dragged exactly the numbers ``informative`` is computed from.
    named = set(candidate.live_bidder_keys)
    report.buyer_named.add(sale.team_key in named)

    # Baseline one: any rival with a hole at that position, budget ignored. If this scores
    # as well, the ``max_bid`` half of the gate is doing nothing.
    open_anywhere = {
        team.team_key
        for team in state.teams
        if team.team_key != my_key
        and candidate.position in _open_positions(state, team.team_key, position_of, settings)
    }
    report.baseline_any_open.add(sale.team_key in open_anywhere)
    report.named_size += len(named)
    report.open_size += len(open_anywhere)

    # Baseline two: the single deepest pocket. If this scores as well, the *set* is doing
    # nothing that "whoever has the most money" would not.
    rivals = [team for team in state.teams if team.team_key != my_key]
    if rivals:
        richest = max(rivals, key=lambda team: state.max_bid(team.team_key))
        report.baseline_deepest.add(sale.team_key == richest.team_key)


def format_report(report: NominationReport, *, limit: int) -> str:
    """The report, with the honesty paragraph attached -- printed, not merely documented."""
    graded = max(1, report.buyer_named.total)
    lines = [
        f"Nomination retrospective -- top {limit} named before each sale",
        f"  distinct players   {report.named:6d}      "
        f"drain {report.drains}   bargain {report.bargains}",
        "  (one trial per player, graded on the first list that names him -- a candidate can",
        "   sit in the top few for dozens of sales against one fixed outcome, and counting",
        "   each appearance turned 34 players into 417 'trials' that were not independent)",
        f"  drain -> a rival   {report.rival_bought}",
        f"  price >= my bid    {report.price_held}"
        f"   (same floor, all available: {report.baseline_price.rate * 100:5.1f}%)",
        f"  bargain <= my bid  {report.bargain_held}",
        "",
        "  Whose money the model named, against two null answers:",
        f"    buyer in live set   {report.buyer_named}"
        f"   naming {report.named_size / graded:4.1f} teams",
        f"    any open slot       {report.baseline_any_open}"
        f"   naming {report.open_size / graded:4.1f} teams",
        f"    deepest pocket      {report.baseline_deepest}   naming  1.0 teams",
        "  A smaller set at the same recall is a better set: live_bidders is a subset of",
        "  'anyone with a hole there', so the money gate can only hold recall or lose it.",
    ]
    if not report.informative:
        lines += [
            "  WARNING: the named set did not clear both null answers. On this record the",
            "  bidder gate carries no information the naive answers do not -- rework 'aim'",
            "  and the max_bid filter before trusting the ordering.",
        ]
    lines += [
        "",
        "  How often I was the buyer, by live bidders named:",
    ]
    for bidders in sorted(report.mine_by_bidders):
        tally = report.mine_by_bidders[bidders]
        lines.append(f"    {bidders:2d} -> {tally.rate:.2f} (n={tally.total:4d})")
    lines += [
        "  This is NOT a fit for _STUCK_DECAY, and cannot be turned into one: that constant",
        "  is the chance *nobody bids at all*, and a record of completed sales contains no",
        "  such event -- every player in it was sold to somebody. What the rows above",
        "  measure is who won a contested player, which is a different question. Fitting",
        "  _STUCK_DECAY needs a draft where unsold nominations are recorded; until one is,",
        "  it stays unfitted judgement and the module docstring says so.",
        "",
        "  This grades the model's *predictions* against what happened. It cannot say a",
        "  different nomination order would have produced a better roster: the record has",
        "  no nominator field, and auction_counterfactual freezes prices by design, so no",
        "  nomination order has a modelled price consequence. Read it as calibration,",
        "  never as a counterfactual.",
    ]
    return "\n".join(lines)
