"""Tests for the valuation and recommendation math."""

from __future__ import annotations

import pytest

from ff_helper.engine import replacement
from ff_helper.engine.room import (
    PickObservation,
    observations_from_board,
    room_tendencies,
)
from ff_helper.engine.scoring import score_stats, scoring_slug
from ff_helper.engine.vona import (
    depth_multiplier,
    expected_best_available,
    extrapolated_picks,
    penalized,
    recommend,
    survival_normalizers,
    survival_probability,
    survival_probability_at,
)
from ff_helper.rankings.blend import PlayerValuation, estimate_adp_stdev
from ff_helper.yahoo.models import (
    STAT_REC,
    STAT_REC_TD,
    STAT_REC_YDS,
    STAT_RUSH_TD,
    STAT_RUSH_YDS,
    LeagueSettings,
    RosterSlot,
)

PPR = {
    STAT_RUSH_YDS: 0.1,
    STAT_RUSH_TD: 6.0,
    STAT_REC: 1.0,
    STAT_REC_YDS: 0.1,
    STAT_REC_TD: 6.0,
}
STANDARD = {**PPR, STAT_REC: 0.0}


def settings(modifiers=None, *, flex=1, bench=6) -> LeagueSettings:
    slots = [
        RosterSlot("QB", 1),
        RosterSlot("RB", 2),
        RosterSlot("WR", 2),
        RosterSlot("TE", 1),
        RosterSlot("W/R/T", flex),
        RosterSlot("BN", bench),
    ]
    return LeagueSettings(
        roster_slots=tuple(slots),
        stat_modifiers=modifiers if modifiers is not None else PPR,
        is_auction=False,
    )


def player(name, position, points, adp, *, stdev=None, tier=None, status="") -> PlayerValuation:
    return PlayerValuation(
        player_key=f"p.{name}",
        name=name,
        position=position,
        team="FA",
        projected_points=points,
        adp=adp,
        adp_stdev=stdev if stdev is not None else estimate_adp_stdev(adp),
        tier=tier,
        status=status,
    )


class TestScoring:
    def test_ppr_rewards_receptions(self):
        stats = {"rec": 100.0, "rec_yds": 1200.0, "rec_td": 8.0}
        assert score_stats(stats, PPR) == pytest.approx(100 + 120 + 48)

    def test_standard_scoring_drops_reception_points(self):
        stats = {"rec": 100.0, "rec_yds": 1200.0, "rec_td": 8.0}
        assert score_stats(stats, STANDARD) == pytest.approx(120 + 48)

    def test_returns_none_when_nothing_scoreable(self):
        # None, not 0.0 -- "no projection" must stay distinguishable from "projected zero".
        assert score_stats({}, PPR) is None
        assert score_stats({"unknown_stat": 5.0}, PPR) is None

    def test_scoring_slug_matches_league_ppr(self):
        assert scoring_slug(settings(PPR)) == "ppr"
        assert scoring_slug(settings(STANDARD)) == "standard"
        assert scoring_slug(settings({**PPR, STAT_REC: 0.5})) == "half-ppr"


class TestReplacement:
    def _pool(self):
        pool = []
        for index in range(40):
            pool.append(player(f"rb{index}", "RB", 250 - index * 5, index + 1))
            pool.append(player(f"wr{index}", "WR", 240 - index * 4, index + 1))
        for index in range(20):
            pool.append(player(f"qb{index}", "QB", 300 - index * 8, index + 20))
            pool.append(player(f"te{index}", "TE", 200 - index * 9, index + 30))
        return pool

    def test_starter_counts_scale_with_league_size(self):
        levels = replacement.compute(self._pool(), settings(), num_teams=12)
        # 12 teams x 1 QB, and no flex is QB-eligible in a W/R/T league.
        assert levels.starters_drafted["QB"] == 12

    def test_flex_slots_are_allocated_by_value_not_by_assumption(self):
        levels = replacement.compute(self._pool(), settings(flex=1), num_teams=12)
        # 12 dedicated RB2 slots x2 = 24, 24 WR, 12 TE, plus 12 flex spread among them.
        flex_total = (
            levels.starters_drafted["RB"]
            + levels.starters_drafted["WR"]
            + levels.starters_drafted["TE"]
        ) - (24 + 24 + 12)
        assert flex_total == 12

        # In this pool RBs and WRs outscore TEs at the margin, so TE should win no flex.
        assert levels.starters_drafted["TE"] == 12

    def test_replacement_level_is_first_player_past_the_starters(self):
        levels = replacement.compute(self._pool(), settings(), num_teams=12)
        assert levels.points["QB"] == pytest.approx(300 - 12 * 8)

    def test_vor_is_relative_to_position(self):
        pool = self._pool()
        levels = replacement.compute(pool, settings(), num_teams=12)
        top_qb = next(p for p in pool if p.name == "qb0")
        # A QB's raw 300 points is worth only its margin over the 13th-best QB.
        assert levels.vor(top_qb) == pytest.approx(96)
        assert levels.vor(top_qb) < top_qb.projected_points


