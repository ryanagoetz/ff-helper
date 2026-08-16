"""Tests for keeper handling.

The failure mode here is silent and expensive: a keeper left in the pool gets recommended
to you all draft and you never learn why the advice felt wrong. So these tests focus on
the places a keeper could quietly go missing -- the pool, roster counts, budgets, and the
number of rounds.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from ff_helper.assistant import Assistant
from ff_helper.draft import keepers
from ff_helper.draft.state import DraftState
from ff_helper.rankings.players import PlayerRegistry
from ff_helper.yahoo.models import DraftPick, KeptPlayer
from ff_helper.yahoo.parse import parse_roster
from tests.test_auction import BUDGET, auction_league, auction_teams
from tests.test_web import MY_SLOT, NUM_TEAMS, build_league, build_snapshot

FIXTURES = Path(__file__).parent / "fixtures"
MY_KEY = f"461.l.1.t.{MY_SLOT}"


def snake_state(kept: list[KeptPlayer] | None = None) -> tuple[Assistant, DraftState]:
    league = build_league()
    state = DraftState(league=league, teams=auction_teams())
    if kept:
        state.apply_keepers(kept)
    assistant = Assistant.build(league, state, build_snapshot(), lock=threading.Lock())
    return assistant, state


def auction_state(kept: list[KeptPlayer] | None = None) -> tuple[Assistant, DraftState]:
    league = auction_league()
    state = DraftState(league=league, teams=auction_teams())
    if kept:
        state.apply_keepers(kept)
    assistant = Assistant.build(league, state, build_snapshot(), lock=threading.Lock())
    return assistant, state


class TestRosterParsing:
    def test_parses_rostered_players(self):
        payload = json.loads((FIXTURES / "roster.json").read_text())
        kept = parse_roster(payload, "461.l.123456.t.1")
        assert [k.player_key for k in kept] == ["461.p.100001", "461.p.100002"]
        assert all(k.team_key == "461.l.123456.t.1" for k in kept)

    def test_reads_keeper_salary_when_yahoo_provides_it(self):
        payload = json.loads((FIXTURES / "roster.json").read_text())
        kept = parse_roster(payload, "461.l.123456.t.1")
        assert kept[0].cost == 55
        # Absent keeper metadata must stay None, not become 0 -- a $0 keeper would look
        # free and distort the budget.
        assert kept[1].cost is None

    def test_players_collection_is_found_a_level_deeper_than_elsewhere(self):
        # A roster nests players under a numeric key, unlike every other endpoint. A
        # plain flatten misses it and would silently return no keepers at all.
        payload = json.loads((FIXTURES / "roster.json").read_text())
        assert len(parse_roster(payload, "t.1")) == 2

    def test_empty_roster_is_not_an_error(self):
        payload = {"fantasy_content": {"team": [[{"team_key": "t.1"}], {"roster": {}}]}}
        assert parse_roster(payload, "t.1") == []


class TestCsvLoading:
    @pytest.fixture
    def registry(self):
        return PlayerRegistry(build_snapshot().players)

    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "keepers.csv"
        path.write_text(body)
        return path

    def test_loads_players_teams_and_costs(self, tmp_path, registry):
        path = self._write(
            tmp_path,
            "player,team,cost,round\nRB Player0,Team 5,55,2\nWR Player1,Team 2,,4\n",
        )
        result = keepers.load_csv(path, registry, auction_teams())

        assert len(result.kept) == 2
        first = result.kept[0]
        assert first.team_key == MY_KEY
        assert first.cost == 55
        assert first.round == 2
        assert first.source == "csv"

    def test_accepts_alternate_column_spellings(self, tmp_path, registry):
        path = self._write(tmp_path, "Name,Owner,Salary\nRB Player0,Team 5,$40\n")
        result = keepers.load_csv(path, registry, auction_teams())
        assert result.kept[0].cost == 40

    def test_matches_teams_by_key_as_well_as_name(self, tmp_path, registry):
        path = self._write(tmp_path, f"player,team\nRB Player0,{MY_KEY}\n")
        result = keepers.load_csv(path, registry, auction_teams())
        assert result.kept[0].team_key == MY_KEY

    def test_unknown_player_fails_loudly(self, tmp_path, registry):
        # Skipping the row would leave a keeper in the pool -- exactly the silent failure
        # this whole module exists to prevent.
        path = self._write(tmp_path, "player,team\nNot A Real Person,Team 5\n")
        with pytest.raises(keepers.KeeperError, match="players not found"):
            keepers.load_csv(path, registry, auction_teams())

    def test_unknown_team_fails_loudly(self, tmp_path, registry):
        path = self._write(tmp_path, "player,team\nRB Player0,Nonexistent Team\n")
        with pytest.raises(keepers.KeeperError, match="teams not found"):
            keepers.load_csv(path, registry, auction_teams())

    def test_missing_file_is_a_clear_error(self, tmp_path, registry):
        with pytest.raises(keepers.KeeperError, match="not found"):
            keepers.load_csv(tmp_path / "nope.csv", registry, auction_teams())

    def test_csv_overrides_yahoo(self, tmp_path, registry):
        path = self._write(tmp_path, "player,team\nRB Player0,Team 5\n")
        rostered = [KeptPlayer(player_key="461.p.WR0", team_key="461.l.1.t.2")]
        result = keepers.resolve(rostered, auction_teams(), registry, path)

        assert result.player_keys == {"461.p.RB0"}
        assert any("Ignoring" in note for note in result.notes)

    def test_yahoo_is_used_when_no_csv_is_given(self, registry):
        rostered = [KeptPlayer(player_key="461.p.WR0", team_key="461.l.1.t.2")]
        result = keepers.resolve(rostered, auction_teams(), registry, None)
        assert result.player_keys == {"461.p.WR0"}


class TestYahooKeeperSet:
    def test_uneven_keeper_counts_are_flagged(self):
        rostered = [
            KeptPlayer(player_key="461.p.RB0", team_key="461.l.1.t.1"),
            KeptPlayer(player_key="461.p.RB1", team_key="461.l.1.t.1"),
            KeptPlayer(player_key="461.p.RB2", team_key="461.l.1.t.2"),
        ]
        result = keepers.from_yahoo(rostered, auction_teams())
        # Uneven counts change how many picks each team gets, which the snake maths
        # cannot infer -- it has to be said out loud rather than silently guessed.
        assert any("different numbers" in note for note in result.notes)

    def test_even_counts_are_not_flagged(self):
        rostered = [
            KeptPlayer(player_key="461.p.RB0", team_key="461.l.1.t.1"),
            KeptPlayer(player_key="461.p.RB1", team_key="461.l.1.t.2"),
        ]
        result = keepers.from_yahoo(rostered, auction_teams())
        assert not any("different numbers" in note for note in result.notes)


class TestKeepersOnTheBoard:
    def test_kept_players_leave_the_pool(self):
        assistant, _ = snake_state([KeptPlayer(player_key="461.p.RB0", team_key=MY_KEY)])
        available = {v.player_key for v in assistant.available()}
        assert "461.p.RB0" not in available

    def test_kept_players_are_never_recommended(self):
        # The whole point: a keeper recommended back to you is the silent failure.
        kept = [
            KeptPlayer(player_key=key, team_key=MY_KEY)
            for key in ("461.p.RB0", "461.p.WR0", "461.p.TE0")
        ]
        assistant, _ = snake_state(kept)
        recommended = {pick.valuation.player_key for pick in assistant.recommendations(limit=25)}
        assert not recommended & {k.player_key for k in kept}

    def test_keepers_count_toward_roster_needs(self):
        kept = [
            KeptPlayer(player_key=key, team_key=MY_KEY)
            for key in ("461.p.RB0", "461.p.RB1", "461.p.RB2", "461.p.RB3")
        ]
        assistant, state = snake_state(kept)
        assert state.my_roster_counts(assistant.position_of) == {"RB": 4}
        # Already four deep at running back, so the engine should look elsewhere.
        assert assistant.recommendations(limit=1)[0].position != "RB"

    def test_keepers_shorten_the_draft(self):
        kept = [
            KeptPlayer(player_key=f"461.p.RB{index}", team_key=f"461.l.1.t.{index + 1}")
            for index in range(NUM_TEAMS)
        ]
        _, state = snake_state(kept)
        # 15 roster spots, one keeper each: 14 rounds are actually drafted.
        assert state.roster_size == 15
        assert state.rounds == 14
        assert state.total_picks == NUM_TEAMS * 14

    def test_my_picks_reflect_the_shorter_draft(self):
        kept = [
            KeptPlayer(player_key=f"461.p.RB{index}", team_key=f"461.l.1.t.{index + 1}")
            for index in range(NUM_TEAMS)
        ]
        _, state = snake_state(kept)
        assert len(state.my_picks) == 14

    def test_slots_remaining_does_not_double_count_keepers(self):
        """The trap: keepers are in slots_filled, and rounds was already reduced.

        Measuring open spots against `rounds` instead of `roster_size` would subtract
        them twice and make the max bid far too low.
        """
        kept = [
            KeptPlayer(player_key=f"461.p.RB{index}", team_key=f"461.l.1.t.{index + 1}")
            for index in range(NUM_TEAMS)
        ]
        _, state = auction_state(kept)
        assert state.slots_filled(MY_KEY) == 1
        assert state.slots_remaining(MY_KEY) == 14  # not 13

    def test_uneven_keepers_still_produce_a_usable_board(self):
        kept = [
            KeptPlayer(player_key="461.p.RB0", team_key="461.l.1.t.1"),
            KeptPlayer(player_key="461.p.RB1", team_key="461.l.1.t.1"),
            KeptPlayer(player_key="461.p.RB2", team_key=MY_KEY),
        ]
        assistant, state = snake_state(kept)
        assert state.rounds >= 1
        assert assistant.recommendations(limit=3)


class TestAuctionKeepers:
    def test_keeper_salary_comes_off_the_budget(self):
        kept = [KeptPlayer(player_key="461.p.RB0", team_key=MY_KEY, cost=55)]
        _, state = auction_state(kept)
        assert state.spent(MY_KEY) == 55
        assert state.budget_remaining(MY_KEY) == BUDGET - 55

    def test_max_bid_accounts_for_keepers(self):
        kept = [KeptPlayer(player_key="461.p.RB0", team_key=MY_KEY, cost=55)]
        _, state = auction_state(kept)
        # $145 left, 14 spots to fill, so $13 stays in reserve.
        assert state.max_bid(MY_KEY) == 145 - 13

    def test_a_free_keeper_still_takes_a_roster_spot(self):
        kept = [KeptPlayer(player_key="461.p.RB0", team_key=MY_KEY, cost=None)]
        _, state = auction_state(kept)
        assert state.spent(MY_KEY) == 0
        assert state.slots_remaining(MY_KEY) == 14

    def test_rival_keeper_salaries_reduce_the_money_in_the_room(self):
        """This is what feeds inflation.

        Keeper salaries are money nobody can bid with. Ignoring them would overstate the
        pool and over-value every remaining player.
        """
        kept = [
            KeptPlayer(player_key=f"461.p.RB{index}", team_key=f"461.l.1.t.{index + 1}", cost=40)
            for index in range(NUM_TEAMS)
        ]
        _, state = auction_state(kept)
        assert state.league_money_remaining() == BUDGET * NUM_TEAMS - 40 * NUM_TEAMS

    def test_expensive_keepers_deflate_prices(self):
        cheap, _ = auction_state(
            [
                KeptPlayer(player_key=f"461.p.RB{i}", team_key=f"461.l.1.t.{i + 1}", cost=1)
                for i in range(NUM_TEAMS)
            ]
        )
        pricey, _ = auction_state(
            [
                KeptPlayer(player_key=f"461.p.RB{i}", team_key=f"461.l.1.t.{i + 1}", cost=90)
                for i in range(NUM_TEAMS)
            ]
        )
        assert pricey.current_inflation() < cheap.current_inflation()


class TestKeeperStateApi:
    def test_state_exposes_my_keepers(self):
        kept = [KeptPlayer(player_key="461.p.RB0", team_key=MY_KEY, cost=30)]
        assistant, _ = auction_state(kept)
        payload = assistant.snapshot_state()

        assert payload["keeper_count"] == 1
        assert len(payload["my_keepers"]) == 1
        entry = payload["my_keepers"][0]
        assert entry["position"] == "RB"
        assert entry["cost"] == 30
        assert entry["source"] == "yahoo"

    def test_notes_report_the_shortened_draft(self):
        kept = [
            KeptPlayer(player_key=f"461.p.RB{index}", team_key=f"461.l.1.t.{index + 1}")
            for index in range(NUM_TEAMS)
        ]
        assistant, _ = snake_state(kept)
        assert any("keepers held out of the pool" in note for note in assistant.notes)

    def test_keepers_and_live_picks_coexist(self):
        kept = [KeptPlayer(player_key="461.p.RB0", team_key=MY_KEY)]
        assistant, state = snake_state(kept)
        state.apply_sync(
            [DraftPick(pick=1, round=1, team_key="461.l.1.t.1", player_key="461.p.WR0")],
            timestamp=0.0,
        )
        unavailable = state.drafted_player_keys
        assert {"461.p.RB0", "461.p.WR0"} <= unavailable
        assert state.current_pick == 2  # the keeper did not consume a pick number
