"""In-season data layer: parsers, the weekly CSV source, the snapshot, and the blend."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ff_helper.rankings.players import PlayerRegistry
from ff_helper.rankings.sources import weekly_csv
from ff_helper.rankings.sources.projections_csv import ProjectionsError
from ff_helper.season import cache as week_cache
from ff_helper.season.valuation import play_probability, weekly_blend
from ff_helper.yahoo.models import (
    STAT_REC,
    STAT_REC_TD,
    STAT_REC_YDS,
    STAT_RUSH_TD,
    STAT_RUSH_YDS,
    LeagueSettings,
    Matchup,
    RosterEntry,
    RosterSlot,
    Transaction,
    YahooPlayer,
)
from ff_helper.yahoo.parse import content as strip_envelope
from ff_helper.yahoo.parse import (
    parse_league,
    parse_player_stats,
    parse_players,
    parse_roster,
    parse_roster_entries,
    parse_scoreboard,
    parse_settings,
    parse_transactions,
    unwrap,
)

FIXTURES = Path(__file__).parent / "fixtures"

WEEKLY_CSV = FIXTURES / "weekly-projections.csv"


def weekly_settings() -> LeagueSettings:
    """Half-PPR-ish scoring, so a re-scored stat line is visibly not the export's total."""
    return LeagueSettings(
        roster_slots=(
            RosterSlot("QB", 1),
            RosterSlot("RB", 2),
            RosterSlot("WR", 2),
            RosterSlot("TE", 1),
            RosterSlot("W/R/T", 1),
            RosterSlot("K", 1),
            RosterSlot("DEF", 1),
            RosterSlot("BN", 6),
        ),
        stat_modifiers={
            STAT_RUSH_YDS: 0.1,
            STAT_RUSH_TD: 6.0,
            STAT_REC: 0.5,
            STAT_REC_YDS: 0.1,
            STAT_REC_TD: 6.0,
        },
        is_auction=False,
    )


class TestLeagueAndSettingsParsing:
    def test_week_fields_are_kept(self, fixture):
        payload = fixture("scoreboard.json")
        league = parse_league(unwrap(strip_envelope(payload), "league"))
        assert league.current_week == 7
        assert league.start_week == 1
        assert league.end_week == 17
        assert league.is_finished is False

    def test_absent_week_fields_stay_none_rather_than_defaulting_to_one(self, fixture):
        payload = fixture("league_settings.json")
        league = parse_league(unwrap(strip_envelope(payload), "league"))
        # The draft fixture publishes no week. None forces every in-season caller to
        # handle the gap instead of quietly picking week 1 and being wrong all season.
        assert league.current_week is None

    def test_waiver_and_playoff_settings_are_kept(self):
        settings = parse_settings(
            {
                "draft_type": "live",
                "waiver_type": "FR",
                "waiver_rule": "gametime",
                "uses_faab": "1",
                "faab_budget": "100",
                "trade_end_date": "2026-11-27",
                "playoff_start_week": "15",
                "num_playoff_teams": "6",
                "roster_positions": [{"roster_position": {"position": "QB", "count": 1}}],
                "stat_modifiers": {"stats": [{"stat": {"stat_id": 11, "value": "0.5"}}]},
            }
        )
        assert settings.waiver_type == "FR"
        assert settings.uses_faab is True
        assert settings.faab_budget == 100
        assert settings.playoff_start_week == 15
        assert settings.num_playoff_teams == 6

    def test_a_league_without_the_new_settings_still_parses(self, fixture):
        payload = fixture("league_settings.json")
        league = parse_league(unwrap(strip_envelope(payload), "league"))
        assert league.settings is not None
        assert league.settings.uses_faab is False
        assert league.settings.playoff_start_week is None


