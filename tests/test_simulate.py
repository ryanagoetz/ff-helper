"""Tests for the Monte Carlo market simulator.

Everything here is seeded: a flaky probability test is worse than no test. Assertions
are ordinal or invariant-shaped wherever possible -- "less than independence predicts",
"exactly the opponent pick budget" -- so they survive retuning of the sampler.
"""

from __future__ import annotations

import pytest

from ff_helper.engine.replacement import ReplacementLevels
from ff_helper.engine.simulate import SimulationConfig, simulate_market
from ff_helper.engine.vona import (
    expected_best_available,
    recommend,
    survival_normalizers,
    survival_probability,
)
from ff_helper.rankings.blend import PlayerValuation, estimate_adp_stdev
from ff_helper.yahoo.models import LeagueSettings, RosterSlot


def settings(*, qb=1, rb=2, wr=2, flex=1, bench=6) -> LeagueSettings:
    slots = [
        RosterSlot("QB", qb),
        RosterSlot("RB", rb),
        RosterSlot("WR", wr),
        RosterSlot("W/R/T", flex),
        RosterSlot("BN", bench),
    ]
    return LeagueSettings(roster_slots=tuple(slots), stat_modifiers={}, is_auction=False)


def player(name, position, points, adp, *, stdev=None, tier=None) -> PlayerValuation:
    return PlayerValuation(
        player_key=f"p.{name}",
        name=name,
        position=position,
        team="FA",
        projected_points=points,
        adp=adp,
        adp_stdev=stdev if stdev is not None else estimate_adp_stdev(adp),
        tier=tier,
    )


def levels_for(pool: list[PlayerValuation]) -> ReplacementLevels:
    # Fixed baselines keep VOR arithmetic legible in assertions.
    return ReplacementLevels(points={"QB": 100.0, "RB": 100.0, "WR": 100.0}, starters_drafted={})


def default_pool() -> list[PlayerValuation]:
    """A coherent little world: enough players at every ADP to absorb every sim pick."""
    pool = []
    for index in range(30):
        pool.append(player(f"rb{index}", "RB", 200 - index * 3, 1 + index * 2, stdev=6))
        pool.append(player(f"wr{index}", "WR", 195 - index * 3, 2 + index * 2, stdev=6))
    return pool


def run(pool, *, current=1, my_picks=(6,), targets=(6,), pick_owner=None, rosters=None,
        config=None, league=None):
    return simulate_market(
        pool,
        levels_for(pool),
        league if league is not None else settings(),
        current_pick=current,
        my_picks=list(my_picks),
        targets=list(targets),
        pick_owner=pick_owner or {},
        team_rosters=rosters or {},
        config=config or SimulationConfig(rollouts=200, seed=11),
    )


class TestDeterminism:
    def test_identical_seed_identical_result(self):
        pool = default_pool()
        first = run(pool, config=SimulationConfig(rollouts=80, seed=7))
        second = run(pool, config=SimulationConfig(rollouts=80, seed=7))
        assert first.removed_counts == second.removed_counts
        assert first.survival_counts == second.survival_counts
        for rank in (1, 2, 3):
            assert first.expected_at("RB", rank, 6) == second.expected_at("RB", rank, 6)

    def test_seed_defaults_to_the_board_so_polls_do_not_flicker(self):
        pool = default_pool()
        first = run(pool, config=SimulationConfig(rollouts=80))
        second = run(pool, config=SimulationConfig(rollouts=80))
        assert first.survival_counts == second.survival_counts


