"""Backtest harness tests: records round-trip, calibration scores, counterfactuals.

The draft being backtested here is synthetic but complete: twelve teams draft a full
roster by noisy ADP with lineup awareness, seeded so every run sees the same draft. In
this world ADP *is* the generating process, so the survival model ought to calibrate
well -- which is exactly what makes it a fixture: a Brier score drifting toward 0.25
here means the model broke, not that the world got weird.
"""

from __future__ import annotations

import random

import pytest

from ff_helper.assistant import Assistant
from ff_helper.backtest.calibration import survival_calibration, turn_reports
from ff_helper.backtest.capture import (
    DraftRecord,
    build_state,
    load_record,
    record_from_live,
    save_record,
)
from ff_helper.backtest.counterfactual import POLICIES, counterfactual
from ff_helper.draft.state import DraftState
from ff_helper.engine import lineup
from ff_helper.yahoo.models import DraftPick
from tests.helpers import NUM_TEAMS, build_league, build_snapshot, build_teams

SEED = 42


def synthesize_record(seed: int = SEED) -> DraftRecord:
    """A full, legal, seeded draft: every team picks by noisy ADP, preferring players
    that fill an open starting slot (which is how real rooms end up with kickers)."""
    snapshot = build_snapshot()
    league = build_league()
    teams = build_teams()
    state = DraftState(league=league, teams=teams)
    assistant = Assistant.build(league, state, snapshot)
    settings = league.settings
    assert settings is not None

    rng = random.Random(seed)
    valuations = list(assistant.valuations.valuations.values())
    counts: dict[str, dict[str, int]] = {team.team_key: {} for team in teams}
    drafted: set[str] = set()
    picks: list[DraftPick] = []

    for number in range(1, state.total_picks + 1):
        team = state.team_for_pick(number)
        assert team is not None
        candidates = sorted(
            (v for v in valuations if v.player_key not in drafted),
            key=lambda v: v.adp + rng.gauss(0.0, 4.0),
        )
        open_dedicated, open_flex, _ = lineup.assign_lineup(counts[team.team_key], settings)

        def fills_open_slot(position: str, dedicated=open_dedicated, flex=open_flex) -> bool:
            if dedicated.get(position, 0) > 0:
                return True
            return any(position in eligible and count > 0 for eligible, count in flex)

        chosen = next((v for v in candidates if fills_open_slot(v.position)), candidates[0])
        drafted.add(chosen.player_key)
        team_counts = counts[team.team_key]
        team_counts[chosen.position] = team_counts.get(chosen.position, 0) + 1
        picks.append(
            DraftPick(
                pick=number,
                round=(number - 1) // NUM_TEAMS + 1,
                team_key=team.team_key,
                player_key=chosen.player_key,
            )
        )

    return record_from_live(league, teams, picks)


@pytest.fixture(scope="module")
def world():
    return synthesize_record(), build_snapshot()


def fresh_assistant(record: DraftRecord) -> Assistant:
    league, state = build_state(record)
    return Assistant.build(league, state, build_snapshot())


class TestCapture:
    def test_round_trip_preserves_everything(self, world, tmp_path):
        record, _ = world
        path = save_record(record, tmp_path / "draft.json", anonymize=False)
        loaded = load_record(path)
        assert loaded.league == record.league  # includes settings, int stat keys
        assert loaded.teams == record.teams
        assert loaded.picks == record.picks
        assert loaded.version == record.version

    def test_anonymize_scrubs_names_but_not_structure(self, world, tmp_path):
        record, _ = world
        loaded = load_record(save_record(record, tmp_path / "anon.json"))
        assert all(team.name.startswith("Team ") for team in loaded.teams)
        assert loaded.my_team is not None
        assert loaded.my_team.team_key == record.my_team.team_key
        assert loaded.picks == record.picks

    def test_record_requires_settings(self, world):
        record, _ = world
        from dataclasses import replace

        bare_league = replace(record.league, settings=None)
        with pytest.raises(ValueError):
            record_from_live(bare_league, list(record.teams), list(record.picks))


