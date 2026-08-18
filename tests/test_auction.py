"""Tests for the auction model.

An auction is not a variant of a snake draft, so this is a genuinely separate path:
scarcity of picks disappears (nobody is unavailable, only unaffordable) and is replaced by
scarcity of dollars. These tests pin the parts that would be silently wrong rather than
loudly broken -- dollar conversion, live inflation, and the max-bid ceiling.
"""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ff_helper.assistant import Assistant
from ff_helper.draft.state import DraftState
from ff_helper.engine.auction import (
    MAX_INFLATION,
    MIN_BID,
    DollarValues,
    Sale,
    compute_par_values,
    inflation_factor,
    need_factor,
    recommend_auction,
    room_premiums,
)
from ff_helper.engine.replacement import ReplacementLevels
from ff_helper.web.app import create_app
from ff_helper.yahoo.models import DraftPick, League, LeagueSettings, Team
from ff_helper.yahoo.parse import content as strip_envelope
from ff_helper.yahoo.parse import parse_league, unwrap
from tests.test_engine import player
from tests.test_engine import settings as engine_settings
from tests.test_web import (
    MY_SLOT,
    NUM_TEAMS,
    POSITION_POOL,
    build_league,
    build_snapshot,
)

BUDGET = 200


def auction_settings(budget: int = BUDGET) -> LeagueSettings:
    base = build_league().settings
    return LeagueSettings(
        roster_slots=base.roster_slots,
        stat_modifiers=base.stat_modifiers,
        is_auction=True,
        auction_budget=budget,
    )


def auction_league(budget: int = BUDGET) -> League:
    base = build_league()
    return League(
        league_key=base.league_key,
        league_id=base.league_id,
        name="Synthetic Auction",
        num_teams=NUM_TEAMS,
        season=base.season,
        draft_status="drafting",
        scoring_type=base.scoring_type,
        settings=auction_settings(budget),
    )


def auction_teams() -> list[Team]:
    return [
        Team(
            team_key=f"461.l.1.t.{i}",
            team_id=str(i),
            name=f"Team {i}",
            is_mine=(i == MY_SLOT),
            draft_position=i,
        )
        for i in range(1, NUM_TEAMS + 1)
    ]


@pytest.fixture
def assistant() -> Assistant:
    league = auction_league()
    state = DraftState(league=league, teams=auction_teams())
    return Assistant.build(league, state, build_snapshot(), lock=threading.Lock())


@pytest.fixture
def client(assistant) -> TestClient:
    return TestClient(create_app(assistant, sync=None))


@pytest.fixture
def my_key() -> str:
    return f"461.l.1.t.{MY_SLOT}"


class TestSettingsParsing:
    def test_auction_draft_type_is_detected(self):
        payload = {
            "fantasy_content": {
                "league": [
                    {"league_key": "461.l.1", "num_teams": 12},
                    {"settings": [{"draft_type": "auction", "roster_positions": []}]},
                ]
            }
        }
        league = parse_league(unwrap(strip_envelope(payload), "league"))
        assert league.settings.is_auction

    def test_budget_is_read_from_settings_when_present(self):
        payload = {
            "fantasy_content": {
                "league": [
                    {"league_key": "461.l.1"},
                    {
                        "settings": [
                            {
                                "draft_type": "auction",
                                "roster_positions": [],
                                "auction_budget_total": "300",
                            }
                        ]
                    },
                ]
            }
        }
        league = parse_league(unwrap(strip_envelope(payload), "league"))
        assert league.settings.auction_budget == 300

    def test_falls_back_to_the_platform_default(self):
        payload = {
            "fantasy_content": {
                "league": [
                    {"league_key": "461.l.1"},
                    {"settings": [{"draft_type": "auction", "roster_positions": []}]},
                ]
            }
        }
        league = parse_league(unwrap(strip_envelope(payload), "league"))
        assert league.settings.auction_budget == 200


