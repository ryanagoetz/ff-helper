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
from ff_helper.engine.auction import MIN_BID, compute_par_values, inflation_factor
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

    def test_roster_arriving_as_a_list_of_fragments_still_parses(self):
        """Yahoo serializes the same object two ways, and this endpoint uses both.

        Finding the players collection only one level down, and only under a dict, means
        the list-of-fragments spelling yields zero keepers -- silently, which puts every
        kept player back in the pool.
        """
        payload = {
            "fantasy_content": {
                "team": [
                    [{"team_key": "t.1"}],
                    {
                        "roster": [
                            {"coverage_type": "week"},
                            {"0": {"players": {"0": {"player": [[{"player_key": "p.1"}]]}}}},
                        ]
                    },
                ]
            }
        }
        assert [k.player_key for k in parse_roster(payload, "t.1")] == ["p.1"]

    def test_yahoos_false_keeper_block_is_not_a_zero_salary(self):
        # Yahoo spells "not a keeper" as is_keeper: {"status": false, "cost": false}.
        # `false` numifies to 0, and $0 is a real auction price -- it must not stand in
        # for "no salary published".
        payload = {
            "fantasy_content": {
                "team": [
                    [{"team_key": "t.1"}],
                    {
                        "roster": {
                            "0": {
                                "players": {
                                    "0": {
                                        "player": [
                                            [
                                                {"player_key": "p.1"},
                                                {"is_keeper": {"status": False, "cost": False}},
                                            ]
                                        ]
                                    }
                                }
                            }
                        }
                    },
                ]
            }
        }
        assert parse_roster(payload, "t.1")[0].cost is None


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
        with pytest.raises(keepers.KeeperError, match=r"player 'Not A Real Person' not found"):
            keepers.load_csv(path, registry, auction_teams())

    def test_unknown_team_fails_loudly(self, tmp_path, registry):
        path = self._write(tmp_path, "player,team\nRB Player0,Nonexistent Team\n")
        with pytest.raises(keepers.KeeperError, match=r"team 'Nonexistent Team' not found"):
            keepers.load_csv(path, registry, auction_teams())

    def test_missing_file_is_a_clear_error(self, tmp_path, registry):
        with pytest.raises(keepers.KeeperError, match="not found"):
            keepers.load_csv(tmp_path / "nope.csv", registry, auction_teams())

    def test_a_file_that_resolves_to_nothing_is_an_error(self, tmp_path, registry):
        """The total-loss case, and the worst one.

        An unrecognised player column loads zero keepers -- and because a CSV overrides
        Yahoo, that empty result replaces a perfectly good roster read. Every keeper in
        the league silently returns to the pool.
        """
        path = self._write(tmp_path, "athlete,squad\nRB Player0,Team 5\n")
        with pytest.raises(keepers.KeeperError, match="no keepers"):
            keepers.load_csv(path, registry, auction_teams())

    def test_empty_file_is_an_error(self, tmp_path, registry):
        with pytest.raises(keepers.KeeperError, match="no keepers"):
            keepers.load_csv(self._write(tmp_path, "player,team,cost\n"), registry, auction_teams())

    def test_a_blank_player_cell_is_an_error_not_a_skip(self, tmp_path, registry):
        path = self._write(tmp_path, "player,team,cost\n,Team 5,55\n")
        with pytest.raises(keepers.KeeperError, match="no player name"):
            keepers.load_csv(path, registry, auction_teams())

    def test_a_blank_column_falls_through_to_the_next_spelling(self, tmp_path, registry):
        path = self._write(tmp_path, "player,name,team\n,RB Player0,Team 5\n")
        result = keepers.load_csv(path, registry, auction_teams())
        assert result.kept[0].player_key == "461.p.RB0"

    def test_unreadable_salary_fails_instead_of_becoming_free(self, tmp_path, registry):
        # A cost that quietly parses to None is spent as $0, handing that team the whole
        # salary back as apparent budget and inflating every price in the room.
        path = self._write(tmp_path, "player,team,cost\nRB Player0,Team 5,5 5\n")
        with pytest.raises(keepers.KeeperError, match="not a number"):
            keepers.load_csv(path, registry, auction_teams())

    def test_thousands_separators_and_trailing_dollar_parse(self, tmp_path, registry):
        path = self._write(tmp_path, 'player,team,cost\nRB Player0,Team 5,"1,200"\n')
        assert keepers.load_csv(path, registry, auction_teams()).kept[0].cost == 1200

    def test_the_same_player_listed_twice_is_an_error(self, tmp_path, registry):
        path = self._write(tmp_path, "player,team\nRB Player0,Team 5\nRB Player0,Team 2\n")
        with pytest.raises(keepers.KeeperError, match="listed 2 times"):
            keepers.load_csv(path, registry, auction_teams())

    def test_a_misspelled_name_still_matches(self, tmp_path, registry):
        # The fuzzy fallback exists so one typo is not a hard startup abort. Passing an
        # empty position made it structurally unable to match anything.
        path = self._write(tmp_path, "player,team\nRB Playr0,Team 5\n")
        result = keepers.load_csv(path, registry, auction_teams())
        assert result.kept[0].player_key == "461.p.RB0"

    def test_a_non_utf8_file_gives_advice_not_a_traceback(self, tmp_path, registry):
        path = tmp_path / "keepers.csv"
        path.write_bytes("player,team\nRB Player0,Se\xf1or Sacks\n".encode("latin-1"))
        with pytest.raises(keepers.KeeperError, match="UTF-8"):
            keepers.load_csv(path, registry, auction_teams())

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
        # Every team keeps exactly one, so the draft really is uniform.
        rostered = [
            KeptPlayer(player_key=f"461.p.RB{index}", team_key=f"461.l.1.t.{index + 1}")
            for index in range(NUM_TEAMS)
        ]
        result = keepers.from_yahoo(rostered, auction_teams())
        assert not any("different numbers" in note for note in result.notes)

    def test_teams_keeping_nobody_count_as_uneven(self):
        """The zeros are the whole point.

        Two teams keeping one player each in a twelve-team league is maximally uneven --
        ten teams draft a full roster and two draft one short. Tallying only the teams
        holding keepers makes that look uniform and skips the warning.
        """
        rostered = [
            KeptPlayer(player_key="461.p.RB0", team_key="461.l.1.t.1"),
            KeptPlayer(player_key="461.p.RB1", team_key="461.l.1.t.2"),
        ]
        result = keepers.from_yahoo(rostered, auction_teams())
        assert any("different numbers" in note for note in result.notes)


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


