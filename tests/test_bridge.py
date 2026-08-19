"""Tests for reading the draft room in bulk.

The failure this guards against is not a crash. It is a player landing on the board twice
and his buyer being charged twice, which looks like a perfectly ordinary board and makes
every price the app quotes wrong for the rest of the draft. So most of these assert on
money, not on structure.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ff_helper.assistant import Assistant
from ff_helper.draft.bridge import BridgeResolver, RawSale, parse_paste
from ff_helper.draft.state import BridgeSale, DraftState
from tests.test_auction import auction_league, auction_teams
from tests.test_web import MY_SLOT, build_snapshot

FIXTURES = Path(__file__).parent / "fixtures"

MY_KEY = f"461.l.1.t.{MY_SLOT}"
RIVAL = "461.l.1.t.2"


@pytest.fixture
def state() -> DraftState:
    league = auction_league()
    return DraftState(league=league, teams=auction_teams())


@pytest.fixture
def assistant(state) -> Assistant:
    return Assistant.build(state.league, state, build_snapshot(), lock=threading.Lock())


def sale(
    player_key: str, team_key: str = RIVAL, cost: int = 10, pick: int | None = None
) -> BridgeSale:
    return BridgeSale(player_key=player_key, team_key=team_key, cost=cost, pick=pick)


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

    def test_an_unknown_buyer_gets_a_free_slot_rather_than_blocking(self, assistant):
        """A renamed or late-joining team must not stop its money leaving the room."""
        report = self._resolver(assistant).resolve_all(
            [RawSale(name="RB Player0", cost=50, buyer="Nobody's Team")], is_auction=True
        )
        assert len(report.resolved) == 1
        assert report.unknown_buyers == []
        assert len(report.assigned_buyers) == 1

    def test_an_assigned_buyer_keeps_the_same_slot(self, assistant):
        resolver = self._resolver(assistant)
        first, _ = resolver.resolve_team("Nobody's Team")
        second, how = resolver.resolve_team("Nobody's Team")
        assert first == second
        assert how == "assigned"

    def test_two_unknown_buyers_get_different_slots(self, assistant):
        resolver = self._resolver(assistant)
        one, _ = resolver.resolve_team("Stranger One")
        two, _ = resolver.resolve_team("Stranger Two")
        assert one != two

    def test_your_own_team_is_never_handed_out(self, assistant):
        """Your budget and max bid are the numbers the app exists to produce."""
        resolver = self._resolver(assistant)
        mine = next(t.team_key for t in assistant.state.teams if t.is_mine)
        handed = {resolver.resolve_team(f"Stranger {i}")[0] for i in range(30)}
        assert mine not in handed

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
            assert resolver.resolve_team(spelling) == (target.team_key, "exact")

    def test_an_alias_resolves_a_room_specific_spelling(self, assistant):
        target = assistant.state.teams[1]
        resolver = BridgeResolver(
            assistant.registry,
            assistant.state.teams,
            team_aliases={"Some Room Name": target.name},
        )
        assert resolver.resolve_team("Some Room Name") == (target.team_key, "exact")

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


class TestRealYahooDraftRoom:
    """Against text actually copied out of a Yahoo auction draft room.

    Everything here was guesswork until this fixture existed. Yahoo writes one record per
    sale across several lines, newest first, with round headings in between, the name
    repeated, an injury flag only sometimes, and the reader's own team called "Your Team".
    """

    def _text(self) -> str:
        return (FIXTURES / "yahoo_draft_results.txt").read_text(encoding="utf-8")

    def test_every_sale_is_read(self):
        sales = parse_paste(self._text())
        assert len(sales) == 16

    def test_the_fields_land_in_the_right_places(self):
        sales = {sale.line: sale for sale in parse_paste(self._text())}
        gibbs = sales[1]
        assert gibbs.name == "J. Gibbs"
        assert gibbs.position == "RB"
        assert gibbs.team_abbr == "DET"
        assert gibbs.cost == 74
        assert gibbs.buyer == "Team 11"

    def test_an_injury_flag_does_not_shift_the_columns(self):
        """McCaffrey carries a "Q" line that London does not."""
        sales = {sale.line: sale for sale in parse_paste(self._text())}
        cmc = sales[6]
        assert cmc.name == "C. McCaffrey"
        assert cmc.position == "RB"
        assert cmc.team_abbr == "SF"
        assert cmc.cost == 63

    def test_round_headings_and_the_table_header_are_not_sales(self):
        names = {sale.name for sale in parse_paste(self._text())}
        assert not any(name.lower().startswith("round") for name in names)
        assert "Player" not in names

    def test_your_team_is_the_reader(self):
        """Yahoo never prints your own team's name, so without this your budget is wrong."""
        sales = [sale for sale in parse_paste(self._text()) if sale.buyer == "Your Team"]
        assert len(sales) == 2  # J. Taylor and B. Robinson