class TestSurvival:
    def test_probability_falls_as_the_horizon_extends(self):
        target = player("x", "RB", 200, adp=20, stdev=6)
        near = survival_probability(target, current_pick=10, target_pick=15)
        far = survival_probability(target, current_pick=10, target_pick=40)
        assert near > far
        assert 0.0 <= far <= near <= 1.0

    def test_same_pick_is_certain(self):
        target = player("x", "RB", 200, adp=20, stdev=6)
        assert survival_probability(target, current_pick=10, target_pick=10) == 1.0

    def test_conditioning_on_current_availability(self):
        # A player with ADP 10 who is somehow still there at pick 30 is a faller. The
        # unconditional odds of him lasting to 36 are ~1e-7; conditioned, they are real.
        faller = player("faller", "WR", 200, adp=10, stdev=5)
        conditioned = survival_probability(faller, current_pick=30, target_pick=36)
        unconditioned = survival_probability_at(36, adp=10, sigma=5)
        assert conditioned > 0.10
        assert conditioned > unconditioned * 1000

    def test_deep_faller_does_not_explode(self):
        # Guards the division in the conditioning step.
        faller = player("faller", "WR", 200, adp=5, stdev=1.0)
        probability = survival_probability(faller, current_pick=120, target_pick=140)
        assert 0.0 <= probability <= 1.0

    def test_estimated_stdev_grows_with_adp(self):
        assert estimate_adp_stdev(1) < estimate_adp_stdev(50) < estimate_adp_stdev(150)
        # Early picks are near-deterministic but never zero-variance.
        assert estimate_adp_stdev(1) >= 1.5

    def test_low_demand_raises_survival(self):
        # ADP is measured across rooms where everyone needs everything. If none of the
        # intervening teams needs a QB, this QB's hazard shrinks -- but never to zero,
        # because teams draft bench and best-player-available too.
        target = player("x", "QB", 200, adp=20, stdev=6)
        base = survival_probability(target, current_pick=10, target_pick=20)
        quiet = survival_probability(target, current_pick=10, target_pick=20, demand=0.0)
        assert quiet > base
        assert quiet < 1.0

    def test_full_demand_is_the_plain_adp_model(self):
        target = player("x", "QB", 200, adp=20, stdev=6)
        base = survival_probability(target, current_pick=10, target_pick=20)
        assert survival_probability(target, current_pick=10, target_pick=20, demand=1.0) == base


class TestExpectedBestAvailable:
    def test_more_depth_means_higher_expected_survivor(self):
        levels = replacement.ReplacementLevels(points={"WR": 100.0}, starters_drafted={"WR": 24})
        thin = [player("w1", "WR", 160, adp=12, stdev=3)]
        deep = [player(f"w{i}", "WR", 160 - i, adp=12 + i * 2, stdev=3) for i in range(6)]

        current, target = 10, 25
        thin_value = expected_best_available(thin, levels, current_pick=current, target_pick=target)
        deep_value = expected_best_available(deep, levels, current_pick=current, target_pick=target)
        assert deep_value > thin_value

    def test_empty_pool_is_replacement_level(self):
        levels = replacement.ReplacementLevels(points={"WR": 100.0}, starters_drafted={})
        assert expected_best_available([], levels, current_pick=1, target_pick=10) == 0.0

    def test_a_second_slot_cannot_lean_on_the_same_fallback(self):
        # Two starting slots at one position: the second is priced at the expected
        # second-best survivor, not the best one counted twice.
        levels = replacement.ReplacementLevels(points={"WR": 100.0}, starters_drafted={"WR": 24})
        pool = [
            player("w1", "WR", 160, adp=40, stdev=4),
            player("w2", "WR", 120, adp=42, stdev=4),
        ]
        best = expected_best_available(pool, levels, current_pick=10, target_pick=20)
        second = expected_best_available(pool, levels, current_pick=10, target_pick=20, rank=2)
        assert second < best
        # Both all but certainly survive, so rank 2 lands on the second player's VOR.
        assert second == pytest.approx(20, abs=3)