class TestKeeperAwarePricing:
    """Keepers are priced out of the auction pool.

    The point of this is *not* to change the numbers. ``par - 1`` is exactly
    ``VOR * dollars_per_vor``, so keepers distort one scalar, and ``inflation_factor``
    cancels that same scalar precisely -- the final values were identical either way. What
    excluding them buys is that ``inflation`` stops carrying a static keeper correction on
    top of live market movement, which is what was eating the clamp headroom.
    """

    def _with_keepers(self, per_team: int, salary: int):
        league = auction_league()
        teams = auction_teams()
        baseline = Assistant.build(
            league,
            DraftState(league=league, teams=teams),
            build_snapshot(),
            lock=threading.Lock(),
        )
        ranked = sorted(
            baseline.valuations.valuations.values(), key=lambda v: -baseline.levels.vor(v)
        )
        kept = [
            KeptPlayer(
                player_key=ranked[index].player_key,
                team_key=f"461.l.1.t.{index % NUM_TEAMS + 1}",
                cost=salary,
            )
            for index in range(per_team * NUM_TEAMS)
        ]
        state = DraftState(league=league, teams=teams)
        state.apply_keepers(kept)
        return Assistant.build(league, state, build_snapshot(), lock=threading.Lock()), kept

    def test_pool_size_counts_only_open_spots(self):
        assistant, kept = self._with_keepers(2, 40)
        assert assistant.dollars.pool_size == NUM_TEAMS * 15 - len(kept)

    def test_keeper_salaries_leave_the_biddable_money(self):
        """A keeper's salary is spent, so it cannot also be chasing the remaining players."""
        plain, _ = self._with_keepers(0, 0)
        assistant, _ = self._with_keepers(3, 45)
        # Less money over fewer players, but the players removed are the best ones, so
        # the rate per point of VOR rises rather than falls.
        assert assistant.dollars.dollars_per_vor > plain.dollars.dollars_per_vor

    def test_inflation_starts_neutral_instead_of_absorbing_a_keeper_correction(self):
        """The reason for the change, stated as a test.

        Previously a keeper league opened with inflation well above 1.0 purely because par
        was computed as if nobody had kept anyone -- at five keepers a team that was 2.44
        of a 3.0 ceiling, leaving almost no room for real market movement before the clamp
        truncated the correction and broke it.
        """
        for per_team, salary in ((1, 40), (3, 45), (5, 35)):
            assistant, _ = self._with_keepers(per_team, salary)
            assert assistant.current_inflation() == pytest.approx(1.0, abs=0.05), (
                f"{per_team} keepers/team opened at {assistant.current_inflation():.2f}"
            )

    def test_recommended_bids_are_unchanged_by_the_refactor(self):
        """The equivalence this rests on: same dollars out, one signal instead of two.

        Recomputing par the old way (keepers left in the pool) and applying the inflation
        it produces must land on the same adjusted value as pricing keepers out up front.
        """
        assistant, kept = self._with_keepers(3, 45)
        old_style = compute_par_values(
            list(assistant.valuations.valuations.values()),
            assistant.levels,
            assistant.league.settings,
            NUM_TEAMS,
        )
        available = assistant.available()
        old_inflation = inflation_factor(
            available,
            old_style,
            money_remaining=assistant.state.league_money_remaining(),
            slots_remaining=assistant.state.league_slots_remaining(),
        )
        new_inflation = assistant.current_inflation()

        # Guard against a vacuous test: the two paths must genuinely disagree about the
        # inflation figure, and agree only after it is applied. If these ever converge,
        # the assertions below would pass without proving anything.
        assert old_inflation > new_inflation * 1.05

        for valuation in sorted(available, key=lambda v: -assistant.levels.vor(v))[:10]:
            key = valuation.player_key
            old_value = MIN_BID + (old_style.value_of(key) - MIN_BID) * old_inflation
            new_value = MIN_BID + (assistant.dollars.value_of(key) - MIN_BID) * new_inflation
            assert new_value == pytest.approx(old_value, abs=0.01)

    def test_a_league_with_no_keepers_is_untouched(self):
        assistant, _ = self._with_keepers(0, 0)
        assert assistant.dollars.pool_size == NUM_TEAMS * 15
        assert assistant.current_inflation() == pytest.approx(1.0, abs=0.05)


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