class TestAbbreviatedNames:
    """Yahoo writes "B. Robinson", and an initial is not an identity."""

    def _resolver(self, assistant, values=None):
        return BridgeResolver(assistant.registry, assistant.state.teams, values=values)

    def test_an_unambiguous_initial_resolves(self, assistant):
        key, how = self._resolver(assistant).resolve_player(
            RawSale(name="R. Player0", position="RB", team_abbr="FA")
        )
        assert key is not None and how == "exact"

    def test_an_ambiguous_initial_is_settled_by_price(self, assistant):
        """Two players fit the name; what it sold for says which."""
        registry = assistant.registry
        cheap, dear = registry.players[0], registry.players[1]
        values = {cheap.player_key: 3.0, dear.player_key: 70.0}
        resolver = self._resolver(assistant, values=values)
        resolver.registry = _TwoCandidates(registry, [cheap, dear])

        key, how = resolver.resolve_player(RawSale(name="X. Ambiguous", cost=68))
        assert key == dear.player_key
        assert how == "priced"

        key, how = resolver.resolve_player(RawSale(name="X. Ambiguous", cost=2))
        assert key == cheap.player_key

    def test_a_priced_guess_is_reported_not_hidden(self, assistant):
        registry = assistant.registry
        cheap, dear = registry.players[0], registry.players[1]
        resolver = self._resolver(
            assistant, values={cheap.player_key: 3.0, dear.player_key: 70.0}
        )
        resolver.registry = _TwoCandidates(registry, [cheap, dear])
        report = resolver.resolve_all(
            [RawSale(name="X. Ambiguous", cost=68, buyer=RIVAL)], is_auction=True
        )
        assert len(report.assumed) == 1


class _TwoCandidates:
    """A registry whose exact lookup abstains, as it does for a genuinely ambiguous name."""

    def __init__(self, real, options):
        self._real = real
        self._options = options
        self.by_key = real.by_key
        self.players = real.players

    def find(self, row):
        return None

    def candidates(self, row):
        return list(self._options)

    def find_fuzzy(self, row):
        return None, 0.0


class TestWholePageNoise:
    """The reader sends innerText, not a tidy copy, so records must survive surroundings.

    This is what makes the userscript viable without understanding Yahoo's DOM: grab a
    bigger blob than necessary and let the parser discard what is not a sale.
    """

    def _blob(self) -> str:
        fixture = (FIXTURES / "yahoo_draft_results.txt").read_text(encoding="utf-8")
        before = "Yahoo Fantasy\nDraft Room\n0:47\nNominate\n$1\nAuction Budget\n200\n"
        after = "\nChat\nBlake: nice pick\n23\nQuinn: ugh\nQueue\nWatch List\n"
        return before + fixture + after

    def test_page_furniture_does_not_become_sales(self):
        sales = parse_paste(self._blob())
        assert len(sales) == 16

    def test_the_final_sale_keeps_its_price(self):
        """It used to absorb whatever followed it and lose the price line."""
        sales = {sale.line: sale for sale in parse_paste(self._blob())}
        assert sales[1].name == "J. Gibbs"
        assert sales[1].cost == 74
        assert sales[1].buyer == "Team 11"

    def test_a_stray_number_in_chat_does_not_start_a_sale(self):
        names = {sale.name for sale in parse_paste(self._blob())}
        assert not any(":" in name for name in names)

    def test_a_number_with_no_price_after_it_is_ignored(self):
        assert parse_paste("7\nSome Heading\nMore Text\nAnd More\n") == []