class TestRoomTendencies:
    """The snake analog of the auction room premiums: learned drift from ADP."""

    def test_empty_board_reports_neutral(self):
        tendencies = room_tendencies([])
        assert tendencies.overall == 0.0
        assert tendencies.shift("RB") == 0.0

    def test_uniformly_slow_room_shifts_positive_but_shrunk(self):
        # Thirty picks each 8 later than ADP: a real tendency, but the prior keeps the
        # estimate below the raw mean and the clamp keeps it sane.
        observations = [PickObservation("WR", 8.0) for _ in range(30)]
        tendencies = room_tendencies(observations)
        assert 0.0 < tendencies.overall < 8.0
        assert tendencies.overall <= 10.0

    def test_positional_reach_is_shrunk_toward_the_room(self):
        # Two quarterbacks taken 20 picks early among thirty neutral picks: the QB
        # shift goes negative, but two picks are an anecdote, not a rewrite.
        observations = [PickObservation("RB", 0.0) for _ in range(30)] + [
            PickObservation("QB", -20.0),
            PickObservation("QB", -20.0),
        ]
        tendencies = room_tendencies(observations)
        assert tendencies.shift("QB") < tendencies.overall
        assert tendencies.shift("QB") > -20.0
        # Positions with no picks fall back to the room-wide tendency.
        assert tendencies.shift("TE") == tendencies.overall

    def test_one_faller_is_not_a_tendency(self):
        # A single 60-pick slide contributes at most the observation clamp.
        lone = room_tendencies([PickObservation("WR", 60.0)])
        capped = room_tendencies([PickObservation("WR", 25.0)])
        assert lone.overall == capped.overall

    def test_positive_shift_raises_survival(self):
        target = player("Target", "RB", 150, adp=20, stdev=5)
        base = survival_probability(target, current_pick=10, target_pick=20)
        slow_room = survival_probability(
            target, current_pick=10, target_pick=20, adp_shift=6.0
        )
        assert slow_room > base

    def test_observations_skip_players_the_blend_guessed_at(self):
        from dataclasses import replace

        from ff_helper.yahoo.models import DraftPick

        known = player("Known", "RB", 150, adp=10)
        guessed = replace(player("Guessed", "WR", 80, adp=200), adp_estimated=True)
        valuations = {v.player_key: v for v in (known, guessed)}
        picks = [
            DraftPick(pick=12, round=1, team_key="t.1", player_key=known.player_key),
            DraftPick(pick=13, round=2, team_key="t.2", player_key=guessed.player_key),
            DraftPick(pick=14, round=2, team_key="t.3", player_key="unknown.player"),
        ]
        observations = observations_from_board(picks, valuations)
        assert len(observations) == 1
        assert observations[0].position == "RB"
        assert observations[0].deviation == pytest.approx(2.0)