class TestKeeperAndBoardOverlap:
    """A player can arrive as both a keeper and a pick. He is still one player.

    Yahoo lists kept players inside ``draftresults`` in some keeper leagues, a restart
    mid-draft re-reads rosters that now hold drafted players, and a pick can be entered by
    hand. Counting the overlap twice charges the salary twice and takes two roster spots,
    which quietly halves the max bid.
    """

    def _kept_then_drafted(self, pick_cost: int | None = None):
        """A keeper the feed later reports as a pick.

        ``pick_cost`` defaults to None because that is what Yahoo actually sends for a
        kept player: it appears in draftresults, but no sale happened so there is no
        price. Giving the pick the same cost as the keeper -- the obvious fixture to
        write -- is the one case where the overlap tie-break cannot change the answer,
        so it would pass no matter which side won and catch nothing.
        """
        state = DraftState(league=auction_league(), teams=auction_teams(), roster_size=15)
        state.apply_keepers([KeptPlayer(player_key="461.p.RB0", team_key=MY_KEY, cost=55)])
        state.apply_sync(
            [DraftPick(pick=1, round=1, team_key=MY_KEY, player_key="461.p.RB0", cost=pick_cost)],
            timestamp=0.0,
        )
        return state

    def test_salary_survives_a_priceless_pick(self):
        """Deduplicating must not turn double-counting into zero-counting.

        The keeper is excluded from keepers_for so the board can own the roster spot, but
        the board's pick carries no price -- so the $55 has to come from the keeper or it
        disappears, inflating every budget in the room and with it the inflation model.
        """
        assert self._kept_then_drafted(pick_cost=None).spent(MY_KEY) == 55

    def test_an_actual_sale_price_wins_over_the_keeper_salary(self):
        # If the board does report a price, that is what was really paid.
        assert self._kept_then_drafted(pick_cost=70).spent(MY_KEY) == 70

    def test_salary_is_charged_once(self):
        assert self._kept_then_drafted(pick_cost=55).spent(MY_KEY) == 55

    def test_one_roster_spot_is_taken(self):
        state = self._kept_then_drafted(pick_cost=55)
        assert state.slots_filled(MY_KEY) == 1
        assert state.slots_remaining(MY_KEY) == 14

    def test_position_is_counted_once(self):
        state = self._kept_then_drafted(pick_cost=55)
        assert state.roster_counts(MY_KEY, {"461.p.RB0": "RB"}) == {"RB": 1}

    def test_max_bid_is_not_halved(self):
        assert self._kept_then_drafted(pick_cost=55).max_bid(MY_KEY) == 145 - 13

    def test_the_same_keeper_twice_fills_one_spot(self):
        state = DraftState(league=auction_league(), teams=auction_teams(), roster_size=15)
        state.apply_keepers(
            [
                KeptPlayer(player_key="461.p.RB0", team_key=MY_KEY, cost=55),
                KeptPlayer(player_key="461.p.RB0", team_key=MY_KEY, cost=55),
            ]
        )
        assert state.slots_filled(MY_KEY) == 1
        assert state.spent(MY_KEY) == 55