class TestBridgeTokenGate:
    """Opening the board to the draft room must not open it to every page you have open."""

    def _client(self, assistant, token=""):
        from fastapi.testclient import TestClient

        from ff_helper.web.app import create_app

        return TestClient(create_app(assistant, None, bridge_token=token))

    def _body(self):
        return {"text": (FIXTURES / "yahoo_draft_results.txt").read_text(), "strict": False}

    YAHOO = "https://football.fantasysports.yahoo.com"

    def test_the_apps_own_page_needs_no_token(self, assistant):
        client = self._client(assistant, token="secret")
        res = client.post(
            "/api/board/paste", json=self._body(), headers={"Origin": "http://127.0.0.1:8777"}
        )
        assert res.status_code != 401

    def test_no_origin_at_all_needs_no_token(self, assistant):
        """The UI's own fetch, and anything driving the API locally."""
        client = self._client(assistant, token="secret")
        assert client.post("/api/board/paste", json=self._body()).status_code != 401

    def test_the_draft_room_needs_the_token(self, assistant):
        client = self._client(assistant, token="secret")
        res = client.post(
            "/api/board/paste",
            json=self._body(),
            headers={"Origin": self.YAHOO, "X-Bridge-Token": "secret"},
        )
        assert res.status_code != 401

    def test_a_wrong_token_is_refused(self, assistant):
        client = self._client(assistant, token="secret")
        res = client.post(
            "/api/board/paste",
            json=self._body(),
            headers={"Origin": self.YAHOO, "X-Bridge-Token": "wrong"},
        )
        assert res.status_code == 401

    def test_every_route_is_gated_not_just_the_paste_one(self, assistant):
        """CORS grants the Yahoo origin the whole app, so the token must cover it."""
        client = self._client(assistant, token="secret")
        for method, path in (
            ("post", "/api/undo"),
            ("get", "/api/state"),
            ("get", "/api/recommend"),
        ):
            res = getattr(client, method)(path, headers={"Origin": self.YAHOO})
            assert res.status_code == 401, path

    def test_a_hostname_that_merely_starts_with_localhost_is_external(self, assistant):
        client = self._client(assistant, token="secret")
        res = client.get("/api/state", headers={"Origin": "http://localhost.evil.example"})
        assert res.status_code == 401

    def test_the_apps_own_origin_is_not_gated(self, assistant):
        client = self._client(assistant, token="secret")
        assert client.get(
            "/api/state", headers={"Origin": "http://127.0.0.1:8777"}
        ).status_code == 200

    def test_without_bridge_mode_no_external_origin_may_post(self, assistant):
        """Default is closed: the board is reachable from its own page and nowhere else."""
        client = self._client(assistant, token="")
        res = client.post(
            "/api/board/paste", json=self._body(), headers={"Origin": self.YAHOO}
        )
        assert res.status_code == 401


class TestNominationLookup:
    """The question an auction actually asks: someone said a name, what is he worth?"""

    def _client(self, assistant):
        from fastapi.testclient import TestClient

        from ff_helper.web.app import create_app

        return TestClient(create_app(assistant, None))

    def test_a_partial_name_returns_a_value(self, assistant):
        res = self._client(assistant).get("/api/lookup?q=RB Player0").json()
        assert res["draft_type"] == "auction"
        assert res["results"], "expected a match"
        assert res["results"][0]["value"] > 0
        assert "max_bid" in res

    def test_it_records_nothing(self, assistant):
        """You look up players you have no intention of bidding on."""
        client = self._client(assistant)
        before = client.get("/api/state").json()["auction"]["league_money_remaining"]
        client.get("/api/lookup?q=RB Player0")
        after = client.get("/api/state").json()["auction"]["league_money_remaining"]
        assert before == after

    def test_drafted_players_are_not_offered(self, assistant):
        client = self._client(assistant)
        name = assistant.registry.players[0].full_name
        assistant.state.apply_bridge(
            [BridgeSale(player_key=assistant.registry.players[0].player_key,
                        team_key=RIVAL, cost=5)],
            timestamp=1.0,
        )
        hits = client.get(f"/api/lookup?q={name}").json()["results"]
        assert all(hit["name"] != name for hit in hits)

    def test_the_ceiling_never_exceeds_what_you_can_pay(self, assistant):
        """Bidding past it means you cannot fill a legal roster afterwards."""
        client = self._client(assistant)
        res = client.get("/api/lookup?q=Player&limit=8").json()
        for hit in res["results"]:
            assert hit["bid_to"] <= res["max_bid"]

    def test_an_empty_query_is_not_an_error(self, assistant):
        res = self._client(assistant).get("/api/lookup?q=").json()
        assert res["results"] == []

    def test_the_value_matches_the_board(self, assistant):
        """Two prices for one player five seconds apart is worse than no price."""
        client = self._client(assistant)
        board = client.get("/api/recommend?limit=1").json()["recommendations"][0]
        looked_up = client.get(f"/api/lookup?q={board['name']}").json()["results"][0]
        assert looked_up["value"] == board["value"]
        assert looked_up["bid_to"] == board["bid_to"]