class TestBudgetTracking:
    def test_spend_and_remaining(self, assistant, my_key):
        state = assistant.state
        assert state.budget_remaining(my_key) == BUDGET

        state.record_manual("461.p.RB0", pick=1, cost=54, team_key=my_key)
        assert state.spent(my_key) == 54
        assert state.budget_remaining(my_key) == BUDGET - 54

    def test_max_bid_reserves_a_dollar_for_every_open_slot(self, assistant, my_key):
        state = assistant.state
        # 15 roster spots, $200. Buy nobody yet: you must keep $1 for the other 14.
        assert state.slots_remaining(my_key) == 15
        assert state.max_bid(my_key) == BUDGET - 14

    def test_max_bid_shrinks_as_money_goes_out(self, assistant, my_key):
        state = assistant.state
        state.record_manual("461.p.RB0", pick=1, cost=100, team_key=my_key)
        # $100 left, 14 spots to fill, so $13 must stay in reserve.
        assert state.max_bid(my_key) == 100 - 13

    def test_max_bid_is_zero_once_the_roster_is_full(self, assistant, my_key):
        state = assistant.state
        for index in range(15):
            state.record_manual(f"461.p.RB{index}", pick=index + 1, cost=1, team_key=my_key)
        assert state.slots_remaining(my_key) == 0
        assert state.max_bid(my_key) == 0

    def test_league_money_tracks_every_team(self, assistant):
        state = assistant.state
        assert state.league_money_remaining() == BUDGET * NUM_TEAMS
        state.record_manual("461.p.RB0", pick=1, cost=60, team_key="461.l.1.t.2")
        assert state.league_money_remaining() == BUDGET * NUM_TEAMS - 60

    def test_manual_auction_pick_keeps_its_price(self, assistant, my_key):
        entry = assistant.state.record_manual("461.p.RB0", pick=1, cost=42, team_key=my_key)
        assert entry.cost == 42
        assert entry.team_key == my_key


class TestDollarValues:
    def test_total_par_value_matches_the_money_in_the_league(self, assistant):
        values = assistant.dollars
        assert values is not None

        settings = assistant.league.settings
        pool_size = NUM_TEAMS * settings.roster_size
        top = sorted(
            assistant.valuations.valuations.values(),
            key=lambda v: -assistant.levels.vor(v),
        )[:pool_size]

        # Every dollar in the league should be allocated across the players who get
        # rostered -- otherwise the studs are priced wrong.
        total = sum(values.value_of(v.player_key) for v in top)
        assert total == pytest.approx(NUM_TEAMS * BUDGET, rel=0.02)

    def test_nobody_is_valued_below_the_minimum_bid(self, assistant):
        values = assistant.dollars
        assert all(value >= MIN_BID for value in values.par.values())

    def test_better_players_cost_more(self, assistant):
        values = assistant.dollars
        ranked = sorted(
            assistant.valuations.valuations.values(),
            key=lambda v: -assistant.levels.vor(v),
        )
        assert values.value_of(ranked[0].player_key) > values.value_of(ranked[50].player_key)

    def test_a_bigger_budget_scales_values_up(self):
        league = auction_league(budget=400)
        state = DraftState(league=league, teams=auction_teams())
        rich = Assistant.build(league, state, build_snapshot(), lock=threading.Lock())

        poor_league = auction_league(budget=200)
        poor = Assistant.build(
            poor_league,
            DraftState(league=poor_league, teams=auction_teams()),
            build_snapshot(),
            lock=threading.Lock(),
        )

        best = max(rich.valuations.valuations.values(), key=lambda v: rich.levels.vor(v)).player_key
        assert rich.dollars.value_of(best) > poor.dollars.value_of(best) * 1.5


class TestInflation:
    def test_par_is_neutral_before_anything_sells(self, assistant):
        assert assistant.current_inflation() == pytest.approx(1.0, abs=0.05)

    def test_overspending_early_creates_bargains(self, assistant):
        """If the room blows its budget on studs, what is left gets cheaper.

        This is the number a pre-draft cheat sheet cannot give you, and the reason to
        recompute after every sale rather than drafting off a printed list.
        """
        state = assistant.state
        ranked = sorted(
            assistant.valuations.valuations.values(),
            key=lambda v: -assistant.levels.vor(v),
        )
        # Twelve teams each drop $150 on one player.
        for index in range(NUM_TEAMS):
            state.record_manual(
                ranked[index].player_key,
                pick=index + 1,
                cost=150,
                team_key=f"461.l.1.t.{index + 1}",
            )

        assert assistant.current_inflation() < 0.9

    def test_underspending_early_inflates_the_survivors(self, assistant):
        state = assistant.state
        ranked = sorted(
            assistant.valuations.valuations.values(),
            key=lambda v: -assistant.levels.vor(v),
        )
        # The room buys the best players for a pittance; the money is still out there.
        for index in range(NUM_TEAMS * 3):
            state.record_manual(
                ranked[index].player_key,
                pick=index + 1,
                cost=1,
                team_key=f"461.l.1.t.{index % NUM_TEAMS + 1}",
            )

        assert assistant.current_inflation() > 1.1

    def test_inflation_is_clamped(self, assistant):
        # Late in a draft the denominator gets tiny and the raw ratio explodes; a $2
        # kicker must not end up valued at $60.
        available = list(assistant.valuations.valuations.values())[:1]
        factor = inflation_factor(
            available, assistant.dollars, money_remaining=2000, slots_remaining=1
        )
        assert factor <= MAX_INFLATION


