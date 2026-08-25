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
from ff_helper.backtest.counterfactual import (
    AUCTION_POLICIES,
    POLICIES,
    auction_counterfactual,
    counterfactual,
)
from ff_helper.backtest.nominations import format_report, nomination_report
from ff_helper.draft.state import DraftState
from ff_helper.engine import auction, lineup
from ff_helper.yahoo.models import DraftPick
from tests.helpers import NUM_TEAMS, build_league, build_snapshot, build_teams

SEED = 42

# A descending price schedule for one team's fifteen buys. Sums to 196 of a 200 budget,
# so a replay that overspends by even a few dollars shows up as an illegal roster rather
# than passing quietly.
AUCTION_PRICES = (60, 40, 25, 20, 15, 10, 8, 5, 4, 3, 2, 1, 1, 1, 1)


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


def synthesize_auction_record(seed: int = SEED) -> DraftRecord:
    """The same seeded draft, re-read as an auction with a price on every sale.

    Reusing the snake draft keeps the rosters legal and the fixture cheap; only the
    settings flag and the costs differ, which is all the auction path reads.
    """
    import dataclasses

    record = synthesize_record(seed)
    settings = dataclasses.replace(record.league.settings, is_auction=True)
    league = dataclasses.replace(record.league, settings=settings)

    spend: dict[str, int] = {}
    priced: list[DraftPick] = []
    for pick in sorted(record.picks):
        nth = spend.get(pick.team_key, 0)
        cost = AUCTION_PRICES[min(nth, len(AUCTION_PRICES) - 1)]
        spend[pick.team_key] = nth + 1
        priced.append(dataclasses.replace(pick, cost=cost))
    return record_from_live(league, list(record.teams), priced)


@pytest.fixture(scope="module")
def world():
    return synthesize_record(), build_snapshot()


@pytest.fixture(scope="module")
def auction_world():
    return synthesize_auction_record(), build_snapshot()


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

    def test_keepers_round_trip_and_shape_the_board(self, world, tmp_path):
        # A keeper league without its keepers round-trips into a keeper-free board:
        # wrong pick counts, invented rounds, and kept studs sitting "available" all
        # draft -- observed as impossible survivals on a real keeper-league record.
        from ff_helper.yahoo.models import KeptPlayer

        record, _ = world
        team = record.teams[0]
        kept = KeptPlayer(
            player_key=record.picks[0].player_key, team_key=team.team_key, round=3
        )
        from dataclasses import replace

        keeper_record = replace(record, keepers=(kept,), picks=record.picks[1:])
        loaded = load_record(save_record(keeper_record, tmp_path / "keeper.json"))
        assert loaded.keepers == (kept,)

        _, state = build_state(loaded)
        assert kept.player_key in state.drafted_player_keys
        assert len(state.keepers) == 1

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

    def test_mc_predictor_is_also_calibrated_in_an_adp_world(self, world):
        # The Monte Carlo predictor scored on the same record, through the same
        # harness -- the A/B this Predictor parameter exists for. Rollouts kept small:
        # this asserts sanity, not the analytic-vs-mc verdict (backtest.py does that).
        from ff_helper.backtest.calibration import mc_predictor

        record, _ = world
        assistant = fresh_assistant(record)
        assistant.mc_rollouts = 40
        report = survival_calibration(
            assistant, list(record.picks), predictor=mc_predictor
        )
        assert report.n > 500
        assert 0.0 < report.brier < 0.25

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


@pytest.fixture(scope="module")
def auction_results(auction_world):
    record, snapshot = auction_world
    return {
        policy: auction_counterfactual(record, snapshot, policy=policy)
        for policy in AUCTION_POLICIES
    }