class TestInvariants:
    def test_each_rollout_removes_exactly_the_opponent_picks(self):
        # Picks 10..19 with mine at 10 and 15: 4 opponents to 15, 8 to 20. Exactly --
        # this is the budget the analytic normalizer can only hit in expectation.
        result = run(
            default_pool(),
            current=10,
            my_picks=(10, 15, 20),
            targets=(15, 20),
            config=SimulationConfig(rollouts=60, seed=3),
        )
        assert set(result.removed_counts[15]) == {4}
        assert set(result.removed_counts[20]) == {8}

    def test_targets_beyond_the_window_are_not_covered(self):
        result = run(
            default_pool(),
            targets=(6, 200),
            config=SimulationConfig(rollouts=40, seed=5, window=60),
        )
        assert result.targets == (6,)
        assert result.expected_at("RB", 1, 200) is None
        assert result.survival("p.rb0", 200) is None

    def test_expected_best_decays_with_target(self):
        result = run(
            default_pool(),
            my_picks=(6, 16),
            targets=(6, 16),
            config=SimulationConfig(rollouts=200, seed=9),
        )
        assert result.expected_at("RB", 1, 6) > result.expected_at("RB", 1, 16)

    def test_exclusion_lowers_expected_best(self):
        pool = default_pool()
        result = run(pool, config=SimulationConfig(rollouts=200, seed=13))
        best_rb = max(pool, key=lambda v: v.projected_points if v.position == "RB" else 0)
        with_him = result.expected_at("RB", 1, 6)
        without_him = result.expected_at("RB", 1, 6, exclude=best_rb.player_key)
        assert without_him < with_him


class TestOpponentBehavior:
    def test_competition_makes_joint_removal_less_likely_than_independence(self):
        # Three equal tier-1 RBs, three opponent picks, everyone hungry for RBs. All
        # three going requires every pick to land on them; independent survival math
        # overstates that, because it lets removals overlap on the same player.
        pool = [
            player("stud1", "RB", 190, 2.0, stdev=3, tier=1),
            player("stud2", "RB", 189, 2.0, stdev=3, tier=1),
            player("stud3", "RB", 188, 2.0, stdev=3, tier=1),
            *[player(f"wr{i}", "WR", 170 - i, 3 + i, stdev=3, tier=2) for i in range(10)],
            *[player(f"rb{i}", "RB", 150 - i, 8 + i, stdev=4, tier=2) for i in range(10)],
        ]
        result = run(
            pool,
            current=1,
            my_picks=(4,),
            targets=(4,),
            config=SimulationConfig(rollouts=2000, seed=17),
        )
        survivals = [result.survival(f"p.stud{i}", 4) for i in (1, 2, 3)]
        independence_all_gone = 1.0
        for survival in survivals:
            independence_all_gone *= 1.0 - survival
        joint_all_gone = result.tier_gone[("RB", 1, 4)]
        assert joint_all_gone < independence_all_gone

    def test_a_team_with_the_position_filled_rarely_takes_it(self):
        # One juicy QB on the board, one opponent pick. A team whose QB slot is full
        # should mostly leave him; a team that needs one should mostly take him.
        pool = [
            player("qb1", "QB", 200, 1.0, stdev=2),
            *[player(f"wr{i}", "WR", 180 - i, 1.5 + i, stdev=2) for i in range(8)],
        ]
        league = settings(qb=1, rb=0, wr=2, flex=1)

        def survival_with(roster):
            result = simulate_market(
                pool,
                levels_for(pool),
                league,
                current_pick=1,
                my_picks=[2],
                targets=[2],
                pick_owner={1: "t.rival"},
                team_rosters={"t.rival": roster},
                config=SimulationConfig(rollouts=400, seed=21),
            )
            return result.survival("p.qb1", 2)

        assert survival_with({"QB": 1}) > survival_with({})

    def test_deterministic_adp_reproduces_the_analytic_ordering(self):
        # Tight sigmas and well-separated ADPs: the room drafts nearly in ADP order, so
        # with five opponent picks the expected best RB at pick 6 is about the sixth.
        pool = [player(f"rb{i}", "RB", 200 - i * 10, i + 1, stdev=0.5) for i in range(12)]
        result = run(
            pool,
            current=1,
            my_picks=(6,),
            targets=(6,),
            config=SimulationConfig(rollouts=400, seed=23),
        )
        assert result.survival("p.rb0", 6) < 0.1
        assert result.survival("p.rb9", 6) > 0.9
        sixth_vor = 200 - 5 * 10 - 100
        assert result.expected_at("RB", 1, 6) == pytest.approx(sixth_vor, abs=5)
        # And the *normalized* analytic model agrees about the same world, within
        # noise. (Unnormalized it is biased low here -- independence over-removes --
        # which is precisely the bias both the normalizer and the simulator fix.)
        ordered = sorted(pool, key=lambda v: -v.projected_points)
        survivals = [
            survival_probability(v, current_pick=1, target_pick=6) for v in pool
        ]
        beta = survival_normalizers({6: survivals}, current_pick=1, my_picks=[6])[6]
        analytic = expected_best_available(
            ordered, levels_for(pool), current_pick=1, target_pick=6, normalizer=beta
        )
        assert result.expected_at("RB", 1, 6) == pytest.approx(analytic, abs=6)


