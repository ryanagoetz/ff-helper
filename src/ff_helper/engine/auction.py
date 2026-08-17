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

from dataclasses import dataclass

from ff_helper.engine.replacement import ReplacementLevels
from ff_helper.engine.vona import depth_multiplier
from ff_helper.rankings.blend import PlayerValuation
from ff_helper.yahoo.models import LeagueSettings

# Every drafted player costs at least this much, so it is reserved off the top.
MIN_BID = 1

# Bounds on the inflation multiplier. Late in a draft the denominator gets small and the
# ratio can swing wildly on a single sale; clamping keeps a $2 kicker from being valued
# at $60 because three teams happen to have money left.
MIN_INFLATION = 0.25
MAX_INFLATION = 3.0


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


@dataclass(frozen=True)
class AuctionRecommendation:
    valuation: PlayerValuation
    value: float  # inflation-adjusted worth to you, in dollars
    par: float  # pre-draft par value, before inflation
    market: float | None  # what the room typically pays
    surplus: float | None  # value - market; the auction analog of VONA
    max_bid: int  # hard ceiling from your remaining budget and slots
    affordable: bool
    depth_factor: float
    score: float
    reason: str

    @property
    def name(self) -> str:
        return self.valuation.name

    @property
    def position(self) -> str:
        return self.valuation.position

    @property
    def bid_to(self) -> int:
        """The most you should actually bid: your worth for him, capped by what you can pay."""
        return int(min(self.max_bid, round(self.value)))


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
    limit: int = 8,
) -> list[AuctionRecommendation]:
    """Rank the remaining players by where your dollars go furthest."""
    inflation = inflation_factor(
        available,
        values,
        money_remaining=money_remaining,
        slots_remaining=slots_remaining,
    )

    recommendations: list[AuctionRecommendation] = []
    for valuation in available:
        par = values.value_of(valuation.player_key)
        adjusted = MIN_BID + (par - MIN_BID) * inflation

        market = valuation.market_cost
        surplus = (adjusted - market) if market is not None else None

        starters = max(1, settings.starters_at(valuation.position))
        held = roster_counts.get(valuation.position, 0)
        depth = depth_multiplier(held, starters)

        # You cannot win a player whose going rate is above your ceiling, however much you
        # like him. Rank those below everyone you can actually buy.
        expected_price = market if market is not None else adjusted
        affordable = expected_price <= my_max_bid

        # Blend mispricing against raw worth. Chasing surplus alone builds a roster of
        # cheap sleepers and no studs; chasing worth alone means overpaying at market.
        score = (0.6 * (surplus if surplus is not None else 0.0) + 0.4 * adjusted) * depth
        if valuation.is_injured:
            score *= 0.5
        if not affordable:
            score -= 1000.0  # sorts below every attainable player without losing order

        recommendations.append(
            AuctionRecommendation(
                valuation=valuation,
                value=adjusted,
                par=par,
                market=market,
                surplus=surplus,
                max_bid=my_max_bid,
                affordable=affordable,
                depth_factor=depth,
                score=score,
                reason=_explain(
                    valuation,
                    adjusted,
                    market,
                    surplus,
                    inflation,
                    depth,
                    held,
                    starters,
                    affordable,
                    my_max_bid,
                    expected_price,
                ),
            )
        )

    recommendations.sort(key=lambda r: -r.score)
    return recommendations[:limit]


def _explain(
    valuation: PlayerValuation,
    adjusted: float,
    market: float | None,
    surplus: float | None,
    inflation: float,
    depth: float,
    held: int,
    starters: int,
    affordable: bool,
    my_max_bid: int,
    expected_price: float,
) -> str:
    parts: list[str] = []

    if not affordable:
        # market can be absent (Yahoo publishes no auction cost for deep players), in
        # which case our own valuation is the best estimate of what he will go for.
        basis = "goes for about" if market is not None else "worth about"
        parts.append(f"{basis} ${expected_price:.0f}, above your ${my_max_bid} ceiling")
    elif surplus is not None and surplus >= 5:
        parts.append(f"worth ${adjusted:.0f}, room pays about ${market:.0f}")
    elif surplus is not None and surplus <= -5:
        parts.append(f"market overpays: ${market:.0f} for ${adjusted:.0f} of value")
    else:
        parts.append(f"worth about ${adjusted:.0f}")

    if inflation >= 1.15:
        parts.append(f"prices running {inflation:.0%} of par")
    elif inflation <= 0.85:
        parts.append(f"bargains available, prices at {inflation:.0%} of par")

    if valuation.tier is not None:
        parts.append(f"tier {valuation.tier}")
    if depth < 1.0:
        parts.append(f"you hold {held} at {valuation.position} (start {starters})")
    if valuation.is_injured:
        parts.append(f"injury status {valuation.status}")
    if valuation.points_estimated:
        parts.append("projection interpolated")

    return "; ".join(parts)