class TestWritersDoNotDoubleCharge:
    """Every confirmed path to one player on the board twice."""

    def test_the_yahoo_feed_and_the_bridge_agree_about_one_sale(self, state):
        """--bridge and the API poller are independent flags, so both can be live."""
        from ff_helper.yahoo.models import DraftPick

        state.apply_bridge([sale("461.p.RB0", cost=50)], timestamp=1.0)
        state.apply_sync(
            [DraftPick(pick=42, round=1, team_key=RIVAL, player_key="461.p.RB0", cost=50)],
            timestamp=2.0,
        )
        assert state.spent(RIVAL) == 50
        assert [p.player_key for p in state.board.values()].count("461.p.RB0") == 1

    def test_a_yahoo_pick_does_not_shadow_a_different_bridge_sale(self, state):
        from ff_helper.yahoo.models import DraftPick

        state.apply_bridge(
            [sale("461.p.RB0", cost=50, pick=1), sale("461.p.WR0", cost=30, pick=2)],
            timestamp=1.0,
        )
        state.apply_sync(
            [DraftPick(pick=1, round=1, team_key=RIVAL, player_key="461.p.RB0", cost=50)],
            timestamp=2.0,
        )
        assert state.spent(RIVAL) == 80, "a sold player must not fall off the board"
        assert "461.p.WR0" in state.drafted_player_keys

    def test_pick_zero_is_a_real_pick_number(self, state):
        """`or` treated it as absent and allocated a second slot for the same player."""
        state.record_manual("461.p.RB0", pick=0, cost=30, team_key=RIVAL)
        state.apply_bridge([sale("461.p.RB0", cost=30)], timestamp=1.0)
        assert state.spent(RIVAL) == 30

    def test_a_hand_entry_for_another_player_is_not_deleted(self, state):
        """apply_bridge popped the manual entry by slot, not by player."""
        state.record_manual("461.p.TE0", pick=1, cost=30, team_key=RIVAL)
        state.apply_bridge([sale("461.p.RB0", cost=50, pick=1)], timestamp=1.0)
        assert "461.p.TE0" in state.drafted_player_keys


class TestRemovalsAreNotRefunds:
    def test_an_empty_reading_never_empties_the_board(self, state):
        """With two sales, both sat inside shrink_tolerance and were refunded."""
        state.apply_bridge(
            [sale("461.p.RB0", cost=90, pick=1), sale("461.p.WR0", cost=80, pick=2)],
            timestamp=1.0,
        )
        diff = state.apply_bridge([], timestamp=2.0)
        assert diff.rejected
        assert state.spent(RIVAL) == 170

    def test_removals_are_withheld_when_the_reading_was_incomplete(self, state):
        """A row that failed to resolve looks exactly like a sale that vanished."""
        state.apply_bridge([sale("461.p.RB0", cost=50, pick=1)], timestamp=1.0)
        diff = state.apply_bridge([], timestamp=2.0, allow_removals=False)
        assert diff.removed == []
        assert state.spent(RIVAL) == 50