class TestAuctionRecommendations:
    def test_recommendations_respect_the_max_bid_ceiling(self, assistant, my_key):
        state = assistant.state
        # Spend almost everything, leaving a tiny ceiling.
        state.record_manual("461.p.RB0", pick=1, cost=185, team_key=my_key)

        picks = assistant.auction_recommendations(limit=5)
        assert picks
        assert picks[0].affordable
        assert picks[0].bid_to <= state.max_bid(my_key)

    def test_unaffordable_players_rank_below_attainable_ones(self, assistant, my_key):
        state = assistant.state
        state.record_manual("461.p.RB1", pick=1, cost=190, team_key=my_key)

        picks = assistant.auction_recommendations(limit=20)
        affordable = [index for index, pick in enumerate(picks) if pick.affordable]
        unaffordable = [index for index, pick in enumerate(picks) if not pick.affordable]
        if affordable and unaffordable:
            assert max(affordable) < min(unaffordable)

    def test_surplus_beats_raw_price(self, assistant):
        """A cheap player the room underrates should outrank an expensive fair-priced one.

        This is the auction analog of VONA: not "who lasts" but "who is mispriced".
        """
        levels = assistant.levels
        settings = assistant.league.settings
        values = compute_par_values(
            list(assistant.valuations.valuations.values()), levels, settings, NUM_TEAMS
        )

        pool = sorted(assistant.valuations.valuations.values(), key=lambda v: -levels.vor(v))[:40]

        # Same worth, wildly different going rates.
        bargain = pool[10]
        fair = pool[11]
        priced = {
            bargain.player_key: values.value_of(bargain.player_key) * 0.5,
            fair.player_key: values.value_of(fair.player_key) * 1.4,
        }
        adjusted = [
            type(valuation)(
                **{
                    **valuation.__dict__,
                    "market_cost": priced.get(valuation.player_key, 5.0),
                }
            )
            for valuation in (bargain, fair)
        ]

        picks = recommend_auction(
            adjusted,
            levels,
            values,
            settings,
            {},
            money_remaining=NUM_TEAMS * BUDGET,
            slots_remaining=NUM_TEAMS * settings.roster_size,
            my_max_bid=BUDGET,
            limit=2,
        )
        assert picks[0].valuation.player_key == bargain.player_key
        assert picks[0].surplus > picks[1].surplus

    def test_roster_needs_still_apply(self, assistant, my_key):
        state = assistant.state
        rb_keys = [
            key for key, value in assistant.valuations.valuations.items() if value.position == "RB"
        ][:4]
        for index, key in enumerate(rb_keys):
            state.manual[100 + index] = DraftPick(
                pick=100 + index, round=1, team_key=my_key, player_key=key, cost=5
            )

        assert state.my_roster_counts(assistant.position_of).get("RB") == 4
        assert assistant.auction_recommendations(limit=1)[0].position != "RB"

    def test_every_recommendation_carries_a_reason(self, assistant):
        assert all(pick.reason for pick in assistant.auction_recommendations(limit=8))


def dollar_values(par: dict[str, float]) -> DollarValues:
    return DollarValues(par=par, dollars_per_vor=1.0, pool_size=len(par))


