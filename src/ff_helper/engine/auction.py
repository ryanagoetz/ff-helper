"""Auction drafting -- a different problem from a snake draft.

In a snake draft the scarce resource is *picks*, handed out in a fixed order, so the
question is "will he last until my turn?" and the answer is VONA.

In an auction nobody is ever unavailable, only unaffordable. Every player can be had by
any team, so scarcity of picks disappears entirely and is replaced by scarcity of
*dollars*. VONA is meaningless here: survival probability is 1.0 for everyone, forever.
The questions that replace it are:

1. **What is he worth, in dollars?** VOR converts to money once you know how much money
   exists and how much value it is chasing.
2. **What will the room pay?** That is Yahoo's ``average_cost`` -- the auction analog of
   ADP, and just as separate from value as ADP is.
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


def _price_ladder(
    pool: list[PlayerValuation],
    demand_index: int,
    expected_of: dict[str, float | None],
    adjusted_of: dict[str, float],
) -> list[tuple[int, float]]:
    """The realistic ways to fund one slot at this position: (price, my value) rungs.

    ``pool`` is the position's remaining players, best first by adjusted value. The top
    rung is the best player left at his going rate -- paying up is always one of the
    choices, which is exactly what ``slot_price``'s reservation cannot express. Below it
    sit the settle rungs around ``demand_index`` (the player the league's remaining
    demand realistically leaves me if I *don't* pay up -- what ``slot_price`` reserves)
    and the last man standing at $1. The point is not price precision but *choice*: a
    plan decides per slot whether to pay up, settle, or punt, and two same-position
    slots sharing the top rung is an accepted approximation -- ladders choose depth,
    not individual players.
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
        price = expected_of.get(chosen.player_key)
        if price is None:
            price = adjusted_of[chosen.player_key]
        ladder.append((max(MIN_BID, round(price)), adjusted_of[chosen.player_key]))
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
    # True when the market price was interpolated rather than published by a source.
    market_estimated: bool
    # Softer ceiling than max_bid: what you can pay and still fill your remaining
    # *starter* slots at realistic prices, not $1 apiece.
    smart_cap: int
    # The highest price at which buying him still beats spending the money on the rest
    # of the plan (None when budget info is missing and no plan could be computed). Can
    # exceed his raw worth: when his position cannot be punted, overpaying beats the
    # alternative -- which is exactly what a per-player value can never express.
    plan_bid: int | None = None

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
    adjusted_of: dict[str, float] = {}
    expected_of: dict[str, float | None] = {}
    for valuation in available:
        key = valuation.player_key
        par = values.value_of(key)
        adjusted_of[key] = MIN_BID + (par - MIN_BID) * inflation
        base = valuation.market_cost
        if base is None:
            base = estimated_markets.get(key)
        expected_of[key] = base * premiums.at(valuation.position) if base is not None else None

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
    # A starter slot reserves the going rate of the player I could realistically end up
    # with (the k-th best remaining, because k other open league slots compete for the
    # cheap ones); a bench slot reserves the $1 minimum, as the hard max_bid already does.
    by_position: dict[str, list[PlayerValuation]] = {}
    for valuation in available:
        by_position.setdefault(valuation.position, []).append(valuation)
    for pool in by_position.values():
        pool.sort(key=lambda v: -adjusted_of[v.player_key])

    league_counts = league_position_counts or {}

    def slot_price(position: str) -> float:
        pool = by_position.get(position)
        if not pool:
            return float(MIN_BID)
        demand = max(1, levels.starters_drafted.get(position, 0) - league_counts.get(position, 0))
        chosen = pool[min(demand - 1, len(pool) - 1)]
        price = expected_of.get(chosen.player_key)
        if price is None:
            price = adjusted_of[chosen.player_key]
        return max(float(MIN_BID), price)

    slot_prices = {position: slot_price(position) for position in by_position}
    flex_prices = [
        min((slot_prices.get(p, float(MIN_BID)) for p in eligible), default=float(MIN_BID))
        for eligible, _ in open_flex
    ]

    starter_reserved = sum(
        slot_prices.get(position, float(MIN_BID)) * count
        for position, count in open_dedicated.items()
    ) + sum(price * count for price, (_, count) in zip(flex_prices, open_flex, strict=True))
    starter_slots_open = sum(open_dedicated.values()) + sum(count for _, count in open_flex)
    my_open_slots = max(0, (settings.roster_size or 0) - sum(roster_counts.values()))
    bench_open = max(0, my_open_slots - starter_slots_open)
    total_reserved = starter_reserved + bench_open * MIN_BID

    def smart_cap_for(position: str) -> int:
        if my_budget_remaining is None:
            return my_max_bid
        # The candidate himself fills one open slot, so its reservation is released.
        if open_dedicated.get(position, 0) > 0:
            released = slot_prices.get(position, float(MIN_BID))
        else:
            flex_hits = [
                price
                for price, (eligible, count) in zip(flex_prices, open_flex, strict=True)
                if position in eligible and count > 0
            ]
            if flex_hits:
                released = flex_hits[0]
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
    if plan_mode:
        needs: list[frozenset[str]] = []
        for position in sorted(open_dedicated):
            needs.extend([frozenset({position})] * open_dedicated[position])
        for eligible, count in open_flex:
            needs.extend([eligible] * count)

        ladder_of = {
            position: _price_ladder(
                pool,
                max(1, levels.starters_drafted.get(position, 0) - league_counts.get(position, 0))
                - 1,
                expected_of,
                adjusted_of,
            )
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
            """Largest price at which buying still beats the plan without him."""
            if my_max_bid < MIN_BID:
                return 0
            if adjusted + plan_after(position, MIN_BID) - base_plan < 0:
                return 0
            low, high = MIN_BID, my_max_bid
            while low < high:
                mid = (low + high + 1) // 2
                if adjusted + plan_after(position, mid) - base_plan >= 0:
                    low = mid
                else:
                    high = mid - 1
            return low

    recommendations: list[AuctionRecommendation] = []
    for valuation in available:
        key = valuation.player_key
        par = values.value_of(key)
        adjusted = adjusted_of[key]

        market = expected_of[key]
        market_estimated = valuation.market_cost is None and market is not None
        surplus = (adjusted - market) if market is not None else None

        need = factor_for(valuation.position)
        held = roster_counts.get(valuation.position, 0)

        # You cannot win a player whose going rate is above your ceiling, however much you
        # like him. Rank those below everyone you can actually buy.
        expected_price = market if market is not None else adjusted
        affordable = expected_price <= my_max_bid

        if plan_mode and market is not None:
            # His value plus the plan with him bought, against the plan without him.
            price_paid = max(MIN_BID, round(expected_price))
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
            # The break-even bid stands either way: even without a market to rank
            # against, "the price where buying him stops beating the rest of the plan"
            # is exactly what to stop bidding at.
            plan_bid_of[key] = plan_bid_for(valuation.position, adjusted)
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
                    expected_price,
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
    recommendations.sort(key=lambda r: (not r.affordable, -r.score))
    return recommendations[:limit]


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
    expected_price: float,
    market_estimated: bool,
    smart_cap: int,
    plan_bid: int | None = None,
    *,
    dart: float = 0.0,
    bye_clash: bool = False,
) -> str:
    parts: list[str] = []

    if not affordable:
        # market can be absent (Yahoo publishes no auction cost for deep players and no
        # priced neighbor may exist to interpolate from), in which case our own valuation
        # is the best estimate of what he will go for.
        basis = "goes for about" if market is not None else "worth about"
        parts.append(f"{basis} ${expected_price:.0f}, above your ${my_max_bid} ceiling")
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
        worth = round(adjusted)
        if plan_bid <= 0:
            parts.append("your plan spends every dollar better elsewhere")
        elif plan_bid < worth:
            parts.append(f"plan says stop at ${plan_bid}; the rest is spoken for")
        elif worth < plan_bid < my_max_bid and round(expected_price) > worth:
            # The room prices him above his worth, yet the plan still pays: his slot
            # has no cheap fallback, and losing him costs more than the overage.
            parts.append(f"worth stretching to ${plan_bid}; his slot has no cheap fallback")
    elif affordable and smart_cap < my_max_bid and smart_cap < round(adjusted):
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