class TestRosterEntries:
    def test_selected_position_is_kept(self, fixture):
        entries = parse_roster_entries(fixture("roster_week.json"), "461.l.123456.t.1", 7)
        slots = {entry.player_key: entry.selected_position for entry in entries}
        assert slots["461.p.100001"] == "WR"
        assert slots["461.p.100002"] == "W/R/T"
        assert slots["461.p.100003"] == "BN"
        assert slots["461.p.100004"] == "IR"

    def test_starters_exclude_bench_and_ir(self, fixture):
        entries = parse_roster_entries(fixture("roster_week.json"), "461.l.123456.t.1", 7)
        starting = {entry.player_key for entry in entries if entry.is_starting}
        assert starting == {"461.p.100001", "461.p.100002"}

    def test_an_unreported_slot_is_empty_not_bench(self, fixture):
        entries = parse_roster_entries(fixture("roster_week.json"), "461.l.123456.t.1", 7)
        unknown = next(e for e in entries if e.player_key == "461.p.100005")
        # "" and "BN" must stay distinguishable: one is Yahoo saying nothing, the other
        # is a deliberate sit, and scoring them the same would invent a decision.
        assert unknown.selected_position == ""
        assert unknown.is_starting is False

    def test_the_requested_week_is_recorded(self, fixture):
        entries = parse_roster_entries(fixture("roster_week.json"), "461.l.123456.t.1", 7)
        assert {entry.week for entry in entries} == {7}

    def test_parse_roster_is_unchanged(self, fixture):
        """The keeper path must not shift because an in-season sibling was added."""
        kept = parse_roster(fixture("roster.json"), "461.l.123456.t.1")
        assert [k.player_key for k in kept] == ["461.p.100001", "461.p.100002"]
        assert kept[0].cost == 55
        assert kept[1].cost is None
        assert all(k.source == "yahoo" for k in kept)


class TestScoreboard:
    def test_each_pairing_yields_both_perspectives(self, fixture):
        matchups = parse_scoreboard(fixture("scoreboard.json"))
        assert len(matchups) == 4  # two pairings, two directions each

        mine = next(m for m in matchups if m.team_key == "461.l.123456.t.1")
        theirs = next(m for m in matchups if m.team_key == "461.l.123456.t.2")
        assert mine.opponent_key == "461.l.123456.t.2"
        assert theirs.opponent_key == "461.l.123456.t.1"
        assert mine.points == pytest.approx(112.34)
        assert mine.opponent_points == pytest.approx(98.06)
        assert theirs.points == pytest.approx(98.06)

    def test_teams_nested_under_a_numeric_wrapper_are_found(self, fixture):
        """The first matchup buries `teams` a level down; the second does not."""
        matchups = parse_scoreboard(fixture("scoreboard.json"))
        keys = {m.team_key for m in matchups}
        assert "461.l.123456.t.1" in keys  # nested shape
        assert "461.l.123456.t.3" in keys  # flat shape

    def test_status_and_projections_survive(self, fixture):
        matchups = parse_scoreboard(fixture("scoreboard.json"))
        mine = next(m for m in matchups if m.team_key == "461.l.123456.t.1")
        upcoming = next(m for m in matchups if m.team_key == "461.l.123456.t.3")
        assert mine.is_final is True
        assert mine.opponent_projected == pytest.approx(121.75)
        assert upcoming.status == "preevent"
        assert upcoming.points is None


class TestTransactions:
    def test_add_and_drop_are_split(self, fixture):
        transactions = parse_transactions(fixture("transactions.json"))
        add_drop = next(t for t in transactions if t.transaction_key.endswith("tr.42"))
        assert add_drop.added == ("461.p.200001",)
        assert add_drop.dropped == ("461.p.100003",)
        assert add_drop.team_key == "461.l.123456.t.1"

    def test_the_winning_faab_bid_is_kept(self, fixture):
        transactions = parse_transactions(fixture("transactions.json"))
        add_drop = next(t for t in transactions if t.transaction_key.endswith("tr.42"))
        assert add_drop.bid == 14
        assert add_drop.source_type == "waivers"

    def test_transaction_data_as_a_list_is_handled(self, fixture):
        """The drop leg arrives as a one-element list where the add is a bare dict."""
        transactions = parse_transactions(fixture("transactions.json"))
        add_drop = next(t for t in transactions if t.transaction_key.endswith("tr.42"))
        assert add_drop.dropped == ("461.p.100003",)

    def test_a_free_agent_add_has_no_bid(self, fixture):
        transactions = parse_transactions(fixture("transactions.json"))
        plain = next(t for t in transactions if t.transaction_key.endswith("tr.43"))
        assert plain.bid is None
        assert plain.source_type == "freeagents"
        assert plain.dropped == ()