class TestRoomPremiums:
    def test_no_sales_is_neutral(self):
        assert room_premiums([]).at("RB") == 1.0

    def test_consistent_overpaying_raises_the_premium(self):
        # The room pays 150% of sheet for ten straight running backs. The RB premium
        # should move most, the room-wide one less, and shrinkage keeps both shy of 1.5.
        sales = [Sale("RB", price=30.0, expected=20.0) for _ in range(10)]
        premiums = room_premiums(sales)
        assert 1.0 < premiums.at("WR") < premiums.at("RB") < 1.5

    def test_minimum_bid_sales_say_nothing(self):
        # $1 players selling for $5 is pocket change, not a 400% market signal.
        sales = [Sale("RB", price=5.0, expected=1.0) for _ in range(10)]
        assert room_premiums(sales).at("RB") == 1.0

    def test_one_wild_price_is_clamped(self):
        sales = [Sale("RB", price=500.0, expected=5.0)]
        premiums = room_premiums(sales)
        assert premiums.at("RB") < 2.5
        assert premiums.at("WR") < 1.5


class TestNeedFactor:
    def test_open_dedicated_slot_is_full_value(self):
        assert need_factor({}, "RB", engine_settings()) == 1.0

    def test_flex_keeps_a_position_live_after_dedicated_slots_fill(self):
        assert need_factor({"RB": 2}, "RB", engine_settings()) == 1.0

    def test_a_flex_spoken_for_by_another_position_is_not_counted(self):
        # The old starters_at logic called this a 3-RB league even though three
        # receivers already occupy the second WR slot and the flex.
        assert need_factor({"RB": 2, "WR": 3}, "RB", engine_settings()) < 1.0

    def test_backups_decay(self):
        first = need_factor({"RB": 3}, "RB", engine_settings())
        second = need_factor({"RB": 4}, "RB", engine_settings())
        assert 1.0 > first > second


class TestMarketEstimation:
    def _pool(self, *, overpaid: bool = False):
        levels = ReplacementLevels(points={"RB": 100.0}, starters_drafted={"RB": 30})
        pool = [player(f"RB{i}", "RB", 250 - i * 10, adp=i + 1) for i in range(8)]
        values = compute_par_values(pool, levels, auction_settings(), NUM_TEAMS)
        priced = []
        for index, valuation in enumerate(pool):
            if index == 3:
                priced.append(valuation)  # nobody published a price for him
            elif overpaid:
                cost = values.value_of(valuation.player_key) * 1.3
                priced.append(replace(valuation, market_cost=cost))
            else:
                priced.append(replace(valuation, market_cost=60.0 - index * 7))
        return priced, levels, values

    def _recommend(self, pool, levels, values):
        return recommend_auction(
            pool,
            levels,
            values,
            auction_settings(),
            {},
            money_remaining=NUM_TEAMS * BUDGET,
            slots_remaining=NUM_TEAMS * auction_settings().roster_size,
            my_max_bid=BUDGET,
            limit=len(pool),
        )

    def test_unpriced_players_get_an_interpolated_market(self):
        pool, levels, values = self._pool()
        picks = self._recommend(pool, levels, values)
        target = next(pick for pick in picks if pick.name == "RB3")
        assert target.market_estimated
        # Between his par-value neighbours' published prices ($46 and $32).
        assert 32.0 <= target.market <= 46.0
        assert "market est." in target.reason

    def test_an_unpriced_player_gets_no_free_pass(self):
        # Everyone measurable is overpaid by 30%. Scoring the unpriced player as if the
        # market were exactly fair used to float him above better, priced players.
        pool, levels, values = self._pool(overpaid=True)
        picks = self._recommend(pool, levels, values)
        names = [pick.name for pick in picks]
        assert names.index("RB2") < names.index("RB3")


class TestPenaltiesSink:
    def test_a_filled_position_sinks_an_overpriced_player(self):
        """Regression: with a negative score, the depth multiply used to *promote*.

        Two identical twins, both priced $60 by the room against $20 of worth. Holding
        four running backs must push the RB twin below the WR twin.
        """
        levels = ReplacementLevels(points={}, starters_drafted={"RB": 30, "WR": 36})
        rb = replace(player("RB twin", "RB", 150, adp=30), market_cost=60.0)
        wr = replace(player("WR twin", "WR", 150, adp=30), market_cost=60.0)
        values = dollar_values({rb.player_key: 20.0, wr.player_key: 20.0})

        picks = recommend_auction(
            [rb, wr],
            levels,
            values,
            engine_settings(),
            {"RB": 4},
            money_remaining=40,
            slots_remaining=2,
            my_max_bid=100,
            limit=2,
        )
        assert picks[0].position == "WR"

    def test_injury_sinks_not_floats_an_overpriced_player(self):
        levels = ReplacementLevels(points={}, starters_drafted={"RB": 30})
        healthy = replace(player("Healthy", "RB", 150, adp=30), market_cost=60.0)
        hurt = replace(player("Hurt", "RB", 150, adp=30, status="O"), market_cost=60.0)
        values = dollar_values({healthy.player_key: 20.0, hurt.player_key: 20.0})

        picks = recommend_auction(
            [healthy, hurt],
            levels,
            values,
            engine_settings(),
            {},
            money_remaining=40,
            slots_remaining=2,
            my_max_bid=100,
            limit=2,
        )
        assert picks[0].name == "Healthy"