class TestAuctionCounterfactual:
    def test_every_policy_leaves_with_a_full_roster(self, auction_results, auction_world):
        # The point of the endgame fill: a policy must not be allowed to "win" by
        # passing on everything and scoring its empty slots as zero.
        record, _ = auction_world
        roster_size = record.league.settings.roster_size
        for policy, result in auction_results.items():
            assert len(result.players) == roster_size, policy

    def test_no_policy_outspends_the_budget(self, auction_results, auction_world):
        record, _ = auction_world
        budget = record.league.settings.auction_budget
        for policy, result in auction_results.items():
            assert result.spent <= budget, policy

    def test_actual_reproduces_the_recorded_roster(self, auction_results, auction_world):
        # The control: if replaying history does not give history back, every other
        # policy's delta is measured against the wrong baseline.
        record, _ = auction_world
        mine = record.my_team.team_key
        recorded = sorted(pick.pick for pick in record.picks if pick.team_key == mine)
        replayed = sorted(pick_number for pick_number, _, _ in auction_results["actual"].players)
        assert replayed == recorded
        assert auction_results["actual"].spent == sum(
            pick.cost or 0 for pick in record.picks if pick.team_key == mine
        )

    def test_snake_record_is_refused(self, world):
        record, snapshot = world
        with pytest.raises(ValueError, match="auction"):
            auction_counterfactual(record, snapshot, policy="actual")

    def test_unknown_policy_rejected(self, auction_world):
        record, snapshot = auction_world
        with pytest.raises(ValueError):
            auction_counterfactual(record, snapshot, policy="yolo")

    def test_follow_from_replays_history_verbatim_before_the_cutoff(self, auction_world):
        # Everything before the hand-over must match `actual` exactly, or the "given the
        # hole I had already dug" question is being asked about a different hole.
        record, snapshot = auction_world
        mine = record.my_team.team_key
        cutoff = max(pick.pick for pick in record.picks) // 2
        result = auction_counterfactual(record, snapshot, policy="engine", follow_from=cutoff)

        early = {pick.pick for pick in record.picks if pick.team_key == mine and pick.pick < cutoff}
        held = {pick_number for pick_number, _, _ in result.players}
        assert early <= held, "a pre-cutoff buy of mine went missing"
        assert len(result.players) == record.league.settings.roster_size

    def test_stop_after_drops_my_post_cutoff_buys_from_every_policy(self, auction_world):
        """The contract: past the cut nobody bids for me -- ``actual`` included.

        Regression on the way that failed. A declined buy of mine looks for a rival who
        could have outbid me, and when none can the fallback used to be "leave him with the
        recorded buyer" -- but in that branch the recorded buyer IS me, at ``cost=None``,
        which ``DraftState.spent`` reads as $0. So the flag whose whole job is to exclude
        the post-cut stretch handed those exact players back, free. It bit hardest late in a
        draft, when every rival is full, which is the stretch ``stop_after`` exists to cut.
        """
        record, snapshot = auction_world
        mine = record.my_team.team_key
        last = max(pick.pick for pick in record.picks)
        # Several cutoffs, including one early enough that declining almost everything
        # fills the rivals up -- that is the state where `_deepest_pocket` finds nobody and
        # the buggy fallback fired. A single mid-draft cutoff never reaches it.
        for cutoff in (1, last // 4, last // 3, last // 2):
            recorded_late = {
                pick.pick for pick in record.picks if pick.team_key == mine and pick.pick > cutoff
            }
            assert recorded_late, f"cutoff {cutoff} leaves no post-cutoff buys of mine"

            for policy in AUCTION_POLICIES:
                result = auction_counterfactual(
                    record, snapshot, policy=policy, stop_after=cutoff
                )
                kept = {pick for pick, _, _ in result.players} & recorded_late
                assert not kept, f"{policy} kept post-cutoff buys {sorted(kept)} at {cutoff}"
                # The two invariants the other policies get, which `actual` now needs too.
                assert len(result.players) == record.league.settings.roster_size, policy
                assert result.spent <= record.league.settings.auction_budget, policy

    def test_stop_after_fills_actual_like_everyone_else(self, auction_world):
        """``actual`` must not keep its real endgame while the others get a greedy one.

        That asymmetry would make the table measure the cut rather than the advice.
        """
        record, snapshot = auction_world
        last = max(pick.pick for pick in record.picks)
        cut = auction_counterfactual(record, snapshot, policy="actual", stop_after=last // 3)
        whole = auction_counterfactual(record, snapshot, policy="actual")
        assert {p for p, _, _ in cut.players} != {p for p, _, _ in whole.players}
        assert len(cut.players) == len(whole.players)

    def test_a_cutoff_before_the_handover_is_refused(self, auction_world):
        """Both flags together leave the policy no picks, so all four rows come back equal.

        A comparison that measures nothing must not print as though it measured something.
        """
        record, snapshot = auction_world
        with pytest.raises(ValueError, match="follow_from"):
            auction_counterfactual(
                record, snapshot, policy="engine", follow_from=80, stop_after=20
            )

    def test_follow_from_none_lets_the_policy_choose_from_the_first_sale(self, auction_world):
        # The complement: with no hand-over the policy owns every decision, so its roster
        # must be free to diverge from mine. If these matched, follow_from would be inert.
        record, snapshot = auction_world
        free = auction_counterfactual(record, snapshot, policy="best_par")
        actual = auction_counterfactual(record, snapshot, policy="actual")
        assert {p for p, _, _ in free.players} != {p for p, _, _ in actual.players}


class TestAuctionRankKey:
    def test_zero_bid_sorts_below_a_buyable_player(self):
        # The defect this key exists to prevent: a high-scoring player the plan prices
        # at $0 crowding out one you can actually buy.
        buyable = _fake_recommendation(bid_to=9, affordable=True, score=1.0)
        unbuyable = _fake_recommendation(bid_to=0, affordable=True, score=99.0)
        assert sorted([unbuyable, buyable], key=auction.rank_key) == [buyable, unbuyable]

    def test_score_still_orders_players_you_can_buy(self):
        low = _fake_recommendation(bid_to=5, affordable=True, score=1.0)
        high = _fake_recommendation(bid_to=5, affordable=True, score=50.0)
        assert sorted([low, high], key=auction.rank_key) == [high, low]


def _fake_recommendation(*, bid_to: int, affordable: bool, score: float):
    class _R:
        pass

    r = _R()
    r.bid_to = bid_to
    r.affordable = affordable
    r.score = score
    return r


@pytest.fixture(scope="module")
def report(auction_world):
    return nomination_report(*auction_world, limit=3)


class TestNominationRetrospective:
    """The nomination model's predictions, graded against a record.

    Not a counterfactual and it must never grow into one -- the record has no nominator
    field and ``auction_counterfactual`` freezes prices, so no nomination order has a
    modelled consequence. What is testable is that the grading is *sound*: that it changes
    no sale, that its baselines are the right shape, and that it refuses a snake record
    rather than printing something meaningless.
    """

    def test_it_grades_something(self, report):
        assert report.named > 0
        assert report.drains + report.bargains == report.named
        assert report.buyer_named.total > 0

    def test_the_named_set_is_a_subset_of_the_position_only_set(self, report):
        """``live_bidders`` adds a money constraint, so it can only tighten.

        If recall ever exceeds the position-only baseline the two are measuring different
        populations and the comparison in the report is meaningless.
        """
        assert report.buyer_named.rate <= report.baseline_any_open.rate
        assert report.named_size <= report.open_size

    def test_a_smaller_set_is_what_the_money_gate_buys(self, report):
        assert report.named_size > 0
        assert report.baseline_deepest.total == report.buyer_named.total

    def test_grading_changes_no_sale(self, auction_world):
        """The replay is a bystander: it must leave the recorded draft untouched."""
        record, snapshot = auction_world
        before = [(p.pick, p.player_key, p.team_key, p.cost) for p in sorted(record.picks)]
        nomination_report(record, snapshot, limit=3)
        after = [(p.pick, p.player_key, p.team_key, p.cost) for p in sorted(record.picks)]
        assert before == after

    def test_a_snake_record_is_refused(self, world):
        record, snapshot = world
        with pytest.raises(ValueError, match="auction"):
            nomination_report(record, snapshot, limit=3)

    def test_the_report_says_what_it_cannot_say(self, report):
        """The honesty paragraph is printed, not merely documented."""
        text = format_report(report, limit=3)
        assert "never as a counterfactual" in text
        assert "no nominator field" in text
        assert "NOT a fit for _STUCK_DECAY" in text