class TestPlayerStatusFields:
    def test_status_full_and_ownership_are_kept(self, fixture):
        players = {p.player_key: p for p in parse_players(fixture("players_status.json"))}
        hurt = players["461.p.200002"]
        assert hurt.status == "Q"
        assert hurt.status_full == "Questionable - Ankle"
        assert hurt.injury_note == "Limited in Wednesday practice"
        assert hurt.percent_owned == pytest.approx(74.0)

    def test_absent_ownership_is_none_not_zero(self, fixture):
        players = {p.player_key: p for p in parse_players(fixture("players_status.json"))}
        # "nobody rosters him" and "we did not ask" are different facts, and only one of
        # them is a waiver signal.
        assert players["461.p.200003"].percent_owned is None

    def test_startable_positions_keeps_multi_position_eligibility(self, fixture):
        players = {p.player_key: p for p in parse_players(fixture("players_status.json"))}
        assert players["461.p.200002"].startable_positions == ("WR", "RB")

    def test_startable_positions_falls_back_to_display_position(self):
        player = YahooPlayer(
            player_key="k",
            player_id="1",
            full_name="Bench Only",
            team_abbr="FA",
            display_position="TE",
            eligible_positions=("BN",),
        )
        assert player.startable_positions == ("TE",)


class TestPlayerStats:
    def test_realized_stats_land_in_the_projection_vocabulary(self):
        payload = {
            "fantasy_content": {
                "league": [
                    {"league_key": "461.l.123456"},
                    {
                        "players": {
                            "0": {
                                "player": [
                                    [{"player_key": "461.p.100001"}],
                                    {
                                        "player_stats": {
                                            "stats": [
                                                {"stat": {"stat_id": "11", "value": "9"}},
                                                {"stat": {"stat_id": "12", "value": "118"}},
                                                {"stat": {"stat_id": "13", "value": "1"}},
                                                # Not scoreable here; must not appear.
                                                {"stat": {"stat_id": "78", "value": "3"}},
                                            ]
                                        }
                                    },
                                ]
                            },
                            "count": 1,
                        }
                    },
                ]
            }
        }
        stats = parse_player_stats(payload)
        assert stats == {"461.p.100001": {"rec": 9.0, "rec_yds": 118.0, "rec_td": 1.0}}


class TestWeeklyCsv:
    def test_loads_and_filters_to_the_requested_week(self):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=7)
        names = [p.name for p in projections]
        assert "Ja'Marr Chase" in names
        # Chase appears twice in the file, once per week.
        assert names.count("Ja'Marr Chase") == 1

    def test_other_weeks_are_reported_not_silently_dropped(self):
        _, notes = weekly_csv.load(WEEKLY_CSV, week=7)
        assert any("other weeks" in note for note in notes)

    def test_opponent_and_home_away_are_parsed(self):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=7)
        by_name = {p.name: p for p in projections}
        assert by_name["Ja'Marr Chase"].opponent == "BAL"
        assert by_name["Ja'Marr Chase"].is_home is False
        assert by_name["Kenneth Walker III"].opponent == "ARI"
        assert by_name["Kenneth Walker III"].is_home is True

    def test_a_bye_row_is_counted_rather_than_raising(self):
        projections, notes = weekly_csv.load(WEEKLY_CSV, week=7)
        assert "Bye Week Player" not in {p.name for p in projections}
        assert any("no projection this week" in note for note in notes)

    def test_all_zero_skill_stats_are_blanked_so_interpolation_can_fire(self):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=7)
        kicker = next(p for p in projections if p.name == "Harrison Butker")
        assert kicker.row.stats == {}
        assert kicker.row.projected_points == pytest.approx(8.4)

    def test_points_without_stats_anywhere_is_rejected(self, tmp_path):
        path = tmp_path / "weekly-w07.csv"
        rows = "\n".join(f"Player {i},WR,BUF,@MIA,7,{10 + i}" for i in range(30))
        path.write_text(f"Player,Pos,Team,Opp,Week,FF Pts\n{rows}\n")
        with pytest.raises(ProjectionsError, match="per-stat columns"):
            weekly_csv.load(path, week=7)

    def test_a_short_file_is_rejected(self, tmp_path):
        path = tmp_path / "weekly-w07.csv"
        rows = "\n".join(f"Player {i},WR,BUF,@MIA,7,5,60,0.4" for i in range(5))
        path.write_text(f"Player,Pos,Team,Opp,Week,Rec,Rec Yds,Rec TD\n{rows}\n")
        with pytest.raises(ProjectionsError, match="too few"):
            weekly_csv.load(path, week=7)

    def test_an_unreadable_stat_cell_raises_rather_than_dropping_the_stat(self, tmp_path):
        path = tmp_path / "weekly-w07.csv"
        rows = [f"Player {i},WR,BUF,@MIA,7,5,60,0.4" for i in range(30)]
        rows[3] = "Broken Guy,WR,BUF,@MIA,7,5,1 234,0.4"
        path.write_text("Player,Pos,Team,Opp,Week,Rec,Rec Yds,Rec TD\n" + "\n".join(rows) + "\n")
        with pytest.raises(ProjectionsError, match="Broken Guy"):
            weekly_csv.load(path, week=7)

    def test_a_file_with_no_week_column_says_so(self, tmp_path):
        path = tmp_path / "weekly-w07.csv"
        rows = "\n".join(f"Player {i},WR,BUF,@MIA,5,60,0.4" for i in range(30))
        path.write_text(f"Player,Pos,Team,Opp,Rec,Rec Yds,Rec TD\n{rows}\n")
        _, notes = weekly_csv.load(path, week=7)
        assert any("no week column" in note for note in notes)


