"""Tests for reading the draft room in bulk.

The failure this guards against is not a crash. It is a player landing on the board twice
and his buyer being charged twice, which looks like a perfectly ordinary board and makes
every price the app quotes wrong for the rest of the draft. So most of these assert on
money, not on structure.
"""

from __future__ import annotations

import threading

import pytest

from ff_helper.assistant import Assistant
from ff_helper.draft.bridge import BridgeResolver, RawSale, parse_paste
from ff_helper.draft.state import BridgeSale, DraftState
from tests.test_auction import auction_league, auction_teams
from tests.test_web import MY_SLOT, build_snapshot

MY_KEY = f"461.l.1.t.{MY_SLOT}"
RIVAL = "461.l.1.t.2"


@pytest.fixture
def state() -> DraftState:
    league = auction_league()
    return DraftState(league=league, teams=auction_teams())


@pytest.fixture
def assistant(state) -> Assistant:
    return Assistant.build(state.league, state, build_snapshot(), lock=threading.Lock())


def sale(player_key: str, team_key: str = RIVAL, cost: int = 10) -> BridgeSale:
    return BridgeSale(player_key=player_key, team_key=team_key, cost=cost)


class TestSnapshotSemantics:
    def test_first_reading_puts_everyone_on_the_board(self, state):
        diff = state.apply_bridge(
            [sale("461.p.RB0", cost=50), sale("461.p.WR0", cost=30)], timestamp=1.0
        )
        assert len(diff.applied) == 2
        assert state.spent(RIVAL) == 80

    def test_repeating_a_reading_changes_nothing(self, state):
        sales = [sale("461.p.RB0", cost=50), sale("461.p.WR0", cost=30)]
        state.apply_bridge(sales, timestamp=1.0)
        diff = state.apply_bridge(sales, timestamp=2.0)
        assert diff.applied == [] and diff.corrected == []
        assert diff.unchanged == 2
        assert state.spent(RIVAL) == 80

    def test_reordering_does_not_duplicate_or_recharge(self, state):
        """The whole reason identity is the player and not the pick number."""
        sales = [sale("461.p.RB0", cost=50), sale("461.p.WR0", cost=30)]
        state.apply_bridge(sales, timestamp=1.0)
        before = dict(state.bridge_order)

        state.apply_bridge(list(reversed(sales)), timestamp=2.0)
        assert state.bridge_order == before
        assert state.spent(RIVAL) == 80
        assert len(state.board) == 2

    def test_the_same_player_twice_in_one_reading_is_charged_once(self, state):
        state.apply_bridge(
            [sale("461.p.RB0", cost=50), sale("461.p.RB0", cost=50)], timestamp=1.0
        )
        assert state.spent(RIVAL) == 50
        assert len(state.board) == 1

    def test_a_corrected_price_moves_the_budget_by_the_difference(self, state):
        state.apply_bridge([sale("461.p.RB0", cost=50)], timestamp=1.0)
        diff = state.apply_bridge([sale("461.p.RB0", cost=42)], timestamp=2.0)
        assert len(diff.corrected) == 1
        assert state.spent(RIVAL) == 42

    def test_a_corrected_buyer_moves_the_money_between_teams(self, state):
        state.apply_bridge([sale("461.p.RB0", team_key=RIVAL, cost=50)], timestamp=1.0)
        state.apply_bridge([sale("461.p.RB0", team_key=MY_KEY, cost=50)], timestamp=2.0)
        assert state.spent(RIVAL) == 0
        assert state.spent(MY_KEY) == 50

    def test_a_growing_board_keeps_earlier_numbers(self, state):
        state.apply_bridge([sale("461.p.RB0")], timestamp=1.0)
        first = state.bridge_order["461.p.RB0"]
        state.apply_bridge([sale("461.p.RB0"), sale("461.p.WR0")], timestamp=2.0)
        assert state.bridge_order["461.p.RB0"] == first


class TestShrinkingIsTreatedAsAFailedRead:
    """Sales do not un-happen, so a board that lost rows is almost always a bad read."""

    def _seed(self, state, count=6):
        sales = [sale(f"461.p.RB{i}", cost=10) for i in range(count)]
        state.apply_bridge(sales, timestamp=1.0)
        return sales

    def test_a_large_drop_is_rejected_wholesale(self, state):
        self._seed(state)
        diff = state.apply_bridge([sale("461.p.RB0", cost=10)], timestamp=2.0)
        assert diff.rejected
        assert state.spent(RIVAL) == 60, "the board must be untouched"

    def test_a_small_drop_is_applied(self, state):
        sales = self._seed(state)
        diff = state.apply_bridge(sales[:-1], timestamp=2.0)
        assert diff.rejected is None
        assert len(diff.removed) == 1
        assert state.spent(RIVAL) == 50

    def test_a_rejected_read_leaves_the_ordering_intact(self, state):
        self._seed(state)
        before = dict(state.bridge_order)
        state.apply_bridge([], timestamp=2.0)
        assert state.bridge_order == before


