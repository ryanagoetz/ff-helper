"""Auction drafting -- a different problem from a snake draft.

In a snake draft the scarce resource is *picks*, handed out in a fixed order, so the
question is "will he last until my turn?" and the answer is VONA.

In an auction nobody is ever unavailable, only unaffordable. Every player can be had by
any team, so scarcity of picks disappears entirely and is replaced by scarcity of
*dollars*. VONA is meaningless here: survival probability is 1.0 for everyone, forever.
The questions that replace it are:

1. **What is he worth, in dollars?** VOR converts to money once you know how much money
   exists and how much value it is chasing.
2. **What will the room pay?** Yahoo's ``average_cost`` where it exists -- the auction
   analog of ADP, and just as separate from value as ADP is. Where it does not (an offline
   league whose projections CSV carries no auction column; any league once the last priced
   player has been sold) ``PriceBasis`` falls back through interpolation to the room's own
   observed pricing, and finally to my own worth. Those are not equally good evidence, and
   which consumer may read which is that class's whole subject.
3. **What can I actually afford?** A hard constraint, not advice.

The gap between (1) and (2) is where an auction is won, and it is the direct analog of
VONA: not "who will be gone" but "who is mispriced".

**Inflation is the live part.** Par values are computed once from a static pool, but money
leaves the room unevenly. If the league blows its budget on early studs, the dollars left
chasing the remaining players shrink and everyone still on the board gets cheaper. If the
room is thrifty early, the survivors inflate. Recomputing this after every sale is what
keeps the numbers honest three hours in -- and it is the single biggest edge available,
because most drafters are still working off a cheat sheet printed before the draft began.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from ff_helper.engine.lineup import assign_lineup, depth_multiplier
from ff_helper.engine.replacement import ReplacementLevels
from ff_helper.engine.upside import upside_bonus

# The injury residual is shared on purpose: the games-missed cut already lives in the
# projection (blend.availability_of), and both formats price the leftover risk alike.
from ff_helper.engine.vona import _BYE_PENALTY, _INJURY_RESIDUAL, penalized
from ff_helper.rankings.blend import PlayerValuation
from ff_helper.yahoo.models import LeagueSettings

# Every drafted player costs at least this much, so it is reserved off the top.
MIN_BID = 1

# Bounds on the inflation multiplier. Late in a draft the denominator gets small and the
# ratio can swing wildly on a single sale; clamping keeps a $2 kicker from being valued
# at $60 because three teams happen to have money left.
MIN_INFLATION = 0.25
MAX_INFLATION = 3.0

# Only players going for pocket change get an upside bonus: a $2 dart is exactly the
# purchase where variance is the point, while a $30 starter is bought on projection.
_DART_PRICE_CEILING = 3.0

# Rungs of a position's price ladder, as offsets from the player the league's remaining
# demand says I could realistically end up with (the k-th best left). Together with the
# last man standing at $1 they are the *choices* a slot really has: pay up now, wait a
# beat, punt a while, or punt entirely.
_LADDER_OFFSETS = (0, 2, 5)

# Slack for the budget plan's break-even test, in dollars. Not a fudge factor: the
# marginal is a difference of two DP totals that share most of their terms, and for the
# best remaining player at a position whose dedicated slots are full it is *exactly* zero
# in real arithmetic -- the plan's top rung for that slot is the candidate himself, so
# buying him and planning to buy him are the same basket. Floating-point summation then
# lands a hair either side of zero, and a strict ``< 0`` read the tie as "worse". A
# hundredth of a cent is far below any dollar difference the model can mean, and far
# above the ~1e-14 noise of summing fifteen dollar values.
_PLAN_EPSILON = 1e-4

# Qualifying sales before the room's own pricing counts as a price at all. Deliberately a
# separate constant from ``_PREMIUM_PRIOR_GLOBAL`` even though they start equal: that one is
# a shrinkage weight (how hard to pull a ratio toward 1.0), this one is a sample-size floor,
# and retuning the smoothing must not silently relocate the evidence gate.
_ROOM_BASIS_MIN_SALES = 8


@dataclass(frozen=True)
class DollarValues:
    """Par dollar values for the whole pool, before inflation."""

    par: dict[str, float]
    dollars_per_vor: float
    pool_size: int

    def value_of(self, player_key: str) -> float:
        return self.par.get(player_key, float(MIN_BID))


def compute_par_values(
    valuations: list[PlayerValuation],
    levels: ReplacementLevels,
    settings: LeagueSettings,
    num_teams: int,
    *,
    kept_player_keys: set[str] | None = None,
    kept_salary: int = 0,
) -> DollarValues:
    """Convert VOR into dollars for the draftable pool.

    Only the players who will actually be *bought* matter -- three ways. Spreading the
    league's money across every player in the database would price the studs far too low,
    because most of that database is never bought. Equally, keepers are already owned: they
    occupy no biddable slot, their salaries are no longer biddable money, and their VOR
    does not belong in the denominator.

    Excluding keepers here does not change what the app recommends. ``par - 1`` is exactly
    ``VOR * dollars_per_vor``, so keepers distort a single scalar, and ``inflation_factor``
    -- remaining money over remaining par surplus -- scales by that same scalar and cancels
    it precisely. Verified identical to the cent at one, three and five keepers per team.

    It is worth doing anyway, because that cancellation costs something real: it spends the
    inflation clamp. With keepers in the pool the correction rides inside ``inflation``, so
    a league keeping five per team starts at 2.44 of a 3.0 ceiling, leaving only 1.23x of
    headroom before genuine market movement gets truncated -- and once it clamps, the
    cancellation breaks. Pricing keepers out here leaves ``inflation`` carrying one signal
    (is the room overspending or thrifty?) instead of two, which is what its name, its
    docstring, and the "price level" readout all claim it means.
    """
    kept = kept_player_keys or set()
    budget = settings.auction_budget
    roster_size = settings.roster_size or 1
    pool_size = max(1, num_teams * roster_size - len(kept))

    buyable = [v for v in valuations if v.player_key not in kept]
    ranked = sorted(buyable, key=lambda v: -levels.vor(v))[:pool_size]
    total_vor = sum(max(0.0, levels.vor(v)) for v in ranked)

    total_money = max(0, num_teams * budget - kept_salary)
    reserved = pool_size * MIN_BID
    discretionary = max(0.0, total_money - reserved)

    dollars_per_vor = (discretionary / total_vor) if total_vor > 0 else 0.0

    # Keepers still get a par value: they are priced for display and for the record, they
    # are simply not part of what the remaining money is chasing.
    par = {
        valuation.player_key: MIN_BID + max(0.0, levels.vor(valuation)) * dollars_per_vor
        for valuation in valuations
    }
    return DollarValues(par=par, dollars_per_vor=dollars_per_vor, pool_size=pool_size)


def inflation_factor(
    available: list[PlayerValuation],
    values: DollarValues,
    *,
    money_remaining: int,
    slots_remaining: int,
) -> float:
    """How much more (or less) than par the remaining players are now worth.

    Above 1.0 the room has money and too few good players left, so everything costs more
    than the pre-draft sheet says. Below 1.0 the room overspent early and there are
    bargains. This is the number that a printed cheat sheet cannot give you.
    """
    if slots_remaining <= 0:
        return 1.0

    discretionary_remaining = money_remaining - slots_remaining * MIN_BID
    if discretionary_remaining <= 0:
        # Everyone is down to minimum bids; nothing has surplus value left.
        return MIN_INFLATION

    # Only the players who will still be bought count toward the remaining par surplus.
    ranked = sorted(available, key=lambda v: -values.value_of(v.player_key))[:slots_remaining]
    par_surplus = sum(max(0.0, values.value_of(v.player_key) - MIN_BID) for v in ranked)
    if par_surplus <= 0:
        return 1.0

    return max(MIN_INFLATION, min(MAX_INFLATION, discretionary_remaining / par_surplus))


# -- live price calibration ------------------------------------------------------------

# How many sales' worth of belief in "the room pays sheet price" a premium starts with.
# A position needs more evidence than the room overall before its premium moves, because
# per-position sale counts are small and one $75 sale should not triple every TE.
_PREMIUM_PRIOR_POSITION = 5.0
_PREMIUM_PRIOR_GLOBAL = 8.0

# Sales expected to go near the minimum bid say nothing about the room: a $3 player
# selling for $5 is a 67% premium in ratio terms and pocket change in real ones.
_PREMIUM_MIN_EXPECTED = 3.0

# One misread price (a $150 typo on a $15 player) must not swamp the average.
_RATIO_FLOOR = 0.2
_RATIO_CEILING = 5.0


@dataclass(frozen=True)
class Sale:
    """One completed sale, reduced to what price calibration needs."""

    position: str
    price: float
    expected: float  # pre-draft market cost, falling back to par value


@dataclass(frozen=True)
class RoomPremiums:
    """How much this room actually pays relative to the pre-draft sheet.

    Distinct from ``inflation_factor``: inflation is an accounting identity (money left
    over talent left) about what prices *must* do from here, while a premium is an
    observation about what this room *chooses* to pay -- overall and per position. A room
    that pays 130% of sheet for running backs and 70% for quarterbacks has inflation 1.0
    and two very different premiums.
    """

    overall: float = 1.0
    by_position: dict[str, float] = field(default_factory=dict)
    # Qualifying sales behind these ratios. Reported because a premium of 1.0 is two
    # completely different statements -- "the room pays sheet price" and "no evidence
    # yet" -- and ``PriceBasis`` has to tell them apart.
    observations: int = 0

    def at(self, position: str) -> float:
        return self.by_position.get(position, self.overall)


def room_premiums(sales: list[Sale]) -> RoomPremiums:
    """Estimate the room's paying habits from the sales so far.

    Each ratio is shrunk toward neutral: the position premium starts at the room-wide one
    (itself starting at 1.0) and only moves as real sales accumulate, so an empty or
    young draft reports 1.0 rather than the noise of its first two prices.
    """
    ratios: list[tuple[str, float]] = []
    for sale in sales:
        if sale.expected < _PREMIUM_MIN_EXPECTED or sale.price <= 0:
            continue
        ratio = max(_RATIO_FLOOR, min(_RATIO_CEILING, sale.price / sale.expected))
        ratios.append((sale.position, ratio))

    overall = (_PREMIUM_PRIOR_GLOBAL + sum(r for _, r in ratios)) / (
        _PREMIUM_PRIOR_GLOBAL + len(ratios)
    )

    by_position: dict[str, list[float]] = {}
    for position, ratio in ratios:
        by_position.setdefault(position, []).append(ratio)

    return RoomPremiums(
        overall=overall,
        by_position={
            position: (_PREMIUM_PRIOR_POSITION * overall + sum(group))
            / (_PREMIUM_PRIOR_POSITION + len(group))
            for position, group in by_position.items()
        },
        observations=len(ratios),
    )


def _estimate_markets(
    available: list[PlayerValuation], values: DollarValues
) -> dict[str, float]:
    """A market price, interpolated by par value, for players no source priced.

    Yahoo publishes no auction cost for deep players. Scoring them as if the market were
    exactly fair (surplus zero) ranks them above comparable players whose measured surplus
    is slightly negative, so instead we read the market off the players who *do* have one:
    sort the priced players by par value and interpolate at the unpriced player's par.
    """
    known = sorted(
        (values.value_of(v.player_key), v.market_cost)
        for v in available
        if v.market_cost is not None
    )
    if not known:
        return {}

    pars = [par for par, _ in known]
    markets = [market for _, market in known]

    estimates: dict[str, float] = {}
    for valuation in available:
        if valuation.market_cost is not None:
            continue
        par = values.value_of(valuation.player_key)
        index = bisect.bisect_left(pars, par)
        if index <= 0:
            estimate = markets[0]
        elif index >= len(pars):
            estimate = markets[-1]
        else:
            low_par, high_par = pars[index - 1], pars[index]
            span = high_par - low_par
            t = (par - low_par) / span if span > 0 else 0.5
            estimate = markets[index - 1] + t * (markets[index] - markets[index - 1])
        estimates[valuation.player_key] = max(float(MIN_BID), estimate)
    return estimates


@dataclass(frozen=True)
class PriceBasis:
    """What a player will cost -- and how much weight that number can carry.

    Two questions this module spent a long time conflating, because both were answered by
    reaching into one dict of prices and substituting my own worth wherever it came back
    empty. That fallback is circular in a specific way: the more a player is worth to me
    the higher his stand-in price, so any consumer reading the price as a *constraint*
    punished exactly the players it should have been promoting.

    Splitting it needs three tiers of price, not two, because they carry different things:

    * **published** -- a source's own auction cost. Real information about this player.
    * **interpolated** -- ``_estimate_markets``, reading an unpriced player's price off the
      published prices at neighbouring par values. Ordered by my par, but *levelled* by
      other players' real prices, so it still says something about him specifically.
    * **room** -- his par scaled by what this room has actually paid per dollar of par at
      his position. The level is real money that changed hands; the ordering within the
      position is entirely mine. It says what the position costs, not what *he* costs.

    The room tier fires when no *still-available* player carries a published cost --
    ``_estimate_markets`` is all-or-nothing, so one published price fills in the whole
    board. That is not only the offline case: a live league lands here too, once the last
    player Yahoo priced has been sold, which is mid-draft rather than never. Offline it
    depends on the projections CSV, which ``rankings/sources/projections_csv.py`` will read
    an auction column from if the export has one -- so an offline league is priceless only
    when its CSV is, not by construction. Before this tier existed, every consumer in that
    state silently ran on my own worth.

    So the two accessors take different evidence:

    ``market_price`` is published-or-interpolated only, and is what affordability, surplus
    and the plan-marginal score read. Those are claims about an individual -- "you cannot
    win him", "he is mispriced by $12" -- and the room tier cannot support one, because
    within a position it is my own ranking wearing the room's price level. Letting it try
    reintroduced the original bug in gentler form.

    ``estimate`` takes all three and falls back to my inflation-adjusted worth, because a
    budget reservation has to name a number and refusing to guess just means guessing $1.
    Here the room tier is exactly right: "what will a receiver of about this quality cost
    me" is the positional question it can answer.

    ``engine/nomination.py`` is the third consumer, and it reads both: ``estimate`` for
    "how much money leaves the room if he sells" -- a budgeting question about a price
    *level* -- and ``surplus`` (hence ``market_price``) for "this room overpays for *him*",
    which is the individual claim the room tier cannot support. It also reads ``tier``,
    below, because it needs one thing no consumer here ever did: whether any price from
    outside me exists at all. See that module for what the drain model does when none does.

    Measured on the one recorded auction (full replay / policy from pick 45 / compared
    through pick 95), against the same tiers with the room tier promoted into
    ``market_price`` as well:

        policy       budgeting only              + affordability
        engine       1460.4 / 1442.5 / 1430.8    1460.4 / 1442.5 / 1430.8
        engine_list  1442.5 / 1442.5 / 1412.9    1352.9 / 1352.9 / 1352.9

    Identical where the tier is only advice, strictly worse in all three where it reorders
    the short list -- ``engine_list`` is the arm that reads the display, so a wrongly
    unaffordable flag is exactly what it feels. Re-measure both columns if the plan or the
    ranking changes; these were taken with ``_PLAN_EPSILON`` in force.
    """

    published: dict[str, float]
    interpolated: dict[str, float]
    room: dict[str, float]
    own_worth: dict[str, float]

    def market_price(self, player_key: str) -> float | None:
        """What the room will pay *for him*, or ``None`` if nothing knows.

        Deliberately excludes the room tier; see the class docstring for the measurement
        that settled it.
        """
        for tier in (self.published, self.interpolated):
            price = tier.get(player_key)
            if price is not None:
                return price
        return None

    def estimate(self, player_key: str) -> float:
        """The going rate if anything knows it, else my own inflation-adjusted worth.

        Subscripts ``own_worth`` rather than defaulting: every dict here is built over the
        same ``available`` list in one pass, so a missing key means the caller assembled a
        basis over a different pool than it is now pricing. Defaulting to $1 there would
        quietly hand an open starter slot a $1 reservation and inflate the smart cap with
        no error -- and this repo's rule for a lookup miss is that it gets reported, never
        silently absorbed. ``_price_ladder`` already subscripts this dict; now both agree.
        """
        for tier in (self.published, self.interpolated, self.room):
            price = tier.get(player_key)
            if price is not None:
                return price
        return self.own_worth[player_key]

    def is_inferred(self, player_key: str) -> bool:
        """True when a market price exists but no source published this one."""
        return player_key in self.interpolated

    def tier(self, player_key: str) -> str:
        """Which tier ``estimate`` would read: published | interpolated | room | own.

        ``is_inferred`` already distinguishes the first two, and that flag is in the API
        payload and read by the page -- this does not replace it. What it adds is the one
        distinction the buy side never needed: ``"own"`` means *no* tier had anything, so
        the price is my own sheet with no external level under it at all. Only a consumer
        making a claim about the room rather than about my board has to care, which is why
        it arrives with ``engine/nomination.py`` and not before.
        """
        for name, prices in (
            ("published", self.published),
            ("interpolated", self.interpolated),
            ("room", self.room),
        ):
            if player_key in prices:
                return name
        return "own"


def _dollars(amount: float) -> int:
    """Whole dollars, halves rounding up.

    ``round()`` is banker's rounding: 2.5 -> 2 but 3.5 -> 4, which prices alternate
    ladder rungs a dollar low and charges the plan less than the room will pay.
    """
    return int(amount + 0.5)


def _price_ladder(
    pool: list[PlayerValuation],
    demand_index: int,
    basis: PriceBasis,
) -> list[tuple[int, float]]:
    """The realistic ways to fund one slot at this position: (price, my value) rungs.

    ``pool`` is the position's remaining players, best first by adjusted value. The top
    rung is the best player left at his going rate -- paying up is always one of the
    choices, which is exactly what ``slot_ladder``'s reservation cannot express. Below it
    sit the settle rungs around ``demand_index`` (the player the league's remaining
    demand realistically leaves me if I *don't* pay up -- where ``slot_ladder`` starts)
    and the last man standing at $1. The point is not price precision but *choice*: a
    plan decides per slot whether to pay up, settle, or punt, and two same-position
    slots sharing the top rung is an accepted approximation -- ladders choose depth,
    not individual players.

    A rung's price is ``basis.estimate``, so with no market at all every rung costs exactly
    what it is worth and the plan degenerates: a knapsack whose weights equal its values
    can only fill the budget, and ``plan_bid`` collapses to "his worth, capped". That is
    the correct answer for a model with no price information -- it is what the no-plan
    branch says too -- but it is not a break-even, which is why ``PriceBasis`` works to
    keep a real price in here for as long as there is one.
    """
    if not pool:
        return []
    indices = sorted(
        {0}
        | {min(demand_index + offset, len(pool) - 1) for offset in _LADDER_OFFSETS}
        | {len(pool) - 1}
    )
    ladder: list[tuple[int, float]] = []
    for index in indices:
        chosen = pool[index]
        price = basis.estimate(chosen.player_key)
        ladder.append((max(MIN_BID, _dollars(price)), basis.own_worth[chosen.player_key]))
    return ladder


def _plan_value(
    options: list[list[tuple[int, float]]],
    budget: int,
    *,
    released: int | None = None,
    memo: dict[tuple, float] | None = None,
) -> float:
    """Best total value my remaining open slots can buy with ``budget`` dollars.

    One entry of ``options`` per open starting slot: the (price, value) rungs that slot
    could take (a flex merges every eligible position's ladder). ``released`` marks a
    slot the candidate under consideration would fill himself, so the plan skips it.

    A slot nothing fits still swallows the $1 minimum and contributes nothing. That
    quiet zero is the actual marginal cost of overspending -- the thing the smart-cap
    heuristic approximated -- and it is what makes "he is worth $40 but pay at most $28"
    a computed statement instead of a rule of thumb.
    """
    if memo is None:
        memo = {}

    def solve(index: int, money: int) -> float:
        if index >= len(options):
            return 0.0
        if index == released:
            return solve(index + 1, money)
        key = (released, index, money)
        cached = memo.get(key)
        if cached is None:
            cached = solve(index + 1, max(0, money - MIN_BID))
            for price, value in options[index]:
                if price <= money:
                    funded = value + solve(index + 1, money - price)
                    if funded > cached:
                        cached = funded
            memo[key] = cached
        return cached

    return solve(0, max(0, int(budget)))


@dataclass(frozen=True)
class AuctionRecommendation:
    valuation: PlayerValuation
    value: float  # inflation-adjusted worth to you, in dollars
    par: float  # pre-draft par value, before inflation
    market: float | None  # what this room will pay: sheet price x the live room premium
    surplus: float | None  # value - market; the auction analog of VONA
    max_bid: int  # hard ceiling from your remaining budget and slots
    affordable: bool
    depth_factor: float
    score: float
    reason: str
    # True when ``market`` was interpolated across the players a source did price, rather
    # than published for this one. Room-derived prices never appear here, because they are
    # not ``market`` at all -- see ``PriceBasis`` for why they only inform budgeting.
    market_estimated: bool
    # Softer ceiling than max_bid: what you can pay and still fill your remaining
    # *starter* slots at realistic prices, not $1 apiece.
    smart_cap: int
    # The highest price at which buying him still beats spending the money on the rest
    # of the plan, capped at his own worth (None when budget info is missing and no
    # plan could be computed). Its value is the downward signal: "worth $90, but stop
    # at $87 -- the rest of your plan needs the difference."
    plan_bid: int | None = None

    # The three fields below are read by ``engine/nomination.py`` and by nothing here:
    # not ``rank_key``, not ``bid_to``, not ``_explain``. They are recorded rather than
    # recomputed because a nomination score built from a second opinion about the same
    # board would be a worse bug than no nomination list at all.

    # What the whole room pays to take him off the board -- ``PriceBasis.estimate``, so
    # the room tier is in it. A *budgeting* number and never an individual claim: it is
    # exactly what ``market``, ``surplus`` and ``affordable`` deliberately refuse to say.
    # Deliberately not the local ``expected_price`` the dart ceiling uses, which skips the
    # room tier on purpose -- see the comment there for why those two must differ.
    budget_price: float | None = None

    # Which ``PriceBasis`` tier ``budget_price`` came from. ``market_estimated`` already
    # says "interpolated" and this does not replace it; "own" is the new information --
    # no source, no interpolation and no room premium, so the number is my own sheet
    # talking to itself and a claim about the room built on it is circular.
    price_basis: str = "own"

    # The plan's marginal of owning him at ``MIN_BID``, in dollars: what "nobody else bids
    # and I keep him for $1" is worth. None exactly when ``plan_bid`` is. Raw rather than
    # ``penalized`` -- that helper is a rank transform that divides on negatives, and this
    # is read as money. Apply ``depth_factor`` yourself if you want the depth discount.
    min_bid_marginal: float | None = None

    @property
    def name(self) -> str:
        return self.valuation.name

    @property
    def position(self) -> str:
        return self.valuation.position

    @property
    def bid_to(self) -> int:
        """The most you should actually bid.

        With a plan, the plan's break-even price governs, under the hard and smart
        ceilings; without one, his raw worth stands in.
        """
        ceiling = min(self.max_bid, self.smart_cap)
        if self.plan_bid is not None:
            return int(min(ceiling, self.plan_bid))
        return int(min(ceiling, round(self.value)))


def recommend_auction(
    available: list[PlayerValuation],
    levels: ReplacementLevels,
    values: DollarValues,
    settings: LeagueSettings,
    roster_counts: dict[str, int],
    *,
    money_remaining: int,
    slots_remaining: int,
    my_max_bid: int,
    my_budget_remaining: int | None = None,
    league_position_counts: dict[str, int] | None = None,
    sales: list[Sale] | None = None,
    roster_byes: dict[str, list[int]] | None = None,
    limit: int = 8,
) -> list[AuctionRecommendation]:
    """Rank the remaining players by where your dollars go furthest.

    ``sales`` feeds the live room premium, ``league_position_counts`` (positions rostered
    across the whole league) sizes the competition for remaining starters, and
    ``my_budget_remaining`` enables the smart cap. All three are optional: without them
    the model degrades to sheet prices and the $1-per-slot hard ceiling.
    """
    inflation = inflation_factor(
        available,
        values,
        money_remaining=money_remaining,
        slots_remaining=slots_remaining,
    )
    premiums = room_premiums(sales) if sales else RoomPremiums()
    estimated_markets = _estimate_markets(available, values)

    # Worth and calibrated expected price for the whole pool, before any ranking: the
    # budget reservations below need prices for players that may never be recommended.
    # Three tiers, best evidence first; ``PriceBasis`` decides who may read which.
    #
    # The room tier is the new one, and it exists because ``room_premiums`` was already
    # measuring what this room pays per dollar of par and the result was then thrown away:
    # the premium multiplied a sheet price, and with no sheet price there was nothing to
    # multiply. Scaling par by it instead is self-consistent -- ``Sale.expected`` falls back
    # to par, so the ratio was measured against this very basis -- and it is the only price
    # information an offline league ever gets.
    #
    # It needs enough sales to *be* an observation, which is what ``_ROOM_BASIS_MIN_SALES``
    # is for: with nothing sold the premium is exactly 1.0, and that means "no evidence yet"
    # rather than "the room pays par" -- without a floor the tier would price the whole board
    # off a prior before anyone had bid on anything. The gate is a step, not a ramp, so the
    # sale that crosses it moves every unpriced player's basis at once, and that step is
    # *not* self-limiting: its size is ``|par * premium - adjusted|``, which is driven by
    # ``inflation``, a quantity the premium's shrinkage has no hold on. Measured at pick 60
    # of the recorded auction (inflation 1.74, WR premium 1.13) one rung moved $39.9 -> $26.3
    # the instant the eighth sale landed, of which the premium explains a third. Raising
    # ``_ROOM_BASIS_MIN_SALES`` delays the step; nothing here shrinks it.
    adjusted_of: dict[str, float] = {}
    published_of: dict[str, float] = {}
    interpolated_of: dict[str, float] = {}
    room_of: dict[str, float] = {}
    room_basis_ready = premiums.observations >= _ROOM_BASIS_MIN_SALES
    for valuation in available:
        key = valuation.player_key
        par = values.value_of(key)
        adjusted_of[key] = MIN_BID + (par - MIN_BID) * inflation
        premium = premiums.at(valuation.position)
        if valuation.market_cost is not None:
            published_of[key] = max(float(MIN_BID), valuation.market_cost * premium)
        elif (interpolated := estimated_markets.get(key)) is not None:
            interpolated_of[key] = max(float(MIN_BID), interpolated * premium)
        elif room_basis_ready:
            # ``par``, not ``adjusted``: the premium was measured as paid-over-par, so this
            # is the basis it belongs on. Tried the alternative -- take the level from
            # ``adjusted`` and only the positional tilt from the premium, which is tidier
            # dimensionally -- and the backtest refused it (engine 1460.4 -> 1350.6 full,
            # 1430.8 -> 1287.4 through pick 95). On the one recorded auction the room paid
            # near par throughout while ``inflation`` sat pinned at its 3.0 clamp, so
            # ``adjusted`` was the worse price predictor by a wide margin. The reservation
            # is bounded below instead; see ``starter_reserved``.
            room_of[key] = max(float(MIN_BID), par * premium)

    basis = PriceBasis(
        published=published_of,
        interpolated=interpolated_of,
        room=room_of,
        own_worth=adjusted_of,
    )

    open_dedicated, open_flex, backups = assign_lineup(roster_counts, settings)

    # Roster fullness for the upside phase-in, and per-position dedicated starter
    # counts for the bye-stack thinness check -- both mirror the snake engine.
    roster_size = settings.roster_size or 0
    fullness = sum(roster_counts.values()) / roster_size if roster_size else 0.0
    dedicated_starters: dict[str, int] = {}
    for slot in settings.starting_slots:
        if len(slot.eligible_positions) == 1:
            slot_position = next(iter(slot.eligible_positions))
            dedicated_starters[slot_position] = (
                dedicated_starters.get(slot_position, 0) + slot.count
            )

    def factor_for(position: str) -> float:
        if open_dedicated.get(position, 0) > 0:
            return 1.0
        if any(position in eligible and count > 0 for eligible, count in open_flex):
            return 1.0
        return depth_multiplier(backups.get(position, 0) + 1, 1)

    # -- budget reservation: what filling each of my remaining slots will really cost.
    # A starter slot reserves the going rate of the players I could realistically end up
    # with (from the k-th best remaining down, because k other open league slots compete
    # for the cheap ones, and successive openings take successive players); a bench slot
    # reserves the $1 minimum, as the hard max_bid already does.
    by_position: dict[str, list[PlayerValuation]] = {}
    for valuation in available:
        by_position.setdefault(valuation.position, []).append(valuation)
    for pool in by_position.values():
        pool.sort(key=lambda v: -adjusted_of[v.player_key])

    league_counts = league_position_counts or {}

    def demand_index_of(position: str) -> int:
        """Index of the player the league's remaining demand realistically leaves me.

        One definition, used by both the reservation (``slot_ladder``) and the budget plan
        (``_price_ladder``). They priced the same slot by two different rules for a while;
        since ``bid_to`` is a minimum over ``smart_cap`` and ``plan_bid``, that had the two
        halves of the model pulling against each other on the same dollar.
        """
        return max(1, levels.starters_drafted.get(position, 0) - league_counts.get(position, 0)) - 1

    def slot_ladder(position: str, count: int) -> list[float]:
        """What each of ``count`` open slots at this position will really cost.

        Two corrections over quoting one price per slot, both of which had the cap holding
        back money for purchases that never cost that much:

        **Successive slots get successive players.** Filling two open receiver slots does
        not mean buying the best remaining receiver twice -- the second slot is filled by
        whoever is left after the first. Quoting one price per slot reserved the top of the
        market once per opening, which on a real board asked $66 of a $56 budget to cover
        two slots that were eventually filled for $4.

        **The demand index is deliberately the same one the plan uses.** An earlier version
        also dropped ``demand``'s floor of 1, on the theory that a position whose league
        starting slots are all filled is uncontested and clears at the minimum. That was
        wrong twice over. ``starters_drafted`` counts league-wide *starting* slots while
        ``league_counts`` counts every *rostered* player, bench included -- so bench depth
        cancels starting slots and the branch fired from about 44% of a real draft onward
        (measured: after sale 100 of 163, QB, RB, WR and TE all reserved $1), collapsing
        the reservation and silently disabling the smart cap for the whole endgame. It also
        put this function and ``_price_ladder`` on two different demand rules for the same
        slot. Computing genuine unmet *starting* demand needs per-team counts, which this
        function is not given; until it is, the floor stays and the two agree.
        """
        pool = by_position.get(position)
        if not pool:
            return [float(MIN_BID)] * count
        ladder: list[float] = []
        for offset in range(count):
            chosen = pool[min(demand_index_of(position) + offset, len(pool) - 1)]
            ladder.append(max(float(MIN_BID), basis.estimate(chosen.player_key)))
        return ladder

    # Reservation for each position's open dedicated slots, cheapest rung last.
    dedicated_ladders = {
        position: slot_ladder(position, count) for position, count in open_dedicated.items()
    }
    # A flex fills from whichever eligible position is cheapest, and its slots ladder for
    # the same reason dedicated ones do -- two flex openings are two different players.
    flex_ladders = [
        min(
            (slot_ladder(p, count) for p in eligible),
            key=sum,
            default=[float(MIN_BID)] * count,
        )
        for eligible, count in open_flex
    ]

    starter_reserved = sum(sum(rungs) for rungs in dedicated_ladders.values()) + sum(
        sum(rungs) for rungs in flex_ladders
    )
    starter_slots_open = sum(open_dedicated.values()) + sum(count for _, count in open_flex)
    my_open_slots = max(0, (settings.roster_size or 0) - sum(roster_counts.values()))
    bench_open = max(0, my_open_slots - starter_slots_open)

    # A reservation larger than my budget drives every position's ``smart_cap`` to 0, so
    # ``bid_to`` is 0 board-wide and ``rank_key`` -- which leads on ``bid_to <= 0`` -- sorts
    # everyone into one bucket. Any price source can trigger it; a room paying 1.5x par is
    # enough. Tried scaling the ladders to fit the budget, which keeps the relative ordering
    # and drops only the level: the backtest refused it (engine_list 1442.5 -> 1380.9 full,
    # 1412.9 -> 1317.7 through pick 95, engine unchanged). The zero is doing real work --
    # when the open starters genuinely cost more than I hold, "do not bid" is the correct
    # advice, and softening it bought worse players. Left as is, deliberately, and noted
    # here so the next person does not re-derive the same rejected fix.
    total_reserved = starter_reserved + bench_open * MIN_BID

    def smart_cap_for(position: str) -> int:
        if my_budget_remaining is None:
            return my_max_bid
        # The candidate himself fills one open slot, so its reservation is released -- and
        # the rung released is the *cheapest* one, not the dearest. Buying him leaves one
        # fewer opening, so the ladder loses its last rung: with rungs [40, 30] the
        # reservation drops 70 -> 40, a release of 30. Releasing the first rung instead
        # overstated spendable by the rung spread on every position with two or more open
        # slots, which is the cap authorising more than the plan behind it can fund.
        if open_dedicated.get(position, 0) > 0:
            rungs = dedicated_ladders.get(position) or [float(MIN_BID)]
            released = rungs[-1]
        else:
            flex_hits = [
                rungs[-1]
                for rungs, (eligible, count) in zip(flex_ladders, open_flex, strict=True)
                if position in eligible and count > 0 and rungs
            ]
            if flex_hits:
                released = min(flex_hits)
            elif bench_open > 0:
                released = float(MIN_BID)
            else:
                released = 0.0
        spendable = my_budget_remaining - (total_reserved - released)
        return max(0, min(my_max_bid, int(spendable)))

    # -- budget plan: what my remaining dollars can still buy, slot by slot ------------
    # Scoring against the plan replaces the 0.6/0.4 surplus/worth blend whenever my own
    # budget is known: a candidate is worth his value *plus what the plan can still fund
    # after paying for him, minus what it could fund without him*. Fair-priced players
    # already in the plan land near zero; bargains surface as the budget they free; a
    # stud at a puntable position scores below the same stud at one that cannot wait.
    plan_mode = my_budget_remaining is not None
    plan_bid_of: dict[str, int] = {}
    min_bid_marginal_of: dict[str, float] = {}
    if plan_mode:
        needs: list[frozenset[str]] = []
        for position in sorted(open_dedicated):
            needs.extend([frozenset({position})] * open_dedicated[position])
        for eligible, count in open_flex:
            needs.extend([eligible] * count)

        ladder_of = {
            position: _price_ladder(pool, demand_index_of(position), basis)
            for position, pool in by_position.items()
        }
        options = [
            [rung for position in sorted(need) for rung in ladder_of.get(position, [])]
            for need in needs
        ]

        plan_memo: dict[tuple, float] = {}
        budget_base = max(0, my_budget_remaining - bench_open * MIN_BID)
        base_plan = _plan_value(options, budget_base, memo=plan_memo)

        # The slot each position's purchase would release: a dedicated slot when open
        # (interchangeable, any one will do), else every eligible flex is tried and the
        # kindest release kept, else None -- he lands on the bench and hands back its $1.
        release_of: dict[str, list[int] | None] = {}
        for position in by_position:
            dedicated = [i for i, need in enumerate(needs) if need == frozenset({position})]
            if dedicated:
                release_of[position] = [dedicated[0]]
            else:
                flexes = [i for i, need in enumerate(needs) if position in need]
                release_of[position] = flexes or None

        after_cache: dict[tuple[str, int], float] = {}

        def plan_after(position: str, price: int) -> float:
            """Best fundable plan for everyone else, once he is bought at ``price``."""
            key = (position, price)
            cached = after_cache.get(key)
            if cached is None:
                spend = budget_base - price
                releases = release_of.get(position)
                if releases is None:
                    credit = MIN_BID if bench_open > 0 else 0
                    cached = _plan_value(options, spend + credit, memo=plan_memo)
                else:
                    cached = max(
                        _plan_value(options, spend, released=index, memo=plan_memo)
                        for index in releases
                    )
                after_cache[key] = cached
            return cached

        def plan_bid_for(position: str, adjusted: float) -> int:
            """Largest price at which buying still beats the plan without him.

            Capped at his own worth. The DP prices leftover dollars at zero -- money
            unspent buys nothing it can see -- so whenever the plan has slack it would
            happily endorse *any* price for *any* player, and a $24 bench body would
            show "bid to $45". Below worth the search carries the real signal ("stop
            at $87 though he is worth $90; the rest is spoken for"); above worth it
            only ever measured the slack.

            Both comparisons carry ``_PLAN_EPSILON`` because a marginal of exactly zero is
            the *common* case, not a coincidence: whenever the plan's own choice for the
            slot he would fill is him, buying him and planning to buy him are the same
            basket and the difference is algebraically nil. Reading that tie as "worse" --
            which is what a strict ``< 0`` did, on noise of order 1e-14 -- returned a bid of
            $0 for the best remaining player at any position whose dedicated slots were
            already full, and ``rank_key`` leads on ``bid_to <= 0``, so he did not merely
            look cheap, he left the short list entirely. Seen live: a back worth $51 with
            $34 still to spend, ranked 214th.

            Measured on the recorded auction (full replay / policy from pick 45 / compared
            through pick 95), strict ``< 0`` against the tolerance:

                policy       strict                      with epsilon
                engine       1460.4 / 1392.3 / 1430.8    1460.4 / 1442.5 / 1430.8
                engine_list  1367.9 / 1367.9 / 1367.9    1442.5 / 1442.5 / 1412.9

            The gain lands on ``engine_list`` because a ``bid_to`` of 0 does not merely
            misprice a player on the short list, it removes him from it.
            """
            ceiling = min(my_max_bid, _dollars(adjusted))
            if ceiling < MIN_BID:
                return 0
            if adjusted + plan_after(position, MIN_BID) - base_plan < -_PLAN_EPSILON:
                return 0
            low, high = MIN_BID, ceiling
            while low < high:
                mid = (low + high + 1) // 2
                if adjusted + plan_after(position, mid) - base_plan >= -_PLAN_EPSILON:
                    low = mid
                else:
                    high = mid - 1
            return low

    recommendations: list[AuctionRecommendation] = []
    for valuation in available:
        key = valuation.player_key
        par = values.value_of(key)
        adjusted = adjusted_of[key]

        market = basis.market_price(key)
        market_estimated = basis.is_inferred(key)
        surplus = (adjusted - market) if market is not None else None

        need = factor_for(valuation.position)
        held = roster_counts.get(valuation.position, 0)

        # You cannot win a player whose going rate is above your ceiling, however much you
        # like him. Rank those below everyone you can actually buy.
        #
        # Only a price from *outside* me can say I am priced out, which is exactly the
        # distinction ``PriceBasis`` draws: ``market_price`` is ``None`` when nothing but my
        # own sheet has an opinion, and "I value him above my remaining budget" is a
        # different statement from "I cannot win him". Reading the first as the second is
        # circular, and it sorted a player further down the more he was worth to me -- a
        # back worth $31, who sold for $14, ranked 214th of 457. Unknown price is no
        # evidence, not bad evidence; ``bid_to`` is still a minimum against ``my_max_bid``,
        # so nothing here can recommend a bid that cannot be made.
        # Not ``basis.estimate``: its only live consumer is the dart ceiling below, and
        # "he will go for pocket change" is a claim about an *individual*, which the class
        # docstring reserves for ``market_price``. Routing the room tier in here made the
        # gate say "I rank him low" instead of "he will be cheap" -- the mirror of the
        # original circularity, promoting players I value poorly rather than demoting ones
        # I value highly. Measured: it moved 32 of 206 players across the $3 ceiling.
        expected_price = market if market is not None else adjusted
        affordable = market is None or market <= my_max_bid

        if plan_mode and market is not None:
            # His value plus the plan with him bought, against the plan without him.
            price_paid = max(MIN_BID, _dollars(expected_price))
            marginal = adjusted + plan_after(valuation.position, price_paid) - base_plan
            score = penalized(marginal, need)
        else:
            # Blend mispricing against raw worth: chasing surplus alone builds a roster
            # of cheap sleepers and no studs; chasing worth alone means overpaying.
            # This is the whole score when my budget is unknown (no plan can be priced),
            # and it also carries any player with no market signal at all -- his only
            # price estimate is my own value, which makes the plan marginal identically
            # ~zero and worth the only ranking signal left.
            score = penalized(
                0.6 * (surplus if surplus is not None else 0.0) + 0.4 * adjusted, need
            )
        if plan_mode:
            # The break-even bid is computed either way, but how much it can say depends on
            # what priced the ladder. With real prices in it -- published, interpolated, or
            # the room's own -- "the price where buying him stops beating the rest of the
            # plan" is a genuine trade against the alternatives. With none of those the
            # rungs cost exactly what they are worth (see ``_price_ladder``), the plan can
            # no longer tell paying up from settling, and ``plan_bid`` degenerates to his
            # capped worth -- which is what the no-plan branch would have said anyway, so
            # it is harmless, just not the break-even the name promises.
            plan_bid_of[key] = plan_bid_for(valuation.position, adjusted)
            # Free: ``plan_bid_for`` evaluates this exact call on its first line and
            # ``after_cache`` memoizes it, so the second lookup costs a dict hit.
            #
            # Deliberately *not* run through ``penalized``, unlike ``score`` above. That
            # helper divides when its argument is negative -- a rank-order transform, on
            # purpose (see its docstring) -- and this field is read as **dollars**: it is
            # subtracted from another player's marginal and summed with ``drain_gain``.
            # Penalizing it mixed scales, because a marginal at ``need`` 0.04 came back 25x
            # magnified while the baseline it is compared against sat at ``need`` 1.0. A
            # consumer that wants the depth discount has ``depth_factor`` on the same row
            # and can apply it as a plain multiply, which preserves the unit.
            min_bid_marginal_of[key] = (
                adjusted + plan_after(valuation.position, MIN_BID) - base_plan
            )
        if valuation.is_injured:
            score = penalized(score, _INJURY_RESIDUAL)

        # Pocket-change darts get their variance priced in dollars; a bye stacked on a
        # thin position costs a real lineup week, also in dollars.
        dart = 0.0
        if expected_price <= _DART_PRICE_CEILING:
            dart = upside_bonus(valuation, roster_fullness=fullness) * values.dollars_per_vor
            score += dart
        bye_clash = False
        if roster_byes and valuation.bye_week is not None:
            same_bye = roster_byes.get(valuation.position, [])
            thin = held <= dedicated_starters.get(valuation.position, 0) + 1
            if valuation.bye_week in same_bye and thin:
                score -= _BYE_PENALTY * values.dollars_per_vor
                bye_clash = True

        smart_cap = smart_cap_for(valuation.position)

        recommendations.append(
            AuctionRecommendation(
                valuation=valuation,
                value=adjusted,
                par=par,
                market=market,
                surplus=surplus,
                max_bid=my_max_bid,
                affordable=affordable,
                depth_factor=need,
                score=score,
                market_estimated=market_estimated,
                smart_cap=smart_cap,
                plan_bid=plan_bid_of.get(key),
                budget_price=basis.estimate(key),
                price_basis=basis.tier(key),
                min_bid_marginal=min_bid_marginal_of.get(key),
                reason=_explain(
                    valuation,
                    adjusted,
                    market,
                    surplus,
                    inflation,
                    premiums,
                    need,
                    held,
                    affordable,
                    my_max_bid,
                    market_estimated,
                    smart_cap,
                    plan_bid_of.get(key),
                    dart=dart,
                    bye_clash=bye_clash,
                ),
            )
        )

    # Unaffordable players sort below every attainable one whatever their score says --
    # a tuple key, not a score offset, so no magic constant can ever be outscored.
    #
    # ``bid_to == 0`` leads, and it is a stricter test than ``affordable``: that flag asks
    # only whether the *hard* ceiling covers his price, while bid_to also carries the smart
    # cap and the budget plan. The two come apart exactly when money is tight, which is
    # when the list matters most -- a room where eleven of the top twelve rows read "bid
    # $0" is not a short list, and the players you can actually buy fall off the bottom of
    # it. Ranking, not valuation: nothing about what a player is worth changes here.
    # A board where every row reads "bid $0" is reachable and not rare -- any reservation
    # exceeding my budget zeroes every position's smart cap at once, and a room paying 1.5x
    # par is enough. The advice is right there (if my open starters really cost more than I
    # hold, "do not bid" is correct; softening the cap to avoid saying so measurably bought
    # worse players -- see ``starter_reserved``), and the *order* survives it too: with the
    # leading term constant, the tuple falls through to affordability and then score, which
    # is the right question when every answer to "can I afford him" is no. Tried special-
    # casing the all-zero board to sort by score explicitly; it is exactly what this already
    # does, and the backtest was identical to the digit.
    recommendations.sort(key=rank_key)
    return recommendations[:limit]


def rank_key(recommendation: AuctionRecommendation) -> tuple[bool, bool, float]:
    """Display order for the short list. Named so a backtest can A/B it.

    ``bid_to <= 0`` leads, and it is a stricter test than ``affordable``: that flag asks
    only whether the hard ceiling covers his going rate, while ``bid_to`` also carries the
    smart cap and the budget plan. The two come apart exactly when money is tight, which
    is when the list matters most -- a room where eleven of the top twelve rows read "bid
    $0" is not a short list, and the players you can actually buy fall off the bottom.

    ``affordable`` still ranks below that, but only where a market price exists to make it
    a real statement; see where it is computed for why an unknown price must count as no
    evidence rather than as unaffordable.
    """
    return (
        recommendation.bid_to <= 0,
        not recommendation.affordable,
        -recommendation.score,
    )


def _explain(
    valuation: PlayerValuation,
    adjusted: float,
    market: float | None,
    surplus: float | None,
    inflation: float,
    premiums: RoomPremiums,
    need: float,
    held: int,
    affordable: bool,
    my_max_bid: int,
    market_estimated: bool,
    smart_cap: int,
    plan_bid: int | None = None,
    *,
    dart: float = 0.0,
    bye_clash: bool = False,
) -> str:
    parts: list[str] = []

    if not affordable:
        # ``affordable`` is now set only from an exogenous market price, so this arm always
        # has one -- see where it is computed for why an unknown price cannot demote.
        parts.append(f"goes for about ${market:.0f}, above your ${my_max_bid} ceiling")
    elif market is None and _dollars(adjusted) > my_max_bid:
        # No published price, so this is deliberately not a claim about what he will cost
        # -- but a player worth several times the ceiling still needs saying, and the
        # ranking no longer demotes him for it. Without this the row reads as an ordinary
        # buy and renders undimmed next to players you can actually take.
        parts.append(
            f"worth about ${adjusted:.0f}, over your ${my_max_bid} ceiling (no market price)"
        )
    elif surplus is not None and surplus >= 5:
        parts.append(f"worth ${adjusted:.0f}, room pays about ${market:.0f}")
    elif surplus is not None and surplus <= -5:
        parts.append(f"market overpays: ${market:.0f} for ${adjusted:.0f} of value")
    else:
        parts.append(f"worth about ${adjusted:.0f}")

    premium = premiums.at(valuation.position)
    if premium >= 1.15 or premium <= 0.85:
        parts.append(f"room paying {premium:.0%} of sheet at {valuation.position}")

    if inflation >= 1.15:
        parts.append(f"prices running {inflation:.0%} of par")
    elif inflation <= 0.85:
        parts.append(f"bargains available, prices at {inflation:.0%} of par")

    if affordable and plan_bid is not None:
        worth = _dollars(adjusted)
        if plan_bid <= 0:
            parts.append("your plan spends every dollar better elsewhere")
        elif plan_bid < worth and plan_bid <= smart_cap:
            parts.append(f"plan says stop at ${plan_bid}; the rest is spoken for")
    # The smart cap gets its own line whenever it is the constraint that actually
    # binds -- an `elif` here left a $32-worth player showing "bid to $8" with no
    # explanation whenever a plan existed.
    if (
        affordable
        and smart_cap < my_max_bid
        and smart_cap < _dollars(adjusted)
        and (plan_bid is None or smart_cap < plan_bid)
    ):
        parts.append(f"cap ${smart_cap} to keep real money for your open starters")

    if bye_clash:
        parts.append(f"shares week {valuation.bye_week} bye with your {valuation.position}")
    if dart >= 1.0:
        parts.append("high-variance upside; late-round dart")

    if valuation.tier is not None:
        parts.append(f"tier {valuation.tier}")
    if need < 1.0:
        parts.append(f"no open slot for him; you hold {held} at {valuation.position}")
    if valuation.is_injured:
        if valuation.availability < 1.0:
            parts.append(
                f"projection cut {1 - valuation.availability:.0%} for {valuation.status}"
            )
        else:
            parts.append(f"injury status {valuation.status}")
    if valuation.points_estimated:
        parts.append("projection interpolated")
    if market_estimated:
        parts.append("market est.")

    return "; ".join(parts)