class TestResolvePath:
    def test_prefers_the_weekly_directory_and_the_league_specific_name(self, tmp_path):
        (tmp_path / "weekly").mkdir()
        keyed = tmp_path / "weekly" / "weekly-461.l.1-w07.csv"
        shared = tmp_path / "weekly" / "weekly-w07.csv"
        keyed.write_text("x")
        shared.write_text("x")
        path, league_specific = weekly_csv.resolve_path(tmp_path, "461.l.1", 7)
        assert path == keyed
        assert league_specific is True

    def test_falls_back_to_the_shared_file(self, tmp_path):
        shared = tmp_path / "weekly-w07.csv"
        shared.write_text("x")
        path, league_specific = weekly_csv.resolve_path(tmp_path, "461.l.1", 7)
        assert path == shared
        assert league_specific is False

    def test_the_week_is_part_of_the_name(self, tmp_path):
        (tmp_path / "weekly-w07.csv").write_text("x")
        path, _ = weekly_csv.resolve_path(tmp_path, "461.l.1", 8)
        assert path is None


class TestPlayProbability:
    def test_out_and_ir_are_zero(self):
        assert play_probability("O") == 0.0
        assert play_probability("IR") == 0.0

    def test_questionable_costs_something_unlike_the_season_model(self):
        from ff_helper.rankings.blend import availability_of

        # Across a season a questionable tag is noise; across one game it is not, and the
        # two models must be allowed to disagree.
        assert availability_of("Q") == 1.0
        assert play_probability("Q") < 1.0

    def test_an_unknown_status_reads_as_healthy(self):
        assert play_probability("NEW-TAG") == 1.0
        assert play_probability("") == 1.0


def weekly_players() -> list[YahooPlayer]:
    return [
        YahooPlayer(
            player_key="461.p.1",
            player_id="1",
            full_name="Ja'Marr Chase",
            team_abbr="CIN",
            display_position="WR",
            eligible_positions=("WR",),
            bye_week=10,
        ),
        YahooPlayer(
            player_key="461.p.2",
            player_id="2",
            full_name="Kenneth Walker III",
            team_abbr="SEA",
            display_position="RB",
            eligible_positions=("RB", "W/R/T"),
            bye_week=7,  # on bye in the week under test
        ),
        YahooPlayer(
            player_key="461.p.3",
            player_id="3",
            full_name="Rome Odunze",
            team_abbr="CHI",
            display_position="WR",
            eligible_positions=("WR",),
            bye_week=5,
            status="Q",
            status_full="Questionable - Knee",
        ),
        YahooPlayer(
            player_key="461.p.4",
            player_id="4",
            full_name="Harrison Butker",
            team_abbr="KC",
            display_position="K",
            eligible_positions=("K",),
            bye_week=6,
        ),
    ]