class TestBridgeAndHumanTogether:
    def test_a_hand_entered_sale_is_absorbed_not_duplicated(self, state):
        entry = state.record_manual("461.p.RB0", cost=50, team_key=RIVAL)
        state.apply_bridge([sale("461.p.RB0", cost=50)], timestamp=1.0)
        assert state.spent(RIVAL) == 50, "charged once, not twice"
        assert state.bridge_order["461.p.RB0"] == entry.pick
        assert len(state.board) == 1

    def test_the_bridge_corrects_a_mistyped_price(self, state):
        state.record_manual("461.p.RB0", cost=5, team_key=RIVAL)
        state.apply_bridge([sale("461.p.RB0", cost=50)], timestamp=1.0)
        assert state.spent(RIVAL) == 50

    def test_a_hand_entered_pick_does_not_collide_with_a_bridge_slot(self, state):
        """record_manual used to default to current_pick, which the bridge already held."""
        state.apply_bridge([sale("461.p.RB0", cost=50)], timestamp=1.0)
        entry = state.record_manual("461.p.WR0", cost=20, team_key=MY_KEY)
        assert entry.pick not in state.bridge
        assert state.spent(MY_KEY) == 20, "the manual entry must not be shadowed"
        assert len(state.board) == 2

    def test_players_the_bridge_never_saw_survive(self, state):
        state.record_manual("461.p.WR0", cost=20, team_key=MY_KEY)
        state.apply_bridge([sale("461.p.RB0", cost=50)], timestamp=1.0)
        assert state.spent(MY_KEY) == 20


class TestResolution:
    def _resolver(self, assistant):
        return BridgeResolver(assistant.registry, assistant.state.teams)

    def test_an_unresolvable_buyer_is_never_written(self, assistant):
        """Money charged to no team never leaves the room and overstates every price."""
        report = self._resolver(assistant).resolve_all(
            [RawSale(name="RB Player0", cost=50, buyer="Nobody's Team")], is_auction=True
        )
        assert report.resolved == []
        assert len(report.unknown_buyers) == 1
        assert not report.ok

    def test_a_missing_price_is_refused(self, assistant):
        report = self._resolver(assistant).resolve_all(
            [RawSale(name="RB Player0", buyer="Team 2")], is_auction=True
        )
        assert report.resolved == []
        assert len(report.missing_price) == 1

    def test_an_unmatched_player_is_reported(self, assistant):
        report = self._resolver(assistant).resolve_all(
            [RawSale(name="Nobody At All", cost=5, buyer="Team 2")], is_auction=True
        )
        assert len(report.unknown_players) == 1

    def test_buyer_matching_survives_decoration(self, assistant):
        """Draft rooms add trophies and truncate with an ellipsis; the settings page does not."""
        resolver = self._resolver(assistant)
        target = assistant.state.teams[1]
        for spelling in (target.name, target.name.upper(), f"  {target.name} 🏆 "):
            assert resolver.resolve_team(spelling) == target.team_key

    def test_an_alias_resolves_a_room_specific_spelling(self, assistant):
        target = assistant.state.teams[1]
        resolver = BridgeResolver(
            assistant.registry,
            assistant.state.teams,
            team_aliases={"Some Room Name": target.name},
        )
        assert resolver.resolve_team("Some Room Name") == target.team_key

    def test_negative_lookups_are_cached(self, assistant):
        """An unmatchable name otherwise costs a full fuzzy scan on every reading."""
        resolver = self._resolver(assistant)
        calls = []
        original = resolver.registry.find_fuzzy
        resolver.registry.find_fuzzy = lambda row: (calls.append(row), original(row))[1]
        for _ in range(5):
            resolver.resolve_player(RawSale(name="Definitely Not A Player"))
        assert len(calls) == 1


class TestPasteParsing:
    def test_tab_delimited(self):
        sales = parse_paste("Ja'Marr Chase\t$55\tTeam Ryan")
        assert sales[0].name == "Ja'Marr Chase"
        assert sales[0].cost == 55
        assert sales[0].buyer == "Team Ryan"

    def test_comma_delimited_with_commas_inside_a_team_name(self):
        sales = parse_paste('Jahmyr Gibbs,62,"Butts, Butts and Butts"')
        assert sales[0].cost == 62
        assert sales[0].buyer == "Butts, Butts and Butts"

    def test_plain_line(self):
        sales = parse_paste("Puka Nacua $3 SumTingWong")
        assert sales[0].name == "Puka Nacua"
        assert sales[0].cost == 3

    def test_position_and_nfl_team_are_extracted(self):
        """A defense is matched by team, so losing the abbreviation loses the match."""
        sales = parse_paste("Seattle Defense\tDEF\tSEA\t$4\tSumTingWong")
        assert sales[0].position == "DEF"
        assert sales[0].team_abbr == "SEA"
        assert sales[0].buyer == "SumTingWong"

    def test_blank_lines_and_noise_are_skipped(self):
        sales = parse_paste("\n\nJa'Marr Chase\t$55\tTeam Ryan\n\n")
        assert len(sales) == 1

    def test_nothing_parseable(self):
        assert parse_paste("just some prose with no prices") == []


class TestWholeAuctionThroughTheBridge:
    def test_a_full_auction_replayed_in_growing_readings(self, state):
        """The test that would actually catch corruption.

        Replays sales in growing, shuffled batches with duplicates thrown in -- the shape
        of a real reader re-sending the board -- and asserts every team's spend is exactly
        right at the end.
        """
        players = [f"461.p.RB{i}" for i in range(20)]
        teams = [MY_KEY, RIVAL, "461.l.1.t.3"]
        truth = {
            key: (teams[index % len(teams)], (index % 7) + 1)
            for index, key in enumerate(players)
        }

        seen: list[BridgeSale] = []
        for index, key in enumerate(players):
            team, cost = truth[key]
            seen.append(BridgeSale(player_key=key, team_key=team, cost=cost))
            # Re-send everything each time, shuffled, with the newest row duplicated.
            payload = list(reversed(seen)) + [seen[-1]]
            state.apply_bridge(payload, timestamp=float(index))

        for team in teams:
            expected = sum(cost for key, (owner, cost) in truth.items() if owner == team)
            assert state.spent(team) == expected, team

        assert len(state.board) == len(players)
        assert len({pick.player_key for pick in state.board.values()}) == len(players)
