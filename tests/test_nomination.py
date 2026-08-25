"""Tests for the nomination model.

This is the one engine whose objective is not my own value, so the invariants worth pinning
are different in kind from the auction tests next door. What would be silently wrong here
rather than loudly broken: a bidder gate that never actually excludes anyone, an overlap
term quietly collapsed to 1.0, a stuck term with the wrong sign (which costs a roster spot
and looks like advice), and the bargain motive firing on a board that has no external price
level to support it -- which is the permanent state of an offline league.
"""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from ff_helper.assistant import Assistant
from ff_helper.draft.state import DraftState
from ff_helper.engine.auction import Sale, compute_par_values, recommend_auction
from ff_helper.engine.nomination import (
    _MIN_DRAIN_PRICE,
    MOTIVE_BARGAIN,
    MOTIVE_DRAIN,
    RivalSeat,
    recommend_nominations,
)
from ff_helper.engine.replacement import ReplacementLevels
from tests.test_auction import BUDGET, auction_league, auction_settings, auction_teams
from tests.test_engine import player
from tests.test_web import NUM_TEAMS, build_snapshot

SETTINGS = auction_settings()
# Starting slots in the synthetic league: QB1 RB2 WR2 TE1 W/R/T1 K1 DEF1, then 6 bench.
ROSTER_SIZE = SETTINGS.roster_size


def board(*, priced: bool = True):
    """A small two-position board, optionally carrying published auction prices.

    ``build_snapshot`` leaves ``market_cost`` at ``None`` for every player, which makes the
    priceless path the default everywhere in this suite -- convenient, since that is the
    state the user's own offline league is permanently in. Pass ``priced=True`` when a test
    needs an external price level to exist at all, which is what the bargain motive
    requires.
    """
    levels = ReplacementLevels(
        points={"RB": 100.0, "WR": 100.0},
        starters_drafted={"RB": 36, "WR": 36},
    )
    pool = [player(f"RB{i}", "RB", 260 - i * 8, adp=i + 1) for i in range(12)]
    pool += [player(f"WR{i}", "WR", 255 - i * 8, adp=i + 1) for i in range(12)]
    if priced:
        # Published costs, descending with quality, so ``market_price`` is real evidence.
        # Per position, so the two ladders are independent and nobody is priced below $1.
        pool = [
            replace(p, market_cost=max(1.0, 60.0 - (i % 12) * 4.0)) for i, p in enumerate(pool)
        ]
    values = compute_par_values(pool, levels, SETTINGS, NUM_TEAMS)
    return pool, levels, values


def recommendations(
    pool,
    levels,
    values,
    *,
    my_roster=None,
    my_budget=BUDGET,
    my_max_bid=BUDGET,
    sales=None,
):
    return recommend_auction(
        pool,
        levels,
        values,
        SETTINGS,
        my_roster or {},
        money_remaining=NUM_TEAMS * BUDGET,
        slots_remaining=NUM_TEAMS * ROSTER_SIZE,
        my_max_bid=my_max_bid,
        my_budget_remaining=my_budget,
        sales=sales,
        limit=len(pool),
    )


def seat(index: int, *, counts=None, max_bid: int = 100) -> RivalSeat:
    return RivalSeat(
        team_key=f"461.l.1.t.{index}",
        name=f"Team {index}",
        roster_counts=dict(counts or {}),
        max_bid=max_bid,
    )


def nominate(pool, levels, values, rivals, **kwargs):
    picks = recommendations(
        pool,
        levels,
        values,
        my_roster=kwargs.get("my_roster"),
        my_budget=kwargs.get("my_budget", BUDGET),
        my_max_bid=kwargs.get("my_max_bid", BUDGET),
        sales=kwargs.get("sales"),
    )
    return recommend_nominations(
        picks,
        SETTINGS,
        kwargs.get("my_roster") or {},
        rivals,
        my_budget_remaining=kwargs.get("my_budget", BUDGET),
        my_slots_remaining=kwargs.get("my_slots", ROSTER_SIZE),
        league_money_remaining=kwargs.get("league_money", NUM_TEAMS * BUDGET),
        limit=kwargs.get("limit", len(pool)),
    )


def find(candidates, name):
    return next(candidate for candidate in candidates if candidate.name == name)