class TestWeeklyBlend:
    def test_points_are_rescored_not_taken_from_the_export(self):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=7)
        registry = PlayerRegistry(weekly_players())
        result = weekly_blend(registry, projections, weekly_settings(), 7)

        chase = result.valuations["461.p.1"]
        # 7.1 rec * 0.5 + 94.2 rec yds * 0.1 + 0.62 rec TD * 6 + 2 rush yds * 0.1 = 16.89
        # -- deliberately not the 18.4 the export claims, which assumed full PPR.
        assert chase.projected_points == pytest.approx(16.89, abs=0.01)
        assert chase.projected_points != pytest.approx(18.4, abs=0.01)
        assert chase.points_estimated is False

    def test_a_bye_is_zero_and_flagged_rather_than_missing(self):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=7)
        registry = PlayerRegistry(weekly_players())
        result = weekly_blend(registry, projections, weekly_settings(), 7)

        walker = result.valuations["461.p.2"]
        assert walker.is_bye is True
        assert walker.projected_points == 0.0
        assert walker.is_playable is False

    def test_injury_status_scales_the_projection(self):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=7)
        registry = PlayerRegistry(weekly_players())
        result = weekly_blend(registry, projections, weekly_settings(), 7)

        odunze = result.valuations["461.p.3"]
        base = 5.4 * 0.5 + 61.7 * 0.1 + 0.34 * 6.0
        assert odunze.base_points == pytest.approx(base, abs=0.01)
        assert odunze.availability == pytest.approx(play_probability("Q"))
        assert odunze.projected_points == pytest.approx(base * play_probability("Q"), abs=0.01)

    def test_eligible_positions_are_carried_through_without_flex_slot_names(self):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=7)
        registry = PlayerRegistry(weekly_players())
        result = weekly_blend(registry, projections, weekly_settings(), 7)
        # Yahoo lists "W/R/T" beside "RB"; a slot is not a position, and which slots he
        # can fill is derived from FLEX_ELIGIBILITY rather than claimed by the player.
        assert result.valuations["461.p.2"].eligible_positions == ("RB",)

    def test_a_kicker_is_left_unvalued_by_default_and_reported(self):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=7)
        registry = PlayerRegistry(weekly_players())
        result = weekly_blend(registry, projections, weekly_settings(), 7)

        assert "461.p.4" not in result.valuations
        assert any("no scoreable stat line" in note for note in result.notes)

    def test_trusting_source_points_values_the_kicker_and_says_so(self):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=7)
        registry = PlayerRegistry(weekly_players())
        result = weekly_blend(
            registry,
            projections,
            weekly_settings(),
            7,
            trust_source_points=frozenset({"K"}),
        )

        kicker = result.valuations["461.p.4"]
        assert kicker.projected_points == pytest.approx(8.4)
        assert kicker.points_estimated is True
        assert any("WARNING" in note and "exporter's scoring" in note for note in result.notes)

    def test_a_single_source_reports_no_disagreement_rather_than_a_guess(self):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=7)
        registry = PlayerRegistry(weekly_players())
        result = weekly_blend(registry, projections, weekly_settings(), 7)
        # points_stdev means source disagreement here and nothing else. Inventing outcome
        # variance at this layer would make a player two sources agree on look safe.
        assert result.valuations["461.p.1"].points_stdev == 0.0

    def test_no_news_store_means_no_adjustments_at_all(self):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=7)
        registry = PlayerRegistry(weekly_players())
        result = weekly_blend(registry, projections, weekly_settings(), 7)
        chase = result.valuations["461.p.1"]
        assert chase.adjustments == ()
        assert chase.effective_multiplier == pytest.approx(1.0)