class TestNormalization:
    """The pick-budget constraint: between two of my turns, exactly as many players
    leave the board as there are opponent picks -- no matter what independent hazards
    would prefer."""

    def test_solved_exponent_balances_the_budget(self):
        pool = [player(f"P{i}", "WR", 150 - i, adp=10 + i * 2, stdev=6) for i in range(40)]
        survivals = [
            survival_probability(v, current_pick=20, target_pick=33) for v in pool
        ]
        beta = survival_normalizers({33: survivals}, current_pick=20, my_picks=[20, 33])[33]
        # Window 20..32 is 13 picks; one (20) is mine, so opponents remove 12.
        assert sum(1 - s**beta for s in survivals) == pytest.approx(12, abs=0.05)

    def test_deep_position_is_not_over_drained(self):
        # Thirty near-identical RBs and a four-opponent window: independent hazards
        # would remove far more than four of them, making the expected best-available
        # look bleak and the position look artificially urgent.
        levels = replacement.ReplacementLevels(points={"RB": 50.0}, starters_drafted={"RB": 30})
        pool = [player(f"RB{i}", "RB", 150 - i, adp=18 + i, stdev=8) for i in range(30)]
        survivals = [
            survival_probability(v, current_pick=20, target_pick=25) for v in pool
        ]
        assert sum(1 - s for s in survivals) > 4  # the raw model overdraws
        beta = survival_normalizers({25: survivals}, current_pick=20, my_picks=[20, 25])[25]
        assert beta < 1.0
        raw = expected_best_available(pool, levels, current_pick=20, target_pick=25)
        normalized = expected_best_available(
            pool, levels, current_pick=20, target_pick=25, normalizer=beta
        )
        assert normalized > raw

    def test_quiet_demand_pushes_hazard_onto_other_positions(self):
        # The old model could only ever shrink hazards: flooring QB demand made
        # quarterbacks safer without making anyone else less safe, which is impossible
        # when the pick count is fixed. Through the normalizer the displaced removals
        # land on the receivers.
        wrs = [player(f"WR{i}", "WR", 150 - i, adp=15 + i * 2, stdev=6) for i in range(15)]
        qbs = [player(f"QB{i}", "QB", 250 - i, adp=16 + i * 2, stdev=6) for i in range(15)]

        def beta_when_qb_demand_is(demand: float) -> float:
            survivals = [
                survival_probability(v, current_pick=14, target_pick=26) for v in wrs
            ] + [
                survival_probability(v, current_pick=14, target_pick=26, demand=demand)
                for v in qbs
            ]
            return survival_normalizers({26: survivals}, current_pick=14, my_picks=[14, 26])[26]

        neutral = beta_when_qb_demand_is(1.0)
        quiet = beta_when_qb_demand_is(0.0)
        assert quiet > neutral
        wr_survival = survival_probability(wrs[0], current_pick=14, target_pick=26)
        assert wr_survival**quiet < wr_survival**neutral

    def test_my_own_picks_are_not_opponent_removals(self):
        pool = [player(f"P{i}", "WR", 150 - i, adp=10 + i * 2, stdev=6) for i in range(40)]
        survivals = [
            survival_probability(v, current_pick=20, target_pick=33) for v in pool
        ]
        two_of_mine = survival_normalizers(
            {33: survivals}, current_pick=20, my_picks=[20, 26, 33]
        )[33]
        one_of_mine = survival_normalizers({33: survivals}, current_pick=20, my_picks=[20, 33])[33]
        # An extra pick of mine inside the window means one fewer opponent removal, so
        # the exponent eases and everyone's survival rises.
        assert two_of_mine < one_of_mine

    def test_default_normalizer_changes_nothing(self):
        target = player("Target", "RB", 150, adp=20, stdev=5)
        assert survival_probability(
            target, current_pick=10, target_pick=20, normalizer=1.0
        ) == survival_probability(target, current_pick=10, target_pick=20)


class TestDepthMultiplier:
    def test_full_value_until_starters_are_filled(self):
        assert depth_multiplier(0, 1) == 1.0
        assert depth_multiplier(1, 2) == 1.0

    def test_discounts_surplus_players(self):
        assert depth_multiplier(1, 1) < 1.0
        assert depth_multiplier(2, 1) < depth_multiplier(1, 1)

    def test_third_qb_in_a_one_qb_league_is_near_worthless(self):
        assert depth_multiplier(3, 1) < 0.2


class TestPenalized:
    def test_scales_positive_scores_down(self):
        assert penalized(20.0, 0.5) == 10.0

    def test_pushes_negative_scores_further_down(self):
        # A bare multiply would give -10, *promoting* the penalised player.
        assert penalized(-20.0, 0.5) == -40.0
        assert penalized(-20.0, 0.5) < -20.0