class TestLiveBidders:
    """The bidder gate is the only term carrying information my own sheet does not have."""

    def test_a_rival_who_cannot_pay_is_not_a_live_bidder(self):
        """Needing the position is not enough; the money is a hard constraint.

        ``DraftState.max_bid`` is what a team can bid and still fill a legal roster, so a
        rival below the going rate genuinely cannot take him. Dropping this half of the
        gate would name every needy team and make ``aim`` meaningless.
        """
        pool, levels, values = board()
        rivals = [seat(1, max_bid=5), seat(2, max_bid=180)]
        candidates = nominate(pool, levels, values, rivals)
        top_rb = find(candidates, "RB0")
        assert top_rb.expected_price > 5
        assert top_rb.live_bidders == ("Team 2",)

    def test_a_rival_with_no_hole_at_the_position_is_not_a_live_bidder(self):
        """Money without a slot to fill is not competition, however much of it there is."""
        pool, levels, values = board()
        # Two backs and a flex-eating third: no dedicated RB slot and no flex left.
        full = {"RB": 3, "WR": 2, "QB": 1, "TE": 1, "K": 1, "DEF": 1}
        rivals = [seat(1, counts=full, max_bid=200), seat(2, counts={"RB": 1}, max_bid=200)]
        candidates = nominate(pool, levels, values, rivals)
        assert find(candidates, "RB0").live_bidders == ("Team 2",)

    def test_the_bidder_set_shrinks_as_rivals_fill_the_position(self):
        pool, levels, values = board()
        empty = [seat(i) for i in range(1, 5)]
        # Three backs fills both dedicated RB slots and the flex, so RB stops being a need.
        sated = [seat(i, counts={"RB": 3, "WR": 2}) for i in range(1, 5)]
        wide = find(nominate(pool, levels, values, empty), "RB0")
        narrow = find(nominate(pool, levels, values, sated), "RB0")
        assert len(narrow.live_bidders) < len(wide.live_bidders)


class TestOverlap:
    def test_overlap_is_load_bearing(self):
        """Same player, same price, same pockets -- only what the rivals need differs.

        Draining a rival who is shopping for backs when I need backs is worth more than
        draining one who is set at the position. If ``_overlap`` were ever collapsed to a
        constant these two boards would score identically, and the whole per-team half of
        this model would be doing nothing while still looking like it worked.
        """
        pool, levels, values = board()
        # RB is the only thing I still need: the third receiver has taken my flex.
        mine = {"WR": 3, "QB": 1, "TE": 2, "K": 1, "DEF": 1}
        # Both rival sets need a receiver, so both are live bidders on the same candidate.
        # Only one of them is also shopping for backs, which is where my money has to go.
        competing = [seat(i, counts={"QB": 1, "TE": 1, "K": 1, "DEF": 1}) for i in range(1, 5)]
        elsewhere = [
            seat(i, counts={"RB": 3, "QB": 1, "TE": 1, "K": 1, "DEF": 1}) for i in range(1, 5)
        ]
        contested = find(nominate(pool, levels, values, competing, my_roster=mine), "WR0")
        apart = find(nominate(pool, levels, values, elsewhere, my_roster=mine), "WR0")
        assert contested.live_bidders == apart.live_bidders, "fixture changed the wrong thing"
        assert contested.aim > apart.aim
        assert contested.drain_gain > apart.drain_gain


class TestMotives:
    def test_the_short_list_does_not_name_my_own_buy_targets(self):
        """A drain is a player I do not want, and my top buys are the opposite of that.

        The property that makes this work is the symmetry between the two branches: the
        same claim that would make a stud a happy accident to be stuck with is what I lose
        when a rival takes him, so wanting him is charged on both sides. Without it the
        drain term scales with price and the list converges on my own board.
        """
        pool, levels, values = board()
        picks = recommendations(pool, levels, values)
        targets = {pick.name for pick in sorted(picks, key=lambda p: -p.score)[:3]}
        rivals = [seat(i) for i in range(1, 12)]
        named = {c.name for c in nominate(pool, levels, values, rivals, limit=6)}
        assert not (targets & named)

    def test_a_player_who_would_waste_a_slot_is_not_the_top_nomination(self):
        """The sign of the stuck term, which would be silently backwards.

        Nobody can bid, so whoever I nominate is mine at $1. A *marginal* back at a
        position I have already filled is then a wasted roster spot, and the model has to
        charge for that rather than credit it -- a positive there would cheerfully
        recommend filling my bench with players I cannot start.

        The sign only exists because ``_slot_floor`` measures him against the $1 filler
        that slot would otherwise hold; ``min_bid_marginal`` on its own is positive for
        *everybody*, which is exactly the trap this pins. Note the model rightly refuses to
        over-apply the rule: the best back in the draft is a fine accident to be stuck with
        at $1 even with the position full, and only the players genuinely worse than a
        filler go negative.
        """
        pool, levels, values = board()
        # Every rival broke: no bidder can reach anyone, so p_stuck is 1.0 for all.
        rivals = [seat(i, max_bid=1) for i in range(1, 12)]
        # RB, WR and the flex are all full; a fifth back can only sit on the bench.
        mine = {"RB": 4, "WR": 2, "QB": 1, "TE": 1, "K": 1, "DEF": 1}
        candidates = nominate(pool, levels, values, rivals, my_roster=mine)
        filler = find(candidates, "RB11")
        assert filler.stuck_probability == pytest.approx(1.0)
        assert filler.stuck_value < 0
        assert candidates[-1].name == "RB11", "the worst body should sort last, not first"

    def test_bargain_fires_when_the_room_cannot_reach_a_player_i_want(self):
        """The other motive: put him up precisely because nobody can bid."""
        pool, levels, values = board()
        rivals = [seat(i, max_bid=2) for i in range(1, 12)]
        mine = {"QB": 1, "TE": 1, "K": 1, "DEF": 1}
        candidates = nominate(pool, levels, values, rivals, my_roster=mine)
        top = candidates[0]
        assert top.motive == MOTIVE_BARGAIN
        assert top.stuck_probability == pytest.approx(1.0)
        assert top.stuck_value > 0