class TestRemovalAndUndo:
    def test_a_bridge_pick_can_be_taken_off_the_board(self, state):
        state.apply_bridge([sale("461.p.RB0", cost=50, pick=1)], timestamp=1.0)
        assert state.remove_player("461.p.RB0") is not None
        assert state.spent(RIVAL) == 0
        assert "461.p.RB0" not in state.drafted_player_keys

    def test_undo_removes_what_was_entered_last_not_the_highest_number(self, state):
        first = state.record_manual("461.p.RB0", pick=6, cost=10, team_key=RIVAL)
        last = state.record_manual("461.p.WR0", pick=3, cost=10, team_key=RIVAL)
        assert state.undo_last_manual().pick == last.pick
        assert first.pick in state.manual


class TestGuessesKeepBeingReported:
    def test_a_priced_guess_is_reported_on_every_reading(self, assistant):
        """The cache returned "cached", so the warning fired once and never again."""
        from ff_helper.draft.bridge import BridgeResolver

        registry = assistant.registry
        cheap, dear = registry.players[0], registry.players[1]
        resolver = BridgeResolver(
            registry,
            assistant.state.teams,
            values={cheap.player_key: 3.0, dear.player_key: 70.0},
        )
        resolver.registry = _TwoCandidates(registry, [cheap, dear])
        rows = [RawSale(name="X. Ambiguous", cost=68, buyer=RIVAL)]
        runs = [len(resolver.resolve_all(rows, is_auction=True).assumed) for _ in range(3)]
        assert runs == [1, 1, 1], "a guess must keep being reported, not just the once"


class TestShortBuyerNames:
    def test_a_three_letter_team_name_is_not_eaten_as_an_nfl_team(self):
        """"TNT" matched the abbreviation shape and left the buyer blank."""
        assert parse_paste("Ja'Marr Chase,55,TNT")[0].buyer == "TNT"

    def test_a_truncated_team_name_still_folds(self):
        """The draft room truncates with an ellipsis; the settings page does not."""
        from ff_helper.draft.bridge import _fold

        assert _fold("Rx…") == _fold("Rx...")
        assert _fold("Team 2…") == _fold("Team 2")


class TestTheLiveDraftRoom:
    """Whole-page text from a running Yahoo auction, not a tidied copy of one panel.

    This is what the reader actually sends, and it differs from the results-panel copy:
    names appear once rather than twice, buyer and price lines carry leading tabs, round
    headings are upper-case, and the page above the table is full of numbers and dollar
    amounts that must not be mistaken for sales.
    """

    def _text(self) -> str:
        return (FIXTURES / "yahoo_draft_room_live.txt").read_text(encoding="utf-8")

    def test_every_sale_and_nothing_else(self):
        sales = parse_paste(self._text())
        assert len(sales) == 31

    def test_the_nomination_counter_is_not_a_sale(self):
        """A bare "8" above the table swallowed the live bid as its price."""
        names = {sale.name for sale in parse_paste(self._text())}
        assert not any("nomination" in name.lower() for name in names)

    def test_the_live_bid_and_budget_panel_are_not_sales(self):
        sales = parse_paste(self._text())
        assert all(sale.buyer and not sale.buyer.startswith("$") for sale in sales)
        assert all(sale.cost and sale.cost > 0 for sale in sales)

    def test_fields_land_correctly_without_the_repeated_name(self):
        sales = {sale.line: sale for sale in parse_paste(self._text())}
        assert sales[31].name == "T. Higgins"
        assert sales[31].position == "WR"
        assert sales[31].team_abbr == "CIN"
        assert sales[31].cost == 32
        assert sales[31].buyer == "Team 8"

    def test_an_injury_flag_before_the_position_does_not_shift_the_fields(self):
        sales = {sale.line: sale for sale in parse_paste(self._text())}
        assert sales[29].name == "B. Hall"
        assert sales[29].position == "RB"
        assert sales[29].cost == 31

    def test_your_own_team_is_recognised_however_yahoo_labels_it(self, assistant):
        from ff_helper.draft.bridge import BridgeResolver

        resolver = BridgeResolver(assistant.registry, assistant.state.teams)
        mine = next(t.team_key for t in assistant.state.teams if t.is_mine)
        for label in ("Your Team", "You", "you"):
            assert resolver.resolve_team(label)[0] == mine, label