def build_week_snapshot(week: int = 7) -> week_cache.WeekSnapshot:
    # Projections always come from week 7 of the fixture -- it is the only week there with
    # a full slate. ``week`` stamps the snapshot, which is what the storage and CLI tests
    # are actually exercising.
    projections, notes = weekly_csv.load(WEEKLY_CSV, week=7)
    return week_cache.WeekSnapshot(
        league_key="461.l.123456",
        season="2026",
        week=week,
        fetched_at=1000.0,
        league_name="Test Dynasty",
        num_teams=12,
        my_team_key="461.l.123456.t.1",
        team_names={"461.l.123456.t.1": "Team Ryan", "461.l.123456.t.2": "Rival Squad"},
        settings=weekly_settings(),
        players=weekly_players(),
        projections=projections,
        rosters={
            "461.l.123456.t.1": [
                RosterEntry("461.p.1", "461.l.123456.t.1", week, "WR"),
                RosterEntry("461.p.2", "461.l.123456.t.1", week, "W/R/T"),
                RosterEntry("461.p.3", "461.l.123456.t.1", week, "BN"),
            ]
        },
        free_agent_keys=["461.p.4"],
        matchups=[Matchup(week, "461.l.123456.t.1", "461.l.123456.t.2", 112.34, 98.06)],
        transactions=[Transaction("461.l.123456.tr.42", "add/drop", "successful", bid=14)],
        notes=notes,
    )


class TestWeekSnapshot:
    def build(self) -> week_cache.WeekSnapshot:
        return build_week_snapshot()

    def test_round_trips(self):
        original = self.build()
        path = week_cache.save(original)
        loaded = week_cache.load("461.l.123456", "2026", 7)

        assert loaded is not None
        assert path.name == "week-461.l.123456-2026-w07.json"
        assert loaded.week == 7
        assert loaded.season == "2026"
        assert [p.player_key for p in loaded.players] == [p.player_key for p in original.players]
        assert len(loaded.projections) == len(original.projections)
        assert loaded.projections[0].opponent == original.projections[0].opponent
        assert loaded.rosters["461.l.123456.t.1"][0].selected_position == "WR"
        assert loaded.matchups[0].opponent_key == "461.l.123456.t.2"
        assert loaded.transactions[0].bid == 14
        assert loaded.notes == original.notes

    def test_league_settings_survive_the_round_trip(self):
        """The whole point of storing them: reading a week back needs no network."""
        week_cache.save(self.build())
        loaded = week_cache.load("461.l.123456", "2026", 7)

        assert loaded is not None
        assert loaded.settings is not None
        # JSON turns dict keys into strings; a stat_modifiers keyed by "11" scores nothing.
        assert all(isinstance(key, int) for key in loaded.settings.stat_modifiers)
        assert loaded.settings.stat_modifiers[STAT_REC] == pytest.approx(0.5)
        assert [slot.position for slot in loaded.settings.roster_slots][:3] == ["QB", "RB", "WR"]

    def test_the_rebuilt_league_carries_the_week_and_scoring(self):
        league = self.build().league()
        assert league.current_week == 7
        assert league.settings is not None
        assert league.name == "Test Dynasty"

    def test_a_version_mismatch_refuses_rather_than_migrating(self, monkeypatch):
        week_cache.save(self.build())
        monkeypatch.setattr(week_cache, "WEEK_SNAPSHOT_VERSION", 99)
        assert week_cache.load("461.l.123456", "2026", 7) is None

    def test_a_missing_week_is_none(self):
        assert week_cache.load("461.l.123456", "2026", 9) is None

    def test_stored_weeks_indexes_what_is_on_disk(self):
        first = self.build()
        week_cache.save(first)
        second = self.build()
        second.week = 8
        week_cache.save(second)

        assert week_cache.stored_weeks("461.l.123456", "2026") == [7, 8]
        latest = week_cache.latest("461.l.123456", "2026")
        assert latest is not None
        assert latest.week == 8

    def test_actuals_are_empty_before_the_week_is_played(self):
        snapshot = self.build()
        assert snapshot.has_actuals is False
        snapshot.actuals = {"461.p.1": {"rec": 9.0}}
        assert snapshot.has_actuals is True

    def test_lookups_by_team(self):
        snapshot = self.build()
        assert len(snapshot.roster_for("461.l.123456.t.1")) == 3
        assert snapshot.roster_for("461.l.123456.t.9") == []
        assert snapshot.matchup_for("461.l.123456.t.1") is not None
        assert snapshot.matchup_for("461.l.123456.t.9") is None