class TestCalibration:
    def test_brier_beats_coin_flip_in_an_adp_world(self, world):
        record, _ = world
        report = survival_calibration(fresh_assistant(record), list(record.picks))
        assert report.n > 500
        assert 0.0 < report.brier < 0.25

    def test_reliability_bins_trend_upward(self, world):
        # Players predicted likely-to-survive should survive more often than players
        # predicted likely-to-be-gone. Directional, not exact: this is the property
        # that makes the reliability table readable at all.
        record, _ = world
        report = survival_calibration(fresh_assistant(record), list(record.picks))
        assert len(report.bins) >= 3
        first_predicted, first_observed, _ = report.bins[0]
        last_predicted, last_observed, _ = report.bins[-1]
        assert first_predicted < last_predicted
        assert first_observed < last_observed

    def test_my_own_removals_are_not_scored(self, world):
        record, _ = world
        report = survival_calibration(fresh_assistant(record), list(record.picks))
        my_key = record.my_team.team_key
        my_picked_at = {
            pick.player_key: pick.pick for pick in record.picks if pick.team_key == my_key
        }
        for sample in report.samples:
            taken_at = my_picked_at.get(sample.player_key)
            if taken_at is not None:
                # Never sampled in the very window where I removed him myself.
                assert not (sample.window[0] <= taken_at < sample.window[1]) or (
                    taken_at != sample.window[0]
                )


class TestTurnReports:
    def test_one_report_per_my_turn(self, world):
        record, _ = world
        my_key = record.my_team.team_key
        my_turn_count = sum(1 for pick in record.picks if pick.team_key == my_key)
        reports = turn_reports(fresh_assistant(record), list(record.picks), limit=3)
        assert len(reports) == my_turn_count
        assert all(report.recommendations for report in reports)
        assert all(report.elapsed >= 0.0 for report in reports)

    def test_match_rank_agrees_with_recommendations(self, world):
        record, _ = world
        for report in turn_reports(fresh_assistant(record), list(record.picks), limit=5):
            keys = [rec.valuation.player_key for rec in report.recommendations]
            if report.match_rank is not None:
                assert keys[report.match_rank - 1] == report.actual_key
            else:
                assert report.actual_key not in keys


@pytest.fixture(scope="module")
def results(world):
    record, snapshot = world
    return {policy: counterfactual(record, snapshot, policy=policy) for policy in POLICIES}


class TestCounterfactual:
    def test_every_policy_fills_the_roster(self, results, world):
        record, _ = world
        roster_size = record.league.settings.roster_size
        for result in results.values():
            assert len(result.players) == roster_size

    def test_actual_roster_is_legal(self, results):
        positions = [position for _, _, position in results["actual"].players]
        for position, needed in (("QB", 1), ("RB", 2), ("WR", 2), ("TE", 1), ("K", 1), ("DEF", 1)):
            assert positions.count(position) >= needed

    def test_engine_covers_the_skill_starters(self, results):
        # The engine never spends a pick on a zero-VOR kicker or defense -- that is a
        # human's end-of-draft chore -- but every skill slot must be covered.
        positions = [position for _, _, position in results["engine"].players]
        for position, needed in (("QB", 1), ("RB", 2), ("WR", 2), ("TE", 1)):
            assert positions.count(position) >= needed

    def test_engine_beats_the_noisy_adp_drafter(self, results):
        # On the decision-grade metric: the starting lineup the roster can field.
        assert results["engine"].lineup_points >= results["actual"].lineup_points
        assert results["engine"].total_vor >= results["actual"].total_vor

    def test_engine_lineup_beats_raw_vor_greed(self, results):
        # best_vor hoards the highest-VOR players regardless of lineup slots; the raw
        # roster sum rewards that, the startable lineup does not. The engine plans
        # around slots, so it must win the metric that decides games.
        assert results["engine"].lineup_points >= results["best_vor"].lineup_points

    def test_best_vor_is_an_upper_bound_on_greed(self, results):
        # best_vor ignores scarcity entirely; it should still land a high-VOR roster in
        # a world with no injuries or busts. Sanity floor, not a claim of optimality.
        assert results["best_vor"].total_vor > 0

    def test_unknown_policy_rejected(self, world):
        record, snapshot = world
        with pytest.raises(ValueError):
            counterfactual(record, snapshot, policy="yolo")