class TestDrainShare:
    def test_draining_is_worth_more_when_more_of_the_money_is_mine(self):
        """``drain_share`` is derived from the inflation accounting, not a tuned weight.

        Same board and the same rivals; only my share of the money left in the room
        changes. A rival's $30 deflates what everyone still buying pays, and my slice of
        that is my slice of the remaining money -- so the same drain is worth more to a
        team holding half the room's cash than to one holding a twelfth of it. Pinning this
        stops the ratio being "simplified" into a constant.
        """
        pool, levels, values = board()
        rivals = [seat(i) for i in range(1, 12)]
        # A back well outside the three my plan is reaching for, so a drain is on offer.
        poor = find(nominate(pool, levels, values, rivals, my_budget=20), "RB6")
        rich = find(nominate(pool, levels, values, rivals, my_budget=180), "RB6")
        assert poor.drain_gain > 0
        assert rich.drain_gain > poor.drain_gain

    def test_a_position_you_still_need_is_suppressed_as_a_whole(self):
        """The term that separates this list from the buy list, and the reason it works.

        Nominating into a tier you are still shopping in sets that tier's price while the
        room is at its richest, so the suppression is positional -- every back, not just the
        best one. An earlier version zeroed only the top ``depth`` players at a position;
        competing backs are substitutes for one slot, so with one open RB hole it protected
        a single name while the buy list still ranked five, and the other four rose to the
        top of the panel. On the real 2026 record that put buys #3, #4, #5 and #8 into
        nominations #2-#5.
        """
        pool, levels, values = board()
        rivals = [seat(i) for i in range(1, 12)]
        # Receivers are done; a back is the only thing left to buy.
        mine = {"WR": 3, "QB": 1, "TE": 2, "K": 1, "DEF": 1}
        candidates = nominate(pool, levels, values, rivals, my_roster=mine)
        backs = [c for c in candidates if c.position == "RB"]
        receivers = [c for c in candidates if c.position == "WR"]
        assert backs and receivers
        # Every back is suppressed, not merely the best one.
        assert all(c.drain_gain == 0.0 for c in backs)
        assert all(c.drain_gain > 0.0 for c in receivers)
        # And no back outranks a receiver, however expensive the back is.
        assert max(c.expected_price for c in backs) > min(c.expected_price for c in receivers)
        assert candidates.index(receivers[0]) < candidates.index(backs[0])

    def test_the_suppression_has_no_cliff_between_adjacent_players(self):
        """Two interchangeable players a cent apart must not get opposite advice.

        The per-player gate cut on ``ranked[:depth]``, which sits on adjacent floats: on the
        real board the last protected receiver graded 27.77 and the app's top nomination was
        the next one at 27.76. Positional pressure has no such boundary, so consecutive
        players at one position differ smoothly.
        """
        pool, levels, values = board()
        rivals = [seat(i) for i in range(1, 12)]
        ranked = [c for c in nominate(pool, levels, values, rivals) if c.position == "WR"]
        ranked.sort(key=lambda c: -c.recommendation.value)
        gains = [c.drain_gain for c in ranked]
        # Strictly no zero-to-full step anywhere in the ladder.
        pairs = zip(gains, gains[1:], strict=False)  # pairwise: lengths differ by one
        assert all(
            (later == 0.0) == (earlier == 0.0) for earlier, later in pairs
        ), "a cliff reappeared in the drain gate"