class TestWeeklyScript:
    """End-to-end through scripts/weekly.py, on the no-network path.

    Runs it as a subprocess rather than importing it, because argparse, the stdout/stderr
    split, and the exit code are the parts a scheduled task actually depends on.
    """

    SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "weekly.py"

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), "--league", "461.l.123456", *args],
            capture_output=True,
            text=True,
            env={**os.environ, "FF_LEAGUE_KEY": "461.l.123456"},
        )

    def test_reads_a_stored_week_without_touching_the_network(self):
        week_cache.save(build_week_snapshot())
        result = self.run()

        assert result.returncode == 0, result.stderr
        assert "Week 7 lineup -- Team Ryan" in result.stdout
        assert "Ja'Marr Chase" in result.stdout
        assert "Rival Squad" in result.stdout

    def test_reports_the_bye_rather_than_hiding_it(self):
        week_cache.save(build_week_snapshot())
        result = self.run()
        assert "BYE" in result.stdout

    def test_json_goes_to_stdout_alone_so_it_can_be_piped(self):
        week_cache.save(build_week_snapshot())
        result = self.run("--json")

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == 1
        assert payload["league"]["week"] == 7
        assert payload["my_team"]["name"] == "Team Ryan"
        # Human output must not contaminate the pipe.
        assert "Week 7 lineup" in result.stderr

    def test_every_roster_number_is_present_in_the_json(self):
        week_cache.save(build_week_snapshot())
        payload = json.loads(self.run("--json").stdout)

        roster = {entry["player_key"]: entry for entry in payload["my_team"]["roster"]}
        chase = roster["461.p.1"]
        assert chase["projected"] == pytest.approx(16.89, abs=0.01)
        assert chase["adjustments"] == []
        # The renderer must never have to derive a figure the engine did not state.
        assert roster["461.p.2"]["is_bye"] is True
        assert roster["461.p.2"]["projected"] == 0.0

    def test_a_specific_week_can_be_asked_for(self):
        week_cache.save(build_week_snapshot(week=7))
        week_cache.save(build_week_snapshot(week=8))
        payload = json.loads(self.run("--json", "--week", "8").stdout)
        assert payload["league"]["week"] == 8

    def test_a_missing_week_fails_loudly(self):
        result = self.run("--week", "3")
        assert result.returncode == 1
        assert "No stored week" in result.stdout

    def test_trusting_source_points_is_opt_in_and_warns(self):
        week_cache.save(build_week_snapshot())
        without = self.run()
        with_trust = self.run("--trust-source-points", "K,DEF")

        assert "no scoreable stat line" in without.stdout
        assert "WARNING" in with_trust.stdout
        assert "exporter's scoring" in with_trust.stdout

    def test_the_lineup_and_gameday_sections_reach_the_json(self):
        week_cache.save(build_week_snapshot())
        payload = json.loads(self.run("--json").stdout)

        lineup = payload["lineup"]
        assert lineup["current"]["total"] == pytest.approx(16.89, abs=0.01)
        assert lineup["optimal"]["total"] == pytest.approx(25.6, abs=0.1)
        assert lineup["points_left_on_bench"] == pytest.approx(8.7, abs=0.1)
        assert [c["player_in_name"] for c in lineup["changes"]] == ["Rome Odunze"]

        at_risk = {entry["name"]: entry for entry in payload["inactive_check"]["at_risk"]}
        assert at_risk["Kenneth Walker III"]["reason"] == "bye"
        assert at_risk["Kenneth Walker III"]["is_certain"] is True
        assert at_risk["Rome Odunze"]["reason"] == "questionable"

    def test_the_two_sections_never_offer_the_same_player_twice(self):
        """Following the lineup advice and the gameday advice must not start one player
        in two slots -- the gameday check runs against the recommended lineup."""
        week_cache.save(build_week_snapshot())
        payload = json.loads(self.run("--json").stdout)

        starting = {change["player_in"] for change in payload["lineup"]["changes"]}
        offered = {
            entry["replacement"]
            for entry in payload["inactive_check"]["at_risk"]
            if entry["replacement"]
        }
        assert starting & offered == set()

    def test_sections_can_be_selected(self):
        week_cache.save(build_week_snapshot())
        payload = json.loads(self.run("--json", "--sections", "lineup").stdout)
        assert "lineup" in payload
        assert "inactive_check" not in payload

    def test_an_unknown_section_is_refused(self):
        week_cache.save(build_week_snapshot())
        result = self.run("--sections", "playoffs")
        assert result.returncode != 0
        assert "Unknown section" in result.stderr + result.stdout