class TestSmartCap:
    def _scenario(self, **overrides):
        # My roster lacks only a quarterback (plus bench). The remaining QBs go for
        # real money, so bidding on a bench back must leave more than $1 for the slot.
        levels = ReplacementLevels(
            points={}, starters_drafted={"QB": 12, "RB": 30, "WR": 36, "TE": 12}
        )
        qbs = [
            replace(player(f"QB{i}", "QB", 300 - i * 10, adp=i + 1), market_cost=40.0 - i * 5.0)
            for i in range(3)
        ]
        bench_rb = replace(player("Bench RB", "RB", 120, adp=50), market_cost=10.0)
        pool = qbs + [bench_rb]
        values = dollar_values(
            {**{qb.player_key: 35.0 for qb in qbs}, bench_rb.player_key: 20.0}
        )
        kwargs = dict(
            money_remaining=125,
            slots_remaining=4,
            my_max_bid=55,
            my_budget_remaining=60,
            league_position_counts={"QB": 0},
            limit=10,
        )
        kwargs.update(overrides)
        picks = recommend_auction(
            pool,
            levels,
            values,
            engine_settings(),
            {"RB": 3, "WR": 3, "TE": 1},
            **kwargs,
        )
        return picks

    def test_reserves_real_money_for_open_starters(self):
        picks = self._scenario()
        bench_rb = next(pick for pick in picks if pick.name == "Bench RB")
        # $60 in hand, but the open QB slot will cost about $30 (the cheapest QB the
        # league's demand leaves reachable) and four bench spots $1 each.
        assert bench_rb.smart_cap == 26
        assert bench_rb.smart_cap < bench_rb.max_bid
        assert bench_rb.bid_to <= bench_rb.smart_cap

    def test_the_player_filling_the_slot_releases_its_reservation(self):
        picks = self._scenario()
        qb = next(pick for pick in picks if pick.position == "QB")
        # Buying a QB is what the reservation was *for*; only bench dollars stay parked.
        assert qb.smart_cap == 55

    def test_without_budget_info_the_hard_ceiling_stands(self):
        picks = self._scenario(my_budget_remaining=None)
        assert all(pick.smart_cap == pick.max_bid for pick in picks)


class TestRoomPremiumIntegration:
    def test_room_overpaying_raises_expected_prices(self):
        levels = ReplacementLevels(points={}, starters_drafted={"RB": 30})
        rb = replace(player("Steady RB", "RB", 150, adp=30), market_cost=20.0)
        values = dollar_values({rb.player_key: 20.0})
        kwargs = dict(money_remaining=20, slots_remaining=1, my_max_bid=100, limit=1)

        cold = recommend_auction([rb], levels, values, engine_settings(), {}, **kwargs)
        hot = recommend_auction(
            [rb],
            levels,
            values,
            engine_settings(),
            {},
            sales=[Sale("RB", price=30.0, expected=20.0)] * 10,
            **kwargs,
        )
        assert hot[0].market > cold[0].market
        assert not hot[0].market_estimated


