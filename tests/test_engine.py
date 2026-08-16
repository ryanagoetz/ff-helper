"""Tests for the valuation and recommendation math."""

from __future__ import annotations

import pytest

from ff_helper.engine import replacement
from ff_helper.engine.scoring import score_stats, scoring_slug
from ff_helper.engine.vona import (
    depth_multiplier,
    expected_best_available,
    recommend,
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


class TestDepthMultiplier:
    def test_full_value_until_starters_are_filled(self):
        assert depth_multiplier(0, 1) == 1.0
        assert depth_multiplier(1, 2) == 1.0

    def test_discounts_surplus_players(self):
        assert depth_multiplier(1, 1) < 1.0
        assert depth_multiplier(2, 1) < depth_multiplier(1, 1)

    def test_third_qb_in_a_one_qb_league_is_near_worthless(self):
        assert depth_multiplier(3, 1) < 0.2


class TestRecommend:
    def test_prefers_the_scarce_position_over_higher_raw_value(self):
        """The central claim of the app.

        The WR is worth more in isolation, but four comparable WRs will survive to the
        next pick while the RB tier falls off a cliff. A cheat sheet takes the WR; this
        should take the RB.
        """
        levels = replacement.ReplacementLevels(
            points={"RB": 100.0, "WR": 100.0}, starters_drafted={"RB": 30, "WR": 36}
        )
        available = [
            player("Elite WR", "WR", 180, adp=13, stdev=4),
            # A deep, flat WR corps going *after* the next pick, so waiting costs little.
            # (Giving these ADPs before the horizon would mean they are gone too, which
            # tests nothing about depth.)
            *[player(f"WR{i}", "WR", 176 - i, adp=27 + i * 2, stdev=5) for i in range(5)],
            # The RB is slightly worse but the next RB is a chasm below, and the drop
            # happens before the next pick comes back around.
            player("Last good RB", "RB", 170, adp=14, stdev=4),
            *[player(f"RB{i}", "RB", 118 - i * 3, adp=30 + i * 4, stdev=6) for i in range(5)],
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