class TestMyPicksUseMyOwnKeeperCount:
    """``rounds`` is the league-wide mode; my own turn count is not.

    Keeping fewer players than most of the league means drafting more times than
    ``rounds`` says. Using the collapsed number drops my last picks off the board
    entirely -- no next pick, no countdown, and a VONA horizon measured against a gap
    that does not exist.
    """

    def _uneven(self):
        # Seven of twelve teams keep two, so the mode is 2 and the draft shortens to 13
        # rounds. My team (slot 5) keeps nobody, so I still draft all 15.
        kept = [
            KeptPlayer(player_key=f"461.p.RB{index}", team_key=f"461.l.1.t.{team}")
            for team in range(1, 9)
            if team != MY_SLOT
            for index in (team * 2, team * 2 + 1)
        ]
        state = DraftState(league=build_league(), teams=auction_teams(), roster_size=15)
        state.apply_keepers(kept)
        return state

    def test_i_draft_the_full_roster_when_i_kept_nobody(self):
        state = self._uneven()
        assert state.rounds < 15  # the league-wide mode is shortened
        assert len(state.my_picks) == 15  # but I still pick fifteen times

    def test_my_last_picks_are_not_lost(self):
        state = self._uneven()
        second_to_last = state.my_picks[-2]
        assert state.next_pick_after(second_to_last) == state.my_picks[-1]

    def test_a_team_that_kept_the_mode_is_unaffected(self):
        state = self._uneven()
        # Team 1 kept two, so it drafts thirteen times.
        assert len(state.keepers_for("461.l.1.t.1")) == 2
        assert state.roster_size - len(state.keepers_for("461.l.1.t.1")) == 13