class TestAuctionAPI:
    def test_state_reports_auction_mode_and_budgets(self, client):
        payload = client.get("/api/state").json()
        assert payload["draft_type"] == "auction"
        auction = payload["auction"]
        assert auction["budget"] == BUDGET
        assert auction["remaining"] == BUDGET
        assert auction["max_bid"] == BUDGET - 14
        assert len(auction["teams"]) == NUM_TEAMS

    def test_recommend_returns_dollar_fields_not_vona(self, client):
        payload = client.get("/api/recommend?limit=3").json()
        assert payload["draft_type"] == "auction"
        assert "inflation" in payload
        first = payload["recommendations"][0]
        assert {"value", "par", "market", "surplus", "bid_to", "affordable"} <= set(first)
        # VONA and survival are meaningless in an auction and must not be served.
        assert "vona" not in first
        assert "survival" not in first

    def test_auction_pick_without_a_price_is_rejected(self, client):
        target = client.get("/api/recommend?limit=1").json()["recommendations"][0]
        response = client.post(
            "/api/pick", json={"player_key": target["player_key"], "team_key": "461.l.1.t.1"}
        )
        # Recording a sale with no price would silently corrupt every budget downstream.
        assert response.status_code == 400

    def test_auction_pick_without_a_buyer_is_rejected(self, client):
        target = client.get("/api/recommend?limit=1").json()["recommendations"][0]
        response = client.post("/api/pick", json={"player_key": target["player_key"], "cost": 30})
        # With no buyer the money leaves nobody's budget, so the league looks richer than
        # it is and every remaining player is over-valued.
        assert response.status_code == 400

    def test_a_sale_with_no_buyer_would_distort_the_money_left(self, assistant):
        """The reason the endpoint insists on a buyer, stated as a property.

        Guarding this at the API is only worth it because the underlying arithmetic
        really does go wrong -- money attributed to no team never leaves the pool.
        """
        state = assistant.state
        before = state.league_money_remaining()

        state.record_manual("461.p.RB0", pick=1, cost=80, team_key="")
        assert state.league_money_remaining() == before  # $80 vanished into thin air

        state.record_manual("461.p.RB1", pick=2, cost=80, team_key="461.l.1.t.1")
        assert state.league_money_remaining() == before - 80

    def test_auction_pick_with_a_price_updates_budgets(self, client):
        state = client.get("/api/state").json()
        my_team = next(t for t in state["auction"]["teams"] if t["is_mine"])
        target = client.get("/api/recommend?limit=1").json()["recommendations"][0]

        client.post(
            "/api/pick",
            json={
                "player_key": target["player_key"],
                "cost": 45,
                "team_key": my_team["team_key"],
            },
        )
        after = client.get("/api/state").json()["auction"]
        assert after["spent"] == 45
        assert after["remaining"] == BUDGET - 45
        assert after["slots_remaining"] == 14

    def test_a_rival_purchase_moves_their_budget_not_mine(self, client):
        state = client.get("/api/state").json()
        rival = next(t for t in state["auction"]["teams"] if not t["is_mine"])
        target = client.get("/api/recommend?limit=1").json()["recommendations"][0]

        client.post(
            "/api/pick",
            json={
                "player_key": target["player_key"],
                "cost": 70,
                "team_key": rival["team_key"],
            },
        )
        after = client.get("/api/state").json()["auction"]
        assert after["remaining"] == BUDGET  # mine untouched
        updated = next(t for t in after["teams"] if t["team_key"] == rival["team_key"])
        assert updated["remaining"] == BUDGET - 70


class TestFullAuctionSimulation:
    def test_a_whole_auction_runs_without_breaking(self, assistant):
        """Sell every roster spot in the league, cycling teams and prices.

        The late rounds are where this could fall over: budgets bottom out, max bids hit
        zero, and the inflation denominator gets small.
        """
        state = assistant.state
        settings = assistant.league.settings
        total = NUM_TEAMS * settings.roster_size
        sold: set[str] = set()

        for index in range(total):
            team_key = f"461.l.1.t.{index % NUM_TEAMS + 1}"
            picks = assistant.auction_recommendations(limit=1)
            if not picks:
                break
            chosen = picks[0]
            assert chosen.valuation.player_key not in sold
            sold.add(chosen.valuation.player_key)

            # Pay what the team can afford, never less than the minimum bid.
            price = max(MIN_BID, min(chosen.bid_to, state.max_bid(team_key)))
            state.record_manual(
                chosen.valuation.player_key, pick=index + 1, cost=price, team_key=team_key
            )

        assert len(sold) == total
        # No team may overspend its budget.
        for team in state.teams:
            assert state.spent(team.team_key) <= BUDGET
            assert state.slots_remaining(team.team_key) == 0

    def test_positions_are_all_still_reachable(self, assistant):
        # A pure surplus-chasing engine can starve a position entirely; kickers and
        # defenses must remain valued even though they have no stat projections.
        keys = {pick.position for pick in assistant.auction_recommendations(limit=60)}
        assert {"RB", "WR"} <= keys
        assert set(POSITION_POOL) >= keys