class TestTailNotes:
    def test_tail_note_reports_a_real_wipeout_risk_and_stays_quiet_otherwise(self):
        pool = [
            player("stud1", "RB", 190, 2.0, stdev=3, tier=1),
            player("stud2", "RB", 189, 2.0, stdev=3, tier=1),
            *[player(f"wr{i}", "WR", 170 - i, 3 + i, stdev=3, tier=1) for i in range(10)],
            *[player(f"rb{i}", "RB", 150 - i, 8 + i, stdev=4, tier=2) for i in range(10)],
        ]
        result = run(
            pool,
            current=1,
            my_picks=(4,),
            targets=(4,),
            config=SimulationConfig(rollouts=1000, seed=29),
        )
        rb_risk = result.tier_gone[("RB", 1, 4)]
        note = result.tail_note("RB")
        if 0.15 <= rb_risk < 0.95:
            assert "tier-1 RBs" in note and "pick 4" in note
        else:
            assert note == ""
        # Ten tier-1 WRs cannot all vanish in three picks; no note, no noise.
        assert result.tail_note("WR") == ""


class TestRecommendIntegration:
    def test_recommend_uses_the_market_when_it_covers_the_question(self):
        pool = default_pool()
        market = run(pool, config=SimulationConfig(rollouts=100, seed=31))
        recommendations = recommend(
            pool,
            levels_for(pool),
            settings(),
            {},
            current_pick=1,
            next_pick=6,
            future_picks=[6],
            my_picks=[1, 6],
            market=market,
        )
        # Displayed survival comes from counting rollouts, so it must be an exact
        # multiple of 1/rollouts -- the analytic model would virtually never land there.
        for rec in recommendations:
            assert rec.survival_to_next == pytest.approx(
                round(rec.survival_to_next * 100) / 100, abs=1e-9
            )
            expected = market.survival(rec.valuation.player_key, 6)
            assert rec.survival_to_next == expected

    def test_recommend_without_market_is_unchanged(self):
        pool = default_pool()
        baseline = recommend(
            pool, levels_for(pool), settings(), {},
            current_pick=1, next_pick=6, future_picks=[6], my_picks=[1, 6],
        )
        explicit_none = recommend(
            pool, levels_for(pool), settings(), {},
            current_pick=1, next_pick=6, future_picks=[6], my_picks=[1, 6], market=None,
        )
        assert [r.valuation.player_key for r in baseline] == [
            r.valuation.player_key for r in explicit_none
        ]
        assert [r.score for r in baseline] == [r.score for r in explicit_none]


class TestAssistantWiring:
    def test_build_reads_the_env_switch(self, monkeypatch):
        from ff_helper.assistant import Assistant
        from ff_helper.draft.state import DraftState
        from tests.helpers import build_league, build_snapshot, build_teams

        monkeypatch.setenv("FF_MC_ROLLOUTS", "25")
        league = build_league()
        state = DraftState(league=league, teams=build_teams(), rounds=15)
        assistant = Assistant.build(league, state, build_snapshot())
        assert assistant.mc_rollouts == 25

        # And the simulated path produces a full short list without incident.
        recommendations = assistant.snake_recommendations(limit=5)
        assert len(recommendations) == 5
