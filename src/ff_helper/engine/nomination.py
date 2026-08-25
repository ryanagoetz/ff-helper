"""Whose name to say next -- the other half of an auction.

``auction.py`` answers one question: where do my dollars go furthest right now. That is the
question you answer when someone *else* puts a name up. It is not the question you answer
when the room turns to you, and until this module existed the app had nothing to say there
at all (README's Limitations said so outright).

**A nomination is a timing decision, not a selection decision.** Every player gets
nominated eventually -- that is what an auction is. What you control is *when*, and
therefore *against whose remaining money*. That reframing is the whole design: the score is
not a ranking of players, it is an expectation over the two ways a nomination can resolve.

    score = p_stuck * stuck_value + (1 - p_stuck) * (drain_gain - capture_at_risk)

* **Someone bids.** A rival's dollars leave the room -- good, but only in proportion to how
  much of that money was aimed at positions I still need -- and I give up my claim on him,
  which is bad exactly to the degree I wanted him.
* **Nobody bids.** I own him at my opening bid. Good if I wanted him cheap; bad if he is a
  body burning a roster slot that a $1 filler at a position I actually need would have used
  better.

**How much this list can differ from the buy list depends entirely on the price source, and
on an unpriced board the answer is measurably "not much."** That is the most important thing
to know before reading a score here, and two earlier versions of this module claimed
otherwise:

* Charging a player's **full value** for nominating someone you want. Wrong by an order of
  magnitude -- a rival buying him at the going rate does not take his value from you, it
  takes your chance to buy him at that rate. Every expensive player scored deeply negative
  and the list collapsed onto the cheapest rows above the floor, recommending a $4
  quarterback, which is not a nomination at all.
* Zeroing the drain for the **top ``depth`` players at a position**. Competing backs are
  substitutes for one slot, so with a single open RB hole this protected exactly one back
  while the buy list still ranked five, and the other four rose to the top of the panel.
  Measured on the 2026 record at pick 40, that set held one name and nominations #2-#5 were
  buys #3, #4, #5 and #8.

Both mistakes priced the *player*. What a nomination resolves is the price of a **tier**, so
``_plan_pressure`` is positional and continuous -- no cut between adjacent players, and two
interchangeable receivers a cent apart cannot get opposite advice.

But it does not buy separation on this repo's own league, and the reason is structural
rather than a tuning failure. Measured on the 2026 record: **every** price on that board
comes from the ``room`` or ``own`` tier, never ``published`` or ``interpolated``, and the
top 20 by price and the top 20 by my own worth agree on **14.9 of 20** players. On an
unpriced board "what the room will pay" *is* "what I think he is worth" reordered by a
positional premium -- exactly what ``PriceBasis`` says the room tier is -- so the two lists
are ranking one signal and no weighting scheme can prise them apart. Mean overlap between
the top-6 nominations and the top-8 buys is **3.9 of 6**, and the positional term only bites
once your needs narrow, which on that record happens after the window where nominations are
still being graded (68 of 88 moments had all six positions open).

**So read this panel for what it independently knows, which is not the ordering.** The
``Aimed at`` column is the real product: who can actually bid, from per-team budgets and
per-team roster holes, which no cheat sheet has and which the retrospective grades at 75.0%
against 9.4% for "the richest team". The ordering is a genuine drain model that becomes
genuinely informative the moment a published price exists -- and until then it is close to
your own board, by construction, and this docstring would rather say so than pretend.

The cascade in ``_cascade_costs`` survives as the *second* cost, correctly scoped: what you
lose when a rival takes a player your plan was reaching for is one rung, the gap to the next
man, usually a dollar or two.

Two more places where a plausible number is the wrong one:

* ``_slot_floor``. A ``min_bid_marginal`` is not automatically positive -- an earlier note
  here claimed it was, and on the real board 296 of the 458 sub-$3 players are negative,
  because a full bench hands back no dollar. "Wasted slot" is measured against the $1 filler
  that slot would otherwise hold, and the floor is allowed to go negative with it. The two
  price bands usually tile, but a board can run out of pocket change, so the floor also
  carries a runner-up: no player is ever scored against a set containing himself.
* ``_held_value``. ``min_bid_marginal`` arrives **raw**, not through ``penalized``: that
  helper divides on negatives as a rank transform, and this quantity is read as money and
  summed with dollar terms. Penalized, a marginal at ``need`` 0.04 came back 25x magnified
  against a baseline measured at ``need`` 1.0.
* ``_rival_shape`` is slot-weighted rather than position-weighted; see its docstring.

Both terms are dollars to me, which is what lets the two nomination *motives* -- drain a
rival's budget, or steal a player the room cannot reach -- share one honest ranking instead
of two lists the human has to arbitrate between under a clock. The motive label on each row
is derived from which term actually dominates, so it can never disagree with the number
printed beside it.

**This is the only module in the repo whose objective is not my own value**, and that
justifies its shape twice over. It sits *above* ``auction.py`` and consumes
``AuctionRecommendation`` rather than re-deriving anything -- ``winprob.py`` under
``weekly.py`` is the precedent, and ``evaluate``'s docstring already argues the general
case: two prices for one player, five seconds apart, is worse than one price. And it is a
separate module behind a separately wrapped endpoint because the failures are asymmetric in
the way this repo usually cares about: the buy list is read on every one of ~160 sales, this
list about thirteen times. A bug here must never be able to change a bid.

**Which price each term reads, and why.** ``PriceBasis`` exists to keep three grades of
evidence apart, and a model about *other people's money* has to respect that split or it
reintroduces the circularity that class was written to kill:

===================  ================================  =========================================
term                 accessor                          why that one
===================  ================================  =========================================
``expected_price``   ``budget_price`` (= ``estimate``)  "how much money leaves the room" is a
                                                        budgeting question about a price *level*
``waste``            ``surplus`` (= ``market_price``)   "the room overpays for *him*" is an
                                                        individual claim; vanishes offline
``live_bidders``     ``DraftState.max_bid``             a hard fact off the board, compared
                                                        against a level
``stuck_value``      ``min_bid_marginal``               my own plan DP -- this term is *supposed*
                                                        to be my valuation
``capture_at_risk``  ``bid_to`` + the cascade gap       already a minimum over the hard ceiling,
                                                        the smart cap and the plan
===================  ================================  =========================================

**What this degrades to with no published prices**, which is the normal state of an offline
league (and of any live league once the last player Yahoo priced has been sold). Two
consequences, stated rather than papered over:

1. The drain list becomes "my best player at a position I do not need". That is still the
   right answer under no information -- the most expensive player you do not want *is* the
   best drain -- but it cannot find the player *this room* overvalues. That capability
   arrives with ``market_price`` and leaves with it, exactly as ``surplus`` does.
2. The bargain motive would be self-fulfilling, and is suppressed. ``live_bidders`` shrinks
   monotonically in the price, and with no external tier the price is my own par -- so
   "nobody can reach him" would fire hardest on precisely my own top players, recommending
   I put up the studs I most want to keep cheap. When ``price_basis`` is ``"own"`` the
   bargain term is clipped to its downside: keep the cost of getting stuck, refuse the
   upside claim. Failures are asymmetric and the code takes sides.

**``drain_share`` is derived, not tuned**, and must stay that way. When a rival spends $X,
``league_money_remaining`` falls by $X and one slot leaves the pool; through
``inflation_factor`` that deflation accrues to everyone still buying in proportion to what
they still spend, so my share of it is my share of the remaining money. This gets the right
behaviour for free and with no constant: early it is about 1/12 and draining barely matters;
late, if I hoarded while the room spent, it climbs and draining dominates -- which is
exactly when a real auction is won on nomination discipline. Broke, it goes to zero and the
list stops recommending drains, which is also correct. Do not simplify it into a weight.

**Every constant here is unfitted judgement** -- ``_STUCK_DECAY``, ``_CONTENTION_PREMIUM``,
``_WASTE_WEIGHT`` and ``_MIN_DRAIN_PRICE`` alike -- in the same sense ``_WEEKLY_CV`` is the
weakest thing in the in-season half. ``scripts/backtest.py --nominations`` grades the named
bidder set against the buyer the record actually names, which is the one claim here a record
can check.

``_STUCK_DECAY`` specifically **cannot be fitted from any record this repo can hold**, and
an earlier version of this paragraph wrongly said the backtest fits it. It is the chance
that *nobody bids at all*, and a draft record contains only completed sales -- every player
in it was sold to somebody, so the event the constant describes never appears. Fitting it
needs a source that records unsold nominations.

**Why the backtest cannot grade this, and what does instead.** ``auction_counterfactual``
freezes prices at what they actually were, and the draft record carries a buyer but no
nominator -- so a different nomination order has no modelled price consequence and no
recorded ground truth. There is no honest counterfactual here and this module does not
pretend to one. ``backtest/nominations.py`` grades the model's *predictions* against the one
recorded auction instead: did the drain targets sell to a rival, above what I would have
paid, to a team this model named as able to reach them. That is calibration, not a
counterfactual, and the report says so in those words every time it runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from ff_helper.engine.auction import AuctionRecommendation, _dollars
from ff_helper.engine.lineup import assign_lineup
from ff_helper.yahoo.models import LeagueSettings

# Chance that one rival holding both an open starting slot and the money declines to bid
# past $1 anyway. Compounds over the bidder set, so two able rivals leave a 12% chance the
# player sticks to me. Unfitted judgement, and *not* measurable from a draft record: this is
# the chance nobody bids, and a record holds only completed sales, so the event never
# appears in one. Fitting it needs a source that records unsold nominations.
_STUCK_DECAY = 0.35

# What a drained *contested* rival dollar is worth beyond my share of the diffuse price
# effect. The diffuse part is derived (``drain_share``); this is not. An auction price is
# set by the second-highest bidder, so knocking one contender out of a two-horse race is
# worth the whole spread rather than a twelfth of it. Judgement, and the weaker of the two.
_CONTENTION_PREMIUM = 2.0

# Extra credit for draining a player this room specifically overpays for, as a fraction of
# his price. Rides on ``surplus``, so it is exactly zero whenever ``surplus`` is None --
# there is no discontinuity offline, the term simply vanishes.
_WASTE_WEIGHT = 1.0

# Below this, nominating him is not a decision: he will still be there at $1 whenever you
# want him. Deliberately a separate constant from ``auction._PREMIUM_MIN_EXPECTED`` even
# though they start equal -- that one is a noise floor on a *ratio*, this one asks whether
# there is a choice here at all.
_MIN_DRAIN_PRICE = 3.0

MOTIVE_DRAIN = "drain"
MOTIVE_BARGAIN = "bargain"


@dataclass(frozen=True)
class RivalSeat:
    """One opponent as the *board* sees them -- facts only, no model.

    Copied out of ``DraftState`` under ``Assistant.lock`` so everything below runs outside
    it, which is this repo's rule for every engine call. Carrying raw ``roster_counts``
    rather than a derived need set is deliberate: what counts as a "need" is a modelling
    decision and it belongs here, not in the plumbing that holds the lock.
    """

    team_key: str
    name: str
    roster_counts: dict[str, int]
    max_bid: int  # DraftState.max_bid -- a hard constraint, not advice


@dataclass(frozen=True)
class NominationCandidate:
    """One name you could put up, and what saying it is worth."""

    recommendation: AuctionRecommendation
    motive: str
    score: float  # dollars to me
    expected_price: float  # what leaves the room if he sells
    live_bidders: tuple[str, ...]  # team names with both a hole at his position and the money
    aim: float  # [0, 1] share of the drained dollars aimed at positions I need
    drain: float  # contested rival dollars removed
    drain_gain: float  # those dollars converted to my dollars
    stuck_probability: float
    stuck_value: float  # worth of owning him at $1; negative means a wasted slot
    capture_at_risk: float  # surplus I forgo by resolving his price now
    reason: str
    # The same rivals as ``live_bidders``, by key. Display names are opponent-authored and
    # Yahoo does not make them unique, so they are not identity -- ``backtest/nominations.py``
    # grades the recorded buyer against this set, and comparing names there silently
    # mis-scored the report's sharpest metric.
    live_bidder_keys: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.recommendation.name

    @property
    def position(self) -> str:
        return self.recommendation.position

    @property
    def player_key(self) -> str:
        return self.recommendation.valuation.player_key


def open_positions(roster_counts: dict[str, int], settings: LeagueSettings) -> set[str]:
    """Positions a team could still start someone at.

    Thin wrapper over ``assign_lineup`` -- the same primitive ``_position_demand`` already
    runs per opponent on the snake path -- so a flex is never counted toward more than one
    position. ``starters_at`` would overcount here in exactly the way ``lineup.py`` exists
    to prevent.

    Public because ``backtest/nominations.py`` needs the identical derivation for its
    position-only baseline, and a second copy there would be a fourth in the repo.
    """
    open_dedicated, open_flex, _ = assign_lineup(roster_counts, settings)
    needs = {position for position, count in open_dedicated.items() if count > 0}
    for eligible, count in open_flex:
        if count > 0:
            needs |= set(eligible)
    return needs


def _slot_floor(recommendations: list[AuctionRecommendation]) -> _SlotFloor:
    """What a roster slot is worth if I spend it on the best pocket-change player instead.

    The baseline that makes ``stuck_value`` a decision rather than an accounting identity.
    Wherever the plan DP hands a bench slot's $1 straight back, ``min_bid_marginal`` comes
    out positive whoever the player is -- so acquiring *anybody* for a dollar scores as a
    gain and a fifth running back reads as a $39 gift. (It is not positive *everywhere*:
    with the bench full there is no dollar to hand back, and 296 of the 458 sub-$3 players
    on the real board are negative. See the note below the fold.) What the plan cannot see
    either way is that the slot he occupies has an alternative use, and at $1 that
    alternative is freely available.

    So the stuck term is measured against it: "if this nomination sticks to me, what do I
    gain over the $1 filler I would otherwise have put in that slot?" A stud is worth many
    times the filler; a body at a position I have already filled is worth less than one,
    and goes negative -- which is the whole cost of a nomination going wrong, and it is
    computed rather than assumed.

    Reads the *whole* pool, which is another reason ``recommend_nominations`` insists on
    one: a short list's cheapest row is not the cheapest player in the draft.
    ``_DART_PRICE_CEILING`` is reused rather than re-picked -- "pocket change" already
    means one thing in this engine, and it should not come to mean two.

    The threshold widens to the cheapest player on the board when nothing is that cheap,
    so the comparison set is never empty. An empty set would return a floor of zero, which
    silently restores the absolute reading this function exists to replace -- a failure
    that looks like a working model rather than a missing one.

    **The floor is genuinely allowed to be negative**, and clamping it at zero was the same
    failure wearing a guard. An earlier draft of this docstring asserted that
    ``min_bid_marginal`` "is never negative"; measured on the real 2026 board with an empty
    roster, 296 of the 458 sub-$3 players have a negative one -- when the bench is full the
    plan hands back no dollar, so a body at $1 is a real loss. On a board where every cheap
    player is underwater the best a slot can do *is* underwater, and a floor pinned to 0.0
    would charge every candidate against an option that does not exist.

    Two floors are returned, best and runner-up, because the comparison set is not always
    disjoint from the candidates. Ideally it is: the floor comes from players priced below
    ``_MIN_DRAIN_PRICE``, exactly the set that can never be a candidate. But a board can run
    out of pocket change -- late in a draft, or on a fixture whose cheapest player is $16 --
    and then the set has to widen to the cheapest players there are, who *are* candidates.
    Scoring a man against a set containing himself forces his ``stuck_value`` non-positive by
    construction and makes a bargain unreachable for him whatever the plan says, which is the
    same defect an earlier inclusive test had at exactly $3. Reproduced on this repo's own
    priced fixture: the floor set was the two cheapest candidates and the better of them
    scored exactly 0.0.

    So the caller subtracts the runner-up when the candidate *is* the argmax, and the best
    otherwise -- each player measured against the best filler that is not him.
    """
    priced = [
        recommendation
        for recommendation in recommendations
        if recommendation.budget_price is not None and recommendation.min_bid_marginal is not None
    ]
    if not priced:
        return _SlotFloor(0.0, 0.0, None)
    # Widened only when nothing is that cheap, so the comparison set is never empty.
    threshold = max(_MIN_DRAIN_PRICE, min(r.budget_price for r in priced) + 0.01)

    best: float | None = None
    second: float | None = None
    holder: str | None = None
    for recommendation in priced:
        if recommendation.budget_price >= threshold:
            continue
        marginal = _held_value(recommendation)
        if best is None or marginal > best:
            best, second, holder = marginal, best, recommendation.valuation.player_key
        elif second is None or marginal > second:
            second = marginal
    if best is None:
        return _SlotFloor(0.0, 0.0, None)
    return _SlotFloor(best, best if second is None else second, holder)


@dataclass(frozen=True)
class _SlotFloor:
    """The best $1 filler a roster slot could hold, and the runner-up.

    ``for_candidate`` hands back the runner-up to whoever *is* the best filler, so no player
    is ever measured against himself. See ``_slot_floor``.
    """

    best: float
    runner_up: float
    holder: str | None

    def for_candidate(self, player_key: str) -> float:
        return self.runner_up if player_key == self.holder else self.best


def _held_value(recommendation: AuctionRecommendation) -> float:
    """What owning him at $1 is worth to me, in dollars, discounted for depth.

    ``min_bid_marginal`` arrives raw from ``auction.py`` precisely so this multiply can be
    a plain one. The engine's own ``score`` runs the same quantity through ``penalized``,
    which *divides* on a negative to push it down in rank order -- correct there, wrong
    here, because this number is subtracted from another player's and then added to
    ``drain_gain``. A ``need`` of 0.04 came back 25x magnified against a baseline measured
    at ``need`` 1.0, so a bench body could score -$125 in a $200 league.
    """
    marginal = recommendation.min_bid_marginal
    if marginal is None:
        return 0.0
    return marginal * min(1.0, recommendation.depth_factor)


def _plan_pressure(my_roster_counts: dict[str, int], settings: LeagueSettings) -> dict[str, float]:
    """How much of my remaining shopping is aimed at each position, summing to 1.0.

    This is the term that keeps the nomination list off my own board, and it replaced a
    per-player gate that did not work. That gate exempted the top ``depth`` players at a
    position -- so with one open RB slot it protected exactly one back, while the buy list
    still ranked five, and the other four took full credit and floated to the top of the
    panel. Measured on the real 2026 record at pick 40 with two backs rostered,
    ``reaching['RB']`` held a single name and nominations #2-#5 were buys #3, #4, #5 and #8;
    across the whole record the top-6 list was in strictly descending price order at 79 of
    88 moments. The panel was the buy list wearing a different hat, which is the exact
    failure the term exists to prevent.

    The mistake was pricing the *player*. Competing backs are substitutes for one slot: I do
    not mind which I get, so protecting only the best one protects nothing. What a
    nomination actually resolves is the price of a **tier**, and if I still need a back then
    putting any back up sets the RB market while the room is at its richest. So the weight
    is positional and continuous -- no cut between adjacent players, and no cliff when two
    interchangeable receivers sit a cent apart.

    A flex is **one** slot, split evenly across the positions that could fill it, so the
    weights total the real number of open starting slots. Adding it whole to every eligible
    position -- which the per-player version did -- claimed 11 wanted slots against 9 real
    ones and read every cascade one rung too deep, the same overcount ``lineup.py`` exists
    to prevent.

    Empty roster in a QB/RB2/WR2/TE/flex/K/DEF league: RB and WR land near 0.26 each and QB
    near 0.11, so early, when I need everything, the weights are mild and near-uniform and
    the ranking falls through to price and aim -- "put up the expensive man" is a sound
    opening. Late, holding only an RB hole, RB goes to 1.0 and every back is suppressed
    outright while the rest of the board stays fully drainable. That is the behaviour the
    hard gate was reaching for and never had.
    """
    open_dedicated, open_flex, _ = assign_lineup(my_roster_counts, settings)
    weight: dict[str, float] = {
        position: float(count) for position, count in open_dedicated.items() if count > 0
    }
    for eligible, count in open_flex:
        if count <= 0 or not eligible:
            continue
        share = count / len(eligible)
        for position in eligible:
            weight[position] = weight.get(position, 0.0) + share

    total = sum(weight.values())
    if total <= 0:
        return {}
    return {position: value / total for position, value in weight.items()}


def _cascade_costs(
    recommendations: list[AuctionRecommendation],
    my_roster_counts: dict[str, int],
    settings: LeagueSettings,
    slot_floor: float,
) -> tuple[dict[str, float], dict[str, set[str]]]:
    """What losing a player my plan wanted actually costs: the gap to the next man.

    The obvious cost to charge for nominating someone I want is what having him is worth --
    and that is wrong by an order of magnitude, because a rival buying him at the going rate
    does not take his *value* from me, it takes my chance to buy him at that same rate. What
    I actually lose is the cascade: my plan slides down one rung at that position, and the
    loss is the gap between the last player it meant to buy and the first it did not.

    Charged with the full value instead, every expensive player scored deeply negative and
    the list collapsed onto the cheapest rows clearing ``_MIN_DRAIN_PRICE`` -- measured on
    the real 2026 board it recommended nominating a $4 quarterback, which is not a
    nomination at all. The gap is usually a dollar or two, which is the honest size of the
    thing, and it is zero for every player at a position I have already filled.

    **When the position is exhausted there is no rung to slide to**, and the full value came
    back through that door: with ``depth`` at 3 and three backs left the charge was $179.79,
    and adding a single fourth back dropped it to $9.23 -- a 19x step from one player, firing
    exactly at the thin positions late in a draft. The fall-back is his value over
    ``slot_floor``, the same $1-filler baseline ``stuck_value`` uses, which is bounded and
    means something rather than being an artifact of running off the end of a list.

    ``depth`` here is the *ladder* depth -- how many I would really buy at this position --
    which is not the flex-inflated reach set. See ``_plan_pressure``.
    """
    open_dedicated, open_flex, _ = assign_lineup(my_roster_counts, settings)
    depth_of: dict[str, float] = {
        position: float(count) for position, count in open_dedicated.items() if count > 0
    }
    for eligible, count in open_flex:
        if count <= 0 or not eligible:
            continue
        share = count / len(eligible)
        for position in eligible:
            depth_of[position] = depth_of.get(position, 0.0) + share

    by_position: dict[str, list[AuctionRecommendation]] = {}
    for recommendation in recommendations:
        by_position.setdefault(recommendation.position, []).append(recommendation)

    gaps: dict[str, float] = {}
    reaching: dict[str, set[str]] = {}
    for position, raw_depth in depth_of.items():
        # ``_dollars`` is half-up; ``round`` is banker's, and a flex contributes exactly
        # 0.5 whenever it has two eligible positions, so RB 1.5 and WR 2.5 would round in
        # opposite directions and credit one flex slot to one position while dropping it
        # from the other.
        depth = max(1, _dollars(raw_depth))
        ranked = sorted(by_position.get(position, []), key=lambda r: -r.value)
        if not ranked:
            continue
        marginal = ranked[min(depth, len(ranked)) - 1]
        following = ranked[depth] if len(ranked) > depth else None
        if following is None:
            gaps[position] = max(0.0, marginal.value - slot_floor)
        else:
            gaps[position] = max(0.0, marginal.value - following.value)
        reaching[position] = {r.valuation.player_key for r in ranked[:depth]}
    return gaps, reaching


def _rival_shape(
    rival: RivalSeat, my_needs: set[str], settings: LeagueSettings
) -> tuple[set[str], float]:
    """What a rival still needs, and what share of it competes with me.

    Both come off one ``assign_lineup``. Splitting them into two helpers meant solving the
    same rival's slot assignment twice on adjacent lines, and left the two derivations free
    to disagree about what an open slot is.

    The overlap is slot-weighted, not position-weighted, and the difference is large. A
    rival with an empty roster needs {QB, RB, WR, TE, K, DEF}; against my {RB, WR} a set
    count says 2/6 and badly understates him, because his money really will go to backs and
    receivers -- that is where his slots are. Counting slots (2 RB + 2 WR + 1 flex out of 9)
    says 0.56.

    A flex counts once, as one slot, if *any* eligible position is one I need: a flex he
    fills with a back is a back out of the pool I am shopping in.
    """
    open_dedicated, open_flex, _ = assign_lineup(rival.roster_counts, settings)
    needs: set[str] = set()
    total = 0
    contested = 0
    for position, count in open_dedicated.items():
        if count <= 0:
            continue
        needs.add(position)
        total += count
        if position in my_needs:
            contested += count
    for eligible, count in open_flex:
        if count <= 0:
            continue
        needs |= set(eligible)
        total += count
        if set(eligible) & my_needs:
            contested += count
    return needs, (contested / total if total > 0 else 0.0)


def recommend_nominations(
    recommendations: list[AuctionRecommendation],
    settings: LeagueSettings,
    my_roster_counts: dict[str, int],
    rivals: list[RivalSeat],
    *,
    my_budget_remaining: int,
    my_slots_remaining: int,
    league_money_remaining: int,
    limit: int = 6,
) -> list[NominationCandidate]:
    """Rank the names worth putting up, best first.

    ``recommendations`` **must be the whole available pool**, not the display short list.
    The best drain is by construction a player the buy list ranks low -- that is what "I do
    not want him" means -- so handing this a top-8 returns exactly the players the list
    exists to steer you away from nominating. ``Assistant.nomination_list`` is the only
    caller and passes the full pool; ``tests/test_nomination.py`` pins it.

    Rows scoring at or below zero are returned and dimmed rather than dropped, the same
    call this repo already made for a board where every row reads "bid $0": "there is no
    good nomination right now, put up someone cheap you do not want" is real advice, and
    hiding it just makes the panel look broken.
    """
    if not recommendations or my_slots_remaining <= 0:
        return []

    my_needs = open_positions(my_roster_counts, settings)
    # My share of the money still in the room -- see the module docstring on why this is
    # derived from the inflation accounting rather than chosen.
    drain_share = my_budget_remaining / max(1, league_money_remaining)
    slot_floor = _slot_floor(recommendations)
    cascade, reaching = _cascade_costs(
        recommendations, my_roster_counts, settings, slot_floor.best
    )
    pressure = _plan_pressure(my_roster_counts, settings)

    # One ``assign_lineup`` per rival, not two: the needs set and the overlap share are both
    # read off the same assignment rather than each helper solving it again.
    needs_of: dict[str, set[str]] = {}
    overlap_of: dict[str, float] = {}
    for rival in rivals:
        needs_of[rival.team_key], overlap_of[rival.team_key] = _rival_shape(
            rival, my_needs, settings
        )

    candidates: list[NominationCandidate] = []
    for recommendation in recommendations:
        price = recommendation.budget_price
        if price is None:
            continue
        dollars = _dollars(price)
        # "He will be there at $1 anyway" has to be judged against a *live* number, and
        # ``budget_price`` is not one: on the room tier it is ``par x premium``, which
        # carries no inflation, while ``bid_to`` on the same row does. Testing the static
        # figure alone blanked the panel for the entire back half of a real draft --
        # measured on the 2026 record, ``nomination_list`` returned nothing for 75 of 163
        # sale moments, continuously from sale 88, while $194 was still changing hands and
        # the buy panel was quoting $7 for a player this list refused to name. Late money
        # is exactly the regime the module docstring calls the payoff.
        #
        # So both readings must agree he is pocket change before he stops being a decision.
        if max(dollars, recommendation.bid_to) < _MIN_DRAIN_PRICE:
            continue
        position = recommendation.position

        # A rival is live on him only if he has somewhere to start him *and* can legally
        # pay the going rate. Both halves are facts off the board, which is what makes this
        # the one term in the model carrying information my own sheet does not have -- and
        # why the UI names the teams rather than printing a count.
        #
        # ``_dollars``, not ``ceil``, and the same ``_dollars`` every display of this price
        # uses. Three roundings of one number meant the gate tested a threshold the panel
        # never showed: at $20.13 it required $21 while every cell read "$20", so a rival
        # holding exactly $20 was dropped from the set on a row asserting he could not
        # reach $20 -- shrinking ``aim``, inflating ``stuck_probability``, and able to flip
        # the motive. ``dollars`` is the single rounded price for the gate, the sentence and
        # the payload alike; it is computed once above, at the pocket-change test.
        live = [
            rival
            for rival in rivals
            if position in needs_of[rival.team_key] and rival.max_bid >= dollars
        ]

        pockets = sum(rival.max_bid for rival in live)
        if pockets > 0:
            aim = sum(rival.max_bid * overlap_of[rival.team_key] for rival in live) / pockets
        else:
            aim = 0.0

        surplus = recommendation.surplus
        waste = 0.0 if surplus is None else min(max(-surplus, 0.0), price) / max(price, 1.0)
        # ``dollars``, not the raw price: a sale removes whole dollars from the room, and
        # this is the same figure the gate tested and the panel prints, so the row's numbers
        # reconcile against each other.
        drain = dollars * aim * (1.0 + _WASTE_WEIGHT * waste)

        # Nominating into a tier you are still shopping in drains nobody worth draining:
        # the dollars are as likely to be yours, and either way you have set the price of
        # the position while the room is at its richest. ``_plan_pressure`` weights that by
        # how much of my remaining shopping is aimed here -- positional and continuous, so
        # two interchangeable receivers a cent apart cannot get opposite advice.
        mine = recommendation.valuation.player_key in reaching.get(position, ())
        shopping = pressure.get(position, 0.0)
        drain_gain = drain * drain_share * _CONTENTION_PREMIUM * (1.0 - shopping)

        stuck_probability = _STUCK_DECAY ** len(live)
        # None when no budget plan could be priced. Owning him at $1 is then a quantity
        # nothing here can name, so both halves that read my plan go quiet rather than
        # guessing -- the drain term still works, since it never does.
        stuck_value = (
            0.0
            if recommendation.min_bid_marginal is None
            else _held_value(recommendation)
            - slot_floor.for_candidate(recommendation.valuation.player_key)
        )

        # Not a claim he would otherwise have survived -- nobody survives an auction. It
        # charges what resolving his price now costs me, which is what a nomination
        # actually controls: my measurable edge over the room (identically zero with no
        # published prices, since my worth *is* the estimate there), plus the cascade if he
        # is one my plan was reaching for. The second term is the one that survives a
        # priceless board, and it is what keeps this list off my own targets.
        loss_if_sold = 0.0
        if mine:
            # He is one my plan is reaching for, so pricing him now costs me twice.
            #
            # The cascade if a rival takes him: my plan slides one rung at his position.
            #
            # And the inflation premium if *I* take him, which is the price-enforcement
            # argument stated as a number. Money leaves the room unevenly and the price
            # level falls as it goes (``inflation_factor``); ``value`` is his worth at
            # today's level and ``par`` is his worth at none, so the gap is what I pay for
            # settling his price while the room is still fat. Correctly zero on an
            # untouched board, where no money has moved and there is no premium yet.
            loss_if_sold = cascade.get(position, 0.0) + max(
                0.0, recommendation.value - recommendation.par
            )
        # The mispricing edge is scaled by the same ``shopping`` weight the drain is, and
        # for the mirror reason: an edge is only forgone if I was going to capture it. Left
        # unscaled it charged the full surplus on every row, including positions I am
        # already full at -- on the priced fixture that buried receivers I would never buy
        # under a $40 penalty and floated the backs I actually wanted to the top.
        # The surplus half requires a price from *outside* me. ``bid_to`` descends from the
        # inflation-adjusted ``value`` while ``dollars`` is the room tier's ``par x
        # premium``, which carries no inflation -- so subtracting them on an unpriced board
        # measures the inflation factor and books it as a mispricing edge. Measured on the
        # record: 62 candidate rows with no published or interpolated price still showed
        # ``bid_to - dollars > 0``, e.g. a $9 phantom edge on Justin Herbert. Gating on
        # ``market`` makes the term identically zero offline, which is what the comment
        # above and the module's price table always claimed it was.
        edge = 0.0
        if recommendation.market is not None:
            edge = max(0.0, recommendation.bid_to - _dollars(recommendation.market))
        capture_at_risk = shopping * edge + loss_if_sold

        bargain_term = stuck_probability * stuck_value
        drain_term = (1.0 - stuck_probability) * (drain_gain - capture_at_risk)
        if recommendation.price_basis in {"room", "own"}:
            # No tier priced *him* -- see the module docstring. Keep the cost of getting
            # stuck, refuse the upside claim.
            #
            # The room tier belongs here as much as ``own`` does, and leaving it out made
            # the guard almost inert: a board flips from ``own`` to ``room`` after
            # ``_ROOM_BASIS_MIN_SALES`` sales and never flips back, so in an unpriced league
            # the suppression covered only the first eight sales. ``PriceBasis`` calls the
            # room tier "my own ranking wearing the room's price level" and keeps it out of
            # ``market_price`` for precisely this reason. Measured on the fixture with a
            # broke room, the sale that flipped the tier took the panel from 0 of 24
            # bargains to 23 of 24, top row my own best player at a claimed $191.
            bargain_term = stuck_probability * min(0.0, stuck_value)

        score = bargain_term + drain_term
        # The label follows whichever term actually dominates, which is what the module
        # docstring promises and what the badge means: the two motives call for opposite
        # behaviour once bidding starts, so a row worth $14 mostly because he would stick to
        # me at $1 must not read "drain".
        #
        # An earlier fix gated this on ``not live`` to stop the reason sentence claiming
        # "nobody can reach him" beside a column naming Team 11. That was the right bug and
        # the wrong lever: it silenced the sentence by mislabelling the row. Measured, it
        # badged a score that was 84% bargain term as a drain. The sentence now does its own
        # bidder check (see ``_explain_nomination``) and the label is left honest.
        #
        # It must still be a positive claim rather than the less negative of two bad numbers:
        # with no plan priced ``bargain_term`` is a flat 0.0, and labelling every underwater
        # row "bargain" for beating a negative drain would be exactly backwards.
        motive = (
            MOTIVE_BARGAIN if bargain_term > 0 and bargain_term > drain_term else MOTIVE_DRAIN
        )

        candidates.append(
            NominationCandidate(
                recommendation=recommendation,
                motive=motive,
                score=score,
                expected_price=float(dollars),
                live_bidders=tuple(rival.name for rival in live),
                live_bidder_keys=tuple(rival.team_key for rival in live),
                aim=aim,
                drain=drain,
                drain_gain=drain_gain,
                stuck_probability=stuck_probability,
                stuck_value=stuck_value,
                capture_at_risk=capture_at_risk,
                reason=_explain_nomination(
                    motive=motive,
                    position=position,
                    dollars=dollars,
                    live=[rival.name for rival in live],
                    aim=aim,
                    drain_gain=drain_gain,
                    stuck_probability=stuck_probability,
                    stuck_value=stuck_value,
                    capture_at_risk=capture_at_risk,
                    inferred=recommendation.price_basis,
                ),
            )
        )

    candidates.sort(key=lambda candidate: -candidate.score)
    return candidates[:limit]


def _names(live: list[str]) -> str:
    """Up to three team names, then a count. Reading twelve is not reading."""
    if not live:
        return "nobody"
    if len(live) <= 3:
        if len(live) == 1:
            return live[0]
        return ", ".join(live[:-1]) + " and " + live[-1]
    return ", ".join(live[:3]) + f" and {len(live) - 3} more"


def _explain_nomination(
    *,
    motive: str,
    position: str,
    dollars: int,
    live: list[str],
    aim: float,
    drain_gain: float,
    stuck_probability: float,
    stuck_value: float,
    capture_at_risk: float,
    inferred: str,
) -> str:
    """One sentence saying why to say this name, in the voice of ``auction._explain``.

    The motive is stated, not implied, because the two call for opposite behaviour once
    bidding starts: on a drain you must not get carried away, on a bargain you must be
    ready to pay to your ``bid_to``. A row that only showed a number would leave the most
    important half of the advice in the reader's head.
    """
    parts: list[str] = []
    if motive == MOTIVE_BARGAIN:
        # The "nobody can reach him" claim is checked here, against ``live``, rather than by
        # the motive test. Gating the *label* on an empty bidder set silenced this sentence
        # by mislabelling the row -- see ``recommend_nominations``. With a funded rival in
        # the room the row is still honestly a bargain; the sentence just has to say the
        # true version of why.
        if live:
            parts.append(
                f"worth putting up mostly because he may stick to you at $1"
                f" -- only {_names(live)} can bid"
            )
        else:
            parts.append(
                f"nobody who needs a {position} can reach ${dollars}, so he may fall to you at $1"
            )
        if stuck_value > 0:
            parts.append(f"worth ${stuck_value:.0f} to hold at that price")
    else:
        if live:
            parts.append(f"{_names(live)} can reach ${dollars} and still need a {position}")
        else:
            parts.append(f"nobody who needs a {position} can reach ${dollars}")
        if aim > 0:
            parts.append(f"{aim * 100:.0f}% of that money is chasing what you still need")
        if drain_gain > 0:
            parts.append(f"draining it is worth about ${drain_gain:.0f} to you")
        if stuck_value < 0:
            parts.append(
                f"but a {stuck_probability * 100:.0f}% chance he sticks to you, "
                f"costing ${-stuck_value:.0f} of roster"
            )
    if capture_at_risk > 0:
        parts.append(f"you give up ${capture_at_risk:.0f} of edge by pricing him now")
    if inferred == "room":
        parts.append("price is what this room pays for the position, not for him")
    elif inferred == "own":
        parts.append("price is your own valuation -- no source or room rate under it")
    return "; ".join(parts)