class TestUnevenKeepersDoNotEndTheDraftEarly:
    """``rounds`` collapses uneven keeper counts to one number; ``total_picks`` must not.

    Under-counting here makes ``is_complete`` true while picks are still arriving, and
    ``DraftSync._run`` returns for good when that happens -- the board freezes with no
    error, which is worse than any wrong number it was protecting.
    """

    def _uneven(self):
        kept = []
        for index in range(1, 9):  # 8 of 12 teams keep 2, the rest keep none
            kept += [
                KeptPlayer(player_key=f"k{index}a", team_key=f"461.l.1.t.{index}"),
                KeptPlayer(player_key=f"k{index}b", team_key=f"461.l.1.t.{index}"),
            ]
        state = DraftState(league=auction_league(), teams=auction_teams(), roster_size=15)
        state.apply_keepers(kept)
        return state, kept

    def test_total_picks_counts_every_real_pick(self):
        state, kept = self._uneven()
        assert state.total_picks == NUM_TEAMS * 15 - len(kept)

    def test_draft_is_not_complete_before_the_last_pick(self):
        state, kept = self._uneven()
        state.apply_sync(
            [
                DraftPick(pick=p, round=1, team_key="461.l.1.t.1", player_key=f"p{p}")
                for p in range(1, NUM_TEAMS * state.rounds + 1)
            ],
            timestamp=0.0,
        )
        assert not state.is_complete

    def test_even_keepers_still_shorten_the_draft(self):
        kept = [
            KeptPlayer(player_key=f"461.p.RB{index}", team_key=f"461.l.1.t.{index + 1}")
            for index in range(NUM_TEAMS)
        ]
        state = DraftState(league=auction_league(), teams=auction_teams(), roster_size=15)
        state.apply_keepers(kept)
        assert state.rounds == 14
        assert state.total_picks == NUM_TEAMS * 14


class TestRosterSizeFollowsRounds:
    def test_a_custom_round_count_sets_the_roster(self):
        # Otherwise roster_size silently keeps its default and slots_remaining -- and so
        # max_bid -- measures against a roster the league does not have.
        state = DraftState(league=auction_league(), teams=auction_teams(), rounds=3)
        assert state.roster_size == 3
        assert state.slots_remaining(MY_KEY) == 3

    def test_an_explicit_roster_size_still_wins(self):
        state = DraftState(
            league=auction_league(), teams=auction_teams(), rounds=13, roster_size=15
        )
        assert state.roster_size == 15


class TestUnpricedAuctionKeepers:
    def test_missing_salaries_are_called_out(self):
        """Unknown is not free.

        ``spent()`` can only treat a missing salary as $0, which leaves that money in the
        room: inflation reads the league as cash-rich against fewer slots and every bid
        ceiling comes out high. It has to be said out loud.
        """
        kept = [
            KeptPlayer(player_key=f"461.p.RB{index}", team_key=f"461.l.1.t.{index + 1}")
            for index in range(NUM_TEAMS)
        ]
        assistant, _ = auction_state(kept)
        assert any("no salary from Yahoo" in note for note in assistant.notes)

    def test_priced_keepers_do_not_warn(self):
        kept = [
            KeptPlayer(player_key=f"461.p.RB{index}", team_key=f"461.l.1.t.{index + 1}", cost=40)
            for index in range(NUM_TEAMS)
        ]
        assistant, _ = auction_state(kept)
        assert not any("no salary from Yahoo" in note for note in assistant.notes)

    def test_a_keeper_outside_the_snapshot_is_called_out(self):
        assistant, _ = auction_state([KeptPlayer(player_key="461.p.NOPE", team_key=MY_KEY, cost=5)])
        assert any("not in the ranking snapshot" in note for note in assistant.notes)