class TestPricelessBoard:
    """The user's actual league: offline, no auction column, no published price anywhere."""

    def test_no_row_claims_a_bargain_without_an_external_price(self):
        """The honesty gate, and the reason the suppression rule exists.

        With every ``PriceBasis`` tier empty, the price *is* my own par -- so "nobody can
        reach him" fires hardest on exactly my own top players, and an unclipped bargain
        term would recommend putting up the studs I most want to buy cheap. The cost of
        getting stuck survives; the upside claim does not.
        """
        pool, levels, values = board(priced=False)
        rivals = [seat(i, max_bid=2) for i in range(1, 12)]
        candidates = nominate(pool, levels, values, rivals)
        assert candidates, "fixture no longer poses the question"
        assert all(c.recommendation.price_basis == "own" for c in candidates)
        assert all(c.motive == MOTIVE_DRAIN for c in candidates)

    def test_the_room_tier_lifts_the_suppression(self):
        """Once the room has priced a position, the level is real money again."""
        pool, levels, values = board(priced=False)
        sales = [Sale(position="RB", price=30.0, expected=20.0) for _ in range(20)]
        candidates = nominate(pool, levels, values, [seat(1)], sales=sales)
        backs = [c for c in candidates if c.position == "RB"]
        assert backs, "no backs priced by the room"
        assert all(c.recommendation.price_basis == "room" for c in backs)

    def test_waste_vanishes_without_a_market_price(self):
        """``surplus`` is None offline, so the overpay term goes quiet rather than guessing."""
        pool, levels, values = board(priced=False)
        candidates = nominate(pool, levels, values, [seat(1)])
        assert all(c.recommendation.surplus is None for c in candidates)
        for candidate in candidates:
            expected = candidate.expected_price * candidate.aim
            assert candidate.drain == pytest.approx(expected)


class TestListShape:
    def test_pocket_change_is_not_a_nomination(self):
        pool, levels, values = board()
        candidates = nominate(pool, levels, values, [seat(1)])
        assert all(c.expected_price >= _MIN_DRAIN_PRICE for c in candidates)

    def test_the_limit_is_respected_and_the_order_is_by_score(self):
        pool, levels, values = board()
        candidates = nominate(pool, levels, values, [seat(i) for i in range(1, 12)], limit=4)
        assert len(candidates) == 4
        assert [c.score for c in candidates] == sorted((c.score for c in candidates), reverse=True)

    def test_a_full_roster_has_nothing_to_nominate_for(self):
        pool, levels, values = board()
        assert nominate(pool, levels, values, [seat(1)], my_slots=0) == []

    def test_no_rivals_at_all_is_survivable(self):
        pool, levels, values = board()
        candidates = nominate(pool, levels, values, [])
        assert all(c.aim == 0.0 for c in candidates)
        assert all(c.live_bidders == () for c in candidates)

    def test_a_board_with_no_budget_plan_still_ranks(self):
        """Without a budget nothing can price "I keep him for $1", so only drain survives."""
        pool, levels, values = board()
        picks = recommend_auction(
            pool,
            levels,
            values,
            SETTINGS,
            {},
            money_remaining=NUM_TEAMS * BUDGET,
            slots_remaining=NUM_TEAMS * ROSTER_SIZE,
            my_max_bid=BUDGET,
            limit=len(pool),
        )
        assert all(pick.min_bid_marginal is None for pick in picks)
        candidates = recommend_nominations(
            picks,
            SETTINGS,
            {},
            [seat(1)],
            my_budget_remaining=BUDGET,
            my_slots_remaining=ROSTER_SIZE,
            league_money_remaining=NUM_TEAMS * BUDGET,
        )
        assert candidates
        assert all(c.stuck_value == 0.0 for c in candidates)
        assert all(c.motive == MOTIVE_DRAIN for c in candidates)


class TestAssistantWiring:
    """The whole-pool contract, at the level where it is easy to get wrong."""

    @pytest.fixture
    def live(self) -> Assistant:
        league = auction_league()
        state = DraftState(league=league, teams=auction_teams())
        return Assistant.build(league, state, build_snapshot(), lock=threading.Lock())

    def test_the_list_names_players_the_buy_list_does_not(self, live):
        """A drain is by construction a player the buy list ranks low.

        Handing ``recommend_nominations`` a display short list would return exactly the
        players it exists to steer you away from nominating, and the bug would be invisible
        -- the panel would render, the numbers would be plausible, and every suggestion
        would be your own best buy.
        """
        buys = {pick.name for pick in live.auction_recommendations(limit=8)}
        named = {candidate.name for candidate in live.nomination_list(limit=8)}
        assert named, "no nominations at all"
        assert named - buys, "the nomination list is a copy of the buy list"

    def test_it_never_names_a_drafted_player(self, live):
        first = live.nomination_list(limit=1)[0]
        with live.lock:
            live.state.record_manual(first.player_key, cost=20, team_key="461.l.1.t.1")
        assert first.player_key not in {c.player_key for c in live.nomination_list(limit=20)}

    def test_a_snake_league_has_no_nominations(self):
        from tests.test_web import build_league as snake_league

        league = snake_league()
        state = DraftState(league=league, teams=auction_teams())
        assistant = Assistant.build(league, state, build_snapshot(), lock=threading.Lock())
        assert assistant.nomination_list() == []
