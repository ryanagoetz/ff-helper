"""Tests for the Yahoo JSON normalizers.

These carry more weight than usual: this layer is the most likely thing to break silently
against live data, and the failure mode (a player quietly missing from recommendations)
is invisible until it costs you a pick.
"""

from __future__ import annotations

from ff_helper.yahoo.models import STAT_PASS_TD, STAT_REC, STAT_REC_YDS
from ff_helper.yahoo.parse import (
    collection_items,
    flatten,
    parse_draft_results,
    parse_league,
    parse_players,
    parse_teams,
    unwrap,
)
from ff_helper.yahoo.parse import content as strip_envelope


class TestPrimitives:
    def test_collection_items_skips_count_key(self):
        node = {"0": "a", "1": "b", "count": 2}
        assert collection_items(node) == ["a", "b"]

    def test_collection_items_ignores_non_numeric_keys(self):
        node = {"0": "a", "some_field": "junk", "count": 1}
        assert collection_items(node) == ["a"]

    def test_collection_items_handles_plain_list(self):
        assert collection_items([{"a": 1}, {"b": 2}]) == [{"a": 1}, {"b": 2}]

    def test_collection_items_tolerates_missing(self):
        assert collection_items(None) == []
        assert collection_items("unexpected") == []

    def test_flatten_merges_nested_fragment_lists(self):
        node = [[{"a": 1}, {"b": 2}], {"c": 3}]
        assert flatten(node) == {"a": 1, "b": 2, "c": 3}

    def test_flatten_later_fragments_win(self):
        assert flatten([{"a": 1}, {"a": 2}]) == {"a": 2}

    def test_unwrap_works_on_both_shapes(self):
        assert unwrap({"league": "x"}, "league") == "x"
        assert unwrap([{"other": 1}, {"league": "x"}], "league") == "x"
        assert unwrap([], "league") is None


class TestLeague:
    def test_parses_metadata_and_settings(self, fixture):
        node = unwrap(strip_envelope(fixture("league_settings.json")), "league")
        league = parse_league(node)

        assert league.league_key == "461.l.123456"
        assert league.num_teams == 12
        assert league.draft_status == "predraft"
        assert not league.is_drafting
        assert league.settings is not None

    def test_stat_modifiers_parsed_as_numbers(self, fixture):
        node = unwrap(strip_envelope(fixture("league_settings.json")), "league")
        settings = parse_league(node).settings

        # Yahoo sends these as strings; scoring math needs floats.
        assert settings.stat_modifiers[STAT_REC] == 1.0
        assert settings.stat_modifiers[STAT_REC_YDS] == 0.1
        assert settings.stat_modifiers[STAT_PASS_TD] == 4.0

    def test_roster_slots_and_starter_counts(self, fixture):
        node = unwrap(strip_envelope(fixture("league_settings.json")), "league")
        settings = parse_league(node).settings

        assert settings.bench_size == 6
        # IR does not count toward a drafted roster.
        assert settings.roster_size == 15

        # W/R/T counts toward RB, WR and TE, since any of them can fill it.
        assert settings.starters_at("RB") == 3
        assert settings.starters_at("WR") == 3
        assert settings.starters_at("TE") == 2
        assert settings.starters_at("QB") == 1

    def test_bench_slots_excluded_from_starters(self, fixture):
        node = unwrap(strip_envelope(fixture("league_settings.json")), "league")
        settings = parse_league(node).settings
        assert all(slot.position not in {"BN", "IR"} for slot in settings.starting_slots)


class TestDraftResults:
    def test_parses_and_sorts_picks(self, fixture):
        picks = parse_draft_results(fixture("draft_results.json"))
        assert [pick.pick for pick in picks] == [1, 2]
        assert picks[0].player_key == "461.p.100001"
        assert picks[0].team_key == "461.l.123456.t.4"

    def test_skips_picks_that_have_not_happened_yet(self, fixture):
        # Pick 3 is on the clock: present in the payload but with an empty player_key.
        # Treating it as a selection would corrupt the board.
        picks = parse_draft_results(fixture("draft_results.json"))
        assert all(pick.player_key for pick in picks)
        assert len(picks) == 2


class TestPlayers:
    def test_parses_all_players_in_page(self, fixture):
        players = parse_players(fixture("players_page.json"))
        assert len(players) == 3
        assert [p.full_name for p in players] == [
            "Ja'Marr Chase",
            "Kenneth Walker III",
            "Travis Kelce",
        ]

    def test_eligible_positions_from_list_shape(self, fixture):
        players = parse_players(fixture("players_page.json"))
        assert players[0].eligible_positions == ("WR",)

    def test_eligible_positions_from_numeric_dict_shape(self, fixture):
        # The multi-position player serializes as a numeric-keyed dict rather than a
        # list. Handling only one of these shapes is the classic silent-failure bug.
        players = parse_players(fixture("players_page.json"))
        assert players[1].eligible_positions == ("RB", "W/R/T")

    def test_primary_position_ignores_flex_slots(self, fixture):
        players = parse_players(fixture("players_page.json"))
        assert players[1].primary_position == "RB"

    def test_draft_analysis_numbers(self, fixture):
        players = parse_players(fixture("players_page.json"))
        assert players[0].draft_analysis.average_pick == 1.4
        assert players[0].draft_analysis.percent_drafted == 1.0

    def test_missing_adp_becomes_none_not_zero(self, fixture):
        # Yahoo uses "-" and "" for absent values. Coercing those to 0.0 would make an
        # undrafted player look like the 1.00 overall pick.
        players = parse_players(fixture("players_page.json"))
        kelce = players[2]
        assert kelce.draft_analysis.average_pick is None
        assert kelce.draft_analysis.average_round is None
        assert kelce.draft_analysis.percent_drafted == 0.41

    def test_bye_week_and_status(self, fixture):
        players = parse_players(fixture("players_page.json"))
        assert players[0].bye_week == 10
        assert players[1].status == "Q"
        assert players[2].status == ""


class TestTeams:
    def test_identifies_my_team(self, fixture):
        teams = parse_teams(fixture("teams.json"))
        assert len(teams) == 2
        mine = [team for team in teams if team.is_mine]
        assert len(mine) == 1
        assert mine[0].team_key == "461.l.123456.t.1"
        assert mine[0].draft_position == 5