class TestRecommend:
    def test_prefers_the_scarce_position_over_higher_raw_value(self):
        """The central claim of the app.

        The WR is worth more in isolation, but four comparable WRs will survive to the
        next pick while the RB tier falls off a cliff. A cheat sheet takes the WR; this
        should take the RB.
        """
        levels = replacement.ReplacementLevels(
            points={"RB": 100.0, "WR": 100.0, "QB": 130.0},
            starters_drafted={"RB": 30, "WR": 36, "QB": 12},
        )
        available = [
            player("Elite WR", "WR", 175, adp=13, stdev=4),
            # A deep, flat WR corps going well *after* the next pick, so waiting costs
            # little -- at pick 25 and at the picks after it. (If the tier died between
            # my turns, grabbing elite WRs early would genuinely be right: with two WR
            # slots to fill, a tier that allows only one more WR pick rewards stacking.)
            *[player(f"WR{i}", "WR", 176 - i, adp=40 + i * 2, stdev=5) for i in range(5)],
            # The RB is slightly worse but the next RB is a chasm below, and the drop
            # happens before the next pick comes back around.
            player("Last good RB", "RB", 170, adp=14, stdev=4),
            *[player(f"RB{i}", "RB", 118 - i * 3, adp=30 + i * 4, stdev=6) for i in range(5)],
            # A whole board's worth of replacement-level depth behind them. The pick
            # budget normalizer takes the pool literally -- every intervening pick
            # removes someone -- so a twelve-player "world" facing ninety removals
            # (the plan looks eight of my picks ahead) would rightly lose everyone.
            # These depth players are the rest of that world.
            *[player(f"Depth QB{i}", "QB", 130, adp=12 + i * 1.3, stdev=5) for i in range(90)],
        ]

        picks = recommend(
            available,
            levels,
            settings(),
            roster_counts={},
            current_pick=12,
            next_pick=25,
        )
        assert picks[0].name == "Last good RB"
        assert picks[0].vona > picks[1].vona

    def test_respects_roster_needs(self):
        levels = replacement.ReplacementLevels(
            points={"RB": 100.0, "QB": 100.0}, starters_drafted={"RB": 30, "QB": 12}
        )
        available = [
            player("Good QB", "QB", 160, adp=20, stdev=5),
            player("Good RB", "RB", 155, adp=21, stdev=5),
        ]

        # With two quarterbacks already rostered in a 1-QB league, a third is dead weight
        # even though he grades higher.
        picks = recommend(
            available,
            levels,
            settings(),
            roster_counts={"QB": 2},
            current_pick=20,
            next_pick=33,
        )
        assert picks[0].name == "Good RB"

    def test_injury_status_is_penalised_not_ignored(self):
        levels = replacement.ReplacementLevels(points={"RB": 100.0}, starters_drafted={"RB": 30})
        healthy = player("Healthy", "RB", 150, adp=20, stdev=5)
        hurt = player("Hurt", "RB", 158, adp=20, stdev=5, status="IR")

        picks = recommend([healthy, hurt], levels, settings(), {}, current_pick=20, next_pick=33)
        assert picks[0].name == "Healthy"

    def test_a_filled_position_cannot_promote_a_weak_player(self):
        """A backup must rank below the identical player who still fills a starting slot.

        The twins are gone by the next pick either way (take now or never), so the plan
        term is the same for both and the ranking isolates the depth penalty: holding
        four running backs must push the RB twin *below* the otherwise-identical WR twin.
        """
        levels = replacement.ReplacementLevels(
            points={"RB": 100.0, "WR": 100.0}, starters_drafted={"RB": 30, "WR": 36}
        )
        available = [
            player("Stud RB", "RB", 200, adp=12, stdev=4),
            player("Twin RB", "RB", 130, adp=13, stdev=4),
            player("Stud WR", "WR", 200, adp=12, stdev=4),
            player("Twin WR", "WR", 130, adp=13, stdev=4),
        ]
        picks = recommend(
            available,
            levels,
            settings(),
            roster_counts={"RB": 4},
            current_pick=10,
            next_pick=25,
            limit=10,
        )
        names = [pick.name for pick in picks]
        assert names.index("Twin WR") < names.index("Twin RB")

    def test_a_player_who_will_survive_is_deferred(self):
        """The plan does not reach for a faller, however well he grades.

        The lone TE is the best VOR on the board, but he will still be there at my next
        several picks, while the last real RB is gone imminently. A raw-value board leads
        with the TE; the plan takes the RB and collects the TE at 25.
        """
        levels = replacement.ReplacementLevels(
            points={"RB": 100.0, "TE": 100.0}, starters_drafted={"RB": 30, "TE": 12}
        )
        available = [
            player("Lone TE", "TE", 200, adp=80, stdev=6),
            player("Last RB", "RB", 170, adp=13, stdev=4),
            *[player(f"RB{i}", "RB", 115 - i * 3, adp=30 + i * 4, stdev=6) for i in range(4)],
        ]
        picks = recommend(available, levels, settings(), {}, current_pick=12, next_pick=25)
        assert picks[0].name == "Last RB"

    def test_two_open_slots_reward_stacking_a_dying_tier(self):
        """Multi-slot reasoning the one-pick VONA model could not do.

        Same shape as the scarce-position test but the flat WR tier dies *between* my
        next two picks. With two WR slots open, that tier only has one more WR pick in
        it: elite-WR-now plus flat-WR-at-25 fills both slots at full value, while
        RB-now means the second WR slot is filled from a dead tier. Enumerating the
        strategies with the model's own numbers: WR-now nets ~169, RB-now ~157.
        """
        levels = replacement.ReplacementLevels(
            points={"RB": 100.0, "WR": 100.0}, starters_drafted={"RB": 30, "WR": 36}
        )
        available = [
            player("Elite WR", "WR", 180, adp=13, stdev=4),
            *[player(f"WR{i}", "WR", 176 - i, adp=27 + i * 2, stdev=5) for i in range(5)],
            player("Last good RB", "RB", 170, adp=14, stdev=4),
            *[player(f"RB{i}", "RB", 118 - i * 3, adp=30 + i * 4, stdev=6) for i in range(5)],
        ]
        picks = recommend(available, levels, settings(), {}, current_pick=12, next_pick=25)
        assert picks[0].name == "Elite WR"

    def test_explicit_future_picks_behave_like_the_padded_fallback(self):
        levels = replacement.ReplacementLevels(
            points={"RB": 100.0, "WR": 100.0}, starters_drafted={"RB": 30, "WR": 36}
        )
        available = [
            player("Elite WR", "WR", 180, adp=13, stdev=4),
            *[player(f"WR{i}", "WR", 176 - i, adp=40 + i * 2, stdev=5) for i in range(5)],
            player("Last good RB", "RB", 170, adp=14, stdev=4),
            *[player(f"RB{i}", "RB", 118 - i * 3, adp=30 + i * 4, stdev=6) for i in range(5)],
        ]
        padded = recommend(available, levels, settings(), {}, current_pick=12, next_pick=25)
        explicit = recommend(
            available,
            levels,
            settings(),
            {},
            current_pick=12,
            next_pick=25,
            future_picks=[25, 38, 51, 64, 77, 90, 103, 116],
        )
        assert [pick.name for pick in explicit] == [pick.name for pick in padded]

    def test_injury_cannot_promote_a_negative_score(self):
        # Same shape: the twins' blended score is negative, and halving a negative score
        # used to *raise* the injured one above his healthy double.
        levels = replacement.ReplacementLevels(points={"RB": 100.0}, starters_drafted={"RB": 30})
        available = [
            player("Stud", "RB", 200, adp=60, stdev=4),
            player("Healthy twin", "RB", 130, adp=60, stdev=4),
            player("Hurt twin", "RB", 130, adp=60, stdev=4, status="IR"),
        ]
        picks = recommend(available, levels, settings(), {}, current_pick=10, next_pick=25)
        names = [pick.name for pick in picks]
        assert names.index("Healthy twin") < names.index("Hurt twin")

    def test_flex_routing_prices_each_position_by_its_own_count(self):
        """A W/R/T sent to RB behind zero other RB picks is the *first* RB the plan
        buys, and must be priced at rank 1 -- not at "rank = how many flex slots exist",
        which is what a precomputed label says. Here the only startable RB left survives
        to a later pick; the right play is the receiver now and the runner via flex
        later, which the old fixed-rank plan could not see (it priced the flex-routed
        RB at rank 2 of a one-man pool: zero)."""
        flex_settings = LeagueSettings(
            roster_slots=(
                RosterSlot("QB", 1),
                RosterSlot("RB", 2),
                RosterSlot("WR", 2),
                RosterSlot("TE", 1),
                RosterSlot("W/T", 1),
                RosterSlot("W/R/T", 1),
                RosterSlot("BN", 5),
            ),
            stat_modifiers={},
            is_auction=False,
        )
        levels = replacement.ReplacementLevels(
            points={"RB": 100.0, "WR": 100.0, "TE": 60.0, "QB": 130.0},
            starters_drafted={"RB": 30, "WR": 36, "TE": 12, "QB": 12},
        )
        available = [
            player("Balanced WR", "WR", 160, adp=21, stdev=4),
            player("Stud RB", "RB", 160, adp=70, stdev=8),
            player("Bad RB", "RB", 90, adp=120, stdev=10),
            *[player(f"Deep WR{i}", "WR", 150 - i, adp=72 + i * 3, stdev=8) for i in range(6)],
            player("Decent TE", "TE", 100, adp=75, stdev=8),
            player("Bad TE", "TE", 55, adp=130, stdev=10),
            # The rest of the board, absorbing the room's picks (see the scarce-position
            # test for why the pick budget needs a coherent world).
            *[player(f"Depth QB{i}", "QB", 130, adp=20 + i, stdev=5) for i in range(36)],
        ]
        picks = recommend(
            available,
            levels,
            flex_settings,
            {"QB": 1, "RB": 2, "WR": 1, "TE": 1},
            current_pick=20,
            next_pick=30,
            future_picks=[30, 40, 50],
            my_picks=[20, 30, 40, 50],
        )
        names = [pick.name for pick in picks]
        assert names[0] == "Balanced WR"
        assert names.index("Balanced WR") < names.index("Stud RB")

    def test_extrapolated_picks_mirror_the_snake(self):
        # Slot 3 of 12: real turns are 3, 22, 27, 46, 51, ... -- gaps alternate 19 and 5.
        assert extrapolated_picks(3, 22, 12)[:4] == [22, 27, 46, 51]
        # From the other side of the turn the gaps come in the other order.
        assert extrapolated_picks(22, 27, 12)[:4] == [27, 46, 51, 70]
        # Without the team count there is nothing to mirror; the gap repeats.
        assert extrapolated_picks(3, 22, None)[:3] == [22, 41, 60]

    def test_vona_measures_the_cliff_behind_him_not_his_own_survival(self):
        levels = replacement.ReplacementLevels(points={"RB": 100.0}, starters_drafted={"RB": 30})
        available = [
            player("Stud RB", "RB", 170, adp=60, stdev=8),
            player("Next RB", "RB", 150, adp=62, stdev=8),
            player("Cliff RB", "RB", 110, adp=64, stdev=8),
            *[player(f"Depth RB{i}", "RB", 105 - i, adp=20 + i, stdev=5) for i in range(16)],
        ]
        picks = recommend(available, levels, settings(), {}, current_pick=10, next_pick=25)
        stud = next(pick for pick in picks if pick.name == "Stud RB")
        # He survives to my next pick almost surely; self-inclusive VONA would call the
        # position safe (~0) and hide the 20-point gap to the next man.
        assert stud.survival_to_next > 0.6
        assert stud.vona > 10

    def test_reason_distinguishes_him_surviving_from_comparables_surviving(self):
        levels = replacement.ReplacementLevels(points={"RB": 100.0}, starters_drafted={"RB": 30})
        available = [
            player("Stud RB", "RB", 170, adp=60, stdev=8),
            player("Next RB", "RB", 169.5, adp=62, stdev=8),
            player("Third RB", "RB", 169, adp=64, stdev=8),
            *[player(f"Depth RB{i}", "RB", 105 - i, adp=20 + i, stdev=5) for i in range(16)],
        ]
        picks = recommend(available, levels, settings(), {}, current_pick=10, next_pick=25)
        stud = next(pick for pick in picks if pick.name == "Stud RB")
        # A flat tier where he himself survives: the reason should say *he* will still
        # be there, not that mere comparables will.
        assert stud.vona <= 1
        assert "he should still be there" in stud.reason

    def test_last_pick_falls_back_to_raw_value(self):
        levels = replacement.ReplacementLevels(
            points={"RB": 100.0, "WR": 100.0}, starters_drafted={"RB": 30, "WR": 36}
        )
        available = [
            player("Best left", "WR", 150, adp=200, stdev=20),
            player("Worse", "RB", 120, adp=200, stdev=20),
        ]
        picks = recommend(available, levels, settings(), {}, current_pick=180, next_pick=None)
        assert picks[0].name == "Best left"

    def test_every_recommendation_carries_a_reason(self):
        levels = replacement.ReplacementLevels(points={"RB": 100.0}, starters_drafted={"RB": 30})
        available = [player(f"RB{i}", "RB", 150 - i * 10, adp=10 + i * 5) for i in range(4)]
        picks = recommend(available, levels, settings(), {}, current_pick=10, next_pick=20)
        assert all(pick.reason for pick in picks)
