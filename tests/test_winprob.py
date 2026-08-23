"""Score distributions, matchup win probability, and the variance-aware lineup."""

from __future__ import annotations

import pytest

from ff_helper.engine import winprob
from ff_helper.engine.weekly import (
    WinProbConfig,
    opponent_lineup,
    recommend_for_win,
)
from ff_helper.season.valuation import WeeklyValuation
from ff_helper.yahoo.models import LeagueSettings, RosterEntry, RosterSlot

WEEK = 7


def valuation(
    key: str,
    position: str,
    points: float,
    *,
    name: str = "",
    team: str = "AAA",
    opponent: str = "BBB",
    stdev: float = 0.0,
    availability: float = 1.0,
    is_bye: bool = False,
) -> WeeklyValuation:
    return WeeklyValuation(
        player_key=key,
        name=name or key.upper(),
        position=position,
        team=team,
        week=WEEK,
        projected_points=points,
        eligible_positions=(position,),
        points_stdev=stdev,
        opponent=opponent,
        availability=0.0 if is_bye else availability,
        is_bye=is_bye,
    )


def settings_for(slots: list[tuple[str, int]]) -> LeagueSettings:
    return LeagueSettings(
        roster_slots=tuple(RosterSlot(position, count) for position, count in slots),
        stat_modifiers={},
        is_auction=False,
    )


def team(prefix: str, *specs: tuple[str, float], team_abbr: str, opponent: str):
    return [
        valuation(f"{prefix}{index}", position, points, team=team_abbr, opponent=opponent)
        for index, (position, points) in enumerate(specs)
    ]


class TestPlayerSpread:
    def test_outcome_variance_dominates_source_disagreement(self):
        """A single source agreeing with itself is not evidence a player is predictable."""
        certain = valuation("a", "WR", 12.0, stdev=0.0)
        assert winprob.player_sigma(certain) > 0.0

    def test_the_two_kinds_of_spread_add_in_quadrature(self):
        quiet = valuation("a", "WR", 12.0, stdev=0.0)
        argued = valuation("b", "WR", 12.0, stdev=4.0)
        expected = (winprob.player_sigma(quiet) ** 2 + 16.0) ** 0.5
        assert winprob.player_sigma(argued) == pytest.approx(expected, abs=0.01)

    def test_positions_have_different_spreads(self):
        quarterback = valuation("a", "QB", 20.0)
        defense = valuation("b", "DEF", 20.0)
        assert winprob.player_sigma(defense) > winprob.player_sigma(quarterback)

    def test_the_mean_is_undone_before_being_re_applied(self):
        """A questionable player's mean must not be discounted twice.

        ``projected_points`` already carries the availability multiplier. Sampling from
        it *and* zero-inflating the draw would charge for the absence at both ends.
        """
        healthy = valuation("a", "WR", 10.0)
        hurt = valuation("b", "WR", 8.0, availability=0.8)
        # 8.0 / 0.8 == 10.0, so the conditional means match and so do the spreads.
        assert winprob.player_sigma(hurt) == pytest.approx(winprob.player_sigma(healthy))

    def test_a_bye_has_no_spread(self):
        assert winprob.player_sigma(valuation("a", "WR", 0.0, is_bye=True)) == 0.0


class TestFittedCv:
    def test_falls_back_to_the_table_when_nothing_is_fitted(self):
        assert winprob.fitted_cv()["WR"] == pytest.approx(0.58)

    def test_a_fitted_file_overrides(self, tmp_path, monkeypatch):
        from ff_helper import config

        monkeypatch.setenv("FF_HELPER_HOME", str(tmp_path))
        (config.cache_dir() / "weekly-cv.json").write_text('{"WR": 0.42}')
        table = winprob.fitted_cv()
        assert table["WR"] == pytest.approx(0.42)
        assert table["QB"] == pytest.approx(0.36)  # untouched positions keep the default

    def test_an_absurd_fitted_value_is_ignored(self, tmp_path, monkeypatch):
        from ff_helper import config

        monkeypatch.setenv("FF_HELPER_HOME", str(tmp_path))
        (config.cache_dir() / "weekly-cv.json").write_text('{"WR": 99.0}')
        # A CV of 99 is a fitting bug, not a discovery about receivers.
        assert winprob.fitted_cv()["WR"] == pytest.approx(0.58)

    def test_a_corrupt_file_does_not_break_the_digest(self, tmp_path, monkeypatch):
        from ff_helper import config

        monkeypatch.setenv("FF_HELPER_HOME", str(tmp_path))
        (config.cache_dir() / "weekly-cv.json").write_text("{not json")
        assert winprob.fitted_cv()["WR"] == pytest.approx(0.58)


class TestMatchupSimulation:
    def test_identical_teams_are_a_coin_flip(self):
        mine = team("m", ("QB", 20.0), ("RB", 14.0), ("WR", 13.0), team_abbr="AAA", opponent="BBB")
        theirs = team(
            "t", ("QB", 20.0), ("RB", 14.0), ("WR", 13.0), team_abbr="CCC", opponent="DDD"
        )
        outcome = winprob.simulate_matchup(mine, theirs, rollouts=8000)
        assert outcome.win_probability == pytest.approx(0.5, abs=0.03)

    def test_the_mean_matches_the_projection(self):
        mine = team("m", ("QB", 20.0), ("RB", 14.0), ("WR", 13.0), team_abbr="AAA", opponent="BBB")
        outcome = winprob.simulate_matchup(mine, [], rollouts=8000)
        assert outcome.mine.mean == pytest.approx(47.0, rel=0.03)

    def test_a_heavy_favourite_usually_wins(self):
        strong = team(
            "m", ("QB", 26.0), ("RB", 20.0), ("WR", 19.0), team_abbr="AAA", opponent="BBB"
        )
        weak = team("t", ("QB", 11.0), ("RB", 7.0), ("WR", 6.0), team_abbr="CCC", opponent="DDD")
        outcome = winprob.simulate_matchup(strong, weak, rollouts=8000)
        assert outcome.win_probability > 0.9
        assert outcome.is_favoured

    def test_the_same_matchup_gives_the_same_number_every_time(self):
        mine = team("m", ("QB", 20.0), ("WR", 13.0), team_abbr="AAA", opponent="BBB")
        theirs = team("t", ("QB", 18.0), ("WR", 15.0), team_abbr="CCC", opponent="DDD")
        first = winprob.simulate_matchup(mine, theirs, rollouts=4000).win_probability
        second = winprob.simulate_matchup(mine, theirs, rollouts=4000).win_probability
        # A win probability that drifts between two runs of the same digest is one
        # nobody can act on.
        assert round(first, 6) == round(second, 6)

    def test_the_seed_is_derived_from_who_is_playing(self):
        mine = team("m", ("QB", 20.0), team_abbr="AAA", opponent="BBB")
        theirs = team("t", ("QB", 20.0), team_abbr="CCC", opponent="DDD")
        other = team("t", ("QB", 20.1), team_abbr="CCC", opponent="DDD")
        a = winprob.simulate_matchup(mine, theirs, rollouts=2000)
        b = winprob.simulate_matchup(mine, other, rollouts=2000)
        assert a.mine.mean == pytest.approx(b.mine.mean, abs=0.01)

    def test_a_ruled_out_player_contributes_nothing(self):
        with_him = team("m", ("QB", 20.0), ("WR", 14.0), team_abbr="AAA", opponent="BBB")
        without = [with_him[0], valuation("m1", "WR", 0.0, availability=0.0)]
        assert winprob.simulate_matchup(without, [], rollouts=4000).mine.mean == pytest.approx(
            winprob.simulate_matchup([with_him[0]], [], rollouts=4000).mine.mean, rel=0.05
        )

    def test_a_doubtful_player_is_a_coin_flip_on_zero_not_a_small_score(self):
        """Zero-inflation, not mean-shrinking: the shape of the risk is different."""
        doubtful = [valuation("a", "WR", 4.0, availability=0.35)]
        outcome = winprob.simulate_matchup(doubtful, [], rollouts=8000)
        # Most weeks he scores nothing at all, which a shrunken mean would hide.
        assert outcome.mine.p_above(0.01) == pytest.approx(0.35, abs=0.04)
        assert outcome.mine.mean == pytest.approx(4.0, rel=0.1)

    def test_players_in_the_same_game_move_together(self):
        """Correlation is what stops a lineup's variance being understated."""
        same_game = [
            valuation("a", "WR", 12.0, team="AAA", opponent="BBB"),
            valuation("b", "WR", 12.0, team="BBB", opponent="AAA"),
        ]
        apart = [
            valuation("a", "WR", 12.0, team="AAA", opponent="BBB"),
            valuation("b", "WR", 12.0, team="CCC", opponent="DDD"),
        ]
        together_sd = winprob.simulate_matchup(same_game, [], rollouts=10000).mine.sd
        apart_sd = winprob.simulate_matchup(apart, [], rollouts=10000).mine.sd
        assert together_sd > apart_sd

    def test_a_quarterback_and_his_receivers_are_correlated_more_than_the_game(self):
        stack = [
            valuation("qb", "QB", 20.0, team="AAA", opponent="BBB"),
            valuation("wr", "WR", 12.0, team="AAA", opponent="BBB"),
        ]
        opposed = [
            valuation("qb", "QB", 20.0, team="AAA", opponent="BBB"),
            valuation("wr", "WR", 12.0, team="BBB", opponent="AAA"),
        ]
        assert (
            winprob.simulate_matchup(stack, [], rollouts=10000).mine.sd
            > winprob.simulate_matchup(opposed, [], rollouts=10000).mine.sd
        )

    def test_a_running_back_does_not_ride_his_own_passing_offense(self):
        assert "RB" not in winprob._PASS_GAME

    def test_an_empty_lineup_is_a_certain_zero(self):
        outcome = winprob.simulate_matchup([], [], rollouts=100)
        assert outcome.mine.mean == 0.0
        assert outcome.mine.sd == 0.0


class TestScoreDistribution:
    def distribution(self):
        mine = team("m", ("QB", 20.0), ("RB", 14.0), ("WR", 13.0), team_abbr="AAA", opponent="BBB")
        return winprob.team_distribution(mine, rollouts=8000)

    def test_the_floor_is_below_the_ceiling(self):
        dist = self.distribution()
        assert dist.floor < dist.mean < dist.ceiling

    def test_p_above_is_monotonic(self):
        dist = self.distribution()
        assert dist.p_above(dist.floor) > dist.p_above(dist.ceiling)

    def test_p_above_the_median_is_about_half(self):
        dist = self.distribution()
        assert dist.p_above(dist.quantile(0.5)) == pytest.approx(0.5, abs=0.02)


class TestVarianceAwareLineup:
    SETTINGS = settings_for([("QB", 1), ("WR", 2), ("BN", 3)])

    def board(self, safe_points: float, boom_points: float):
        """A steady receiver and a volatile one, with the volatile one projected lower.

        Volatility is expressed through source disagreement, which is the only lever the
        valuation exposes -- the position CV is the same for both.
        """
        values = {
            "qb": valuation("qb", "QB", 20.0, name="The QB"),
            "wr1": valuation("wr1", "WR", 15.0, name="Steady One"),
            "safe": valuation("safe", "WR", safe_points, name="Safe Floor", stdev=0.5),
            "boom": valuation("boom", "WR", boom_points, name="Boom Bust", stdev=14.0),
        }
        entries = [
            RosterEntry("qb", "t1", WEEK, "QB"),
            RosterEntry("wr1", "t1", WEEK, "WR"),
            RosterEntry("safe", "t1", WEEK, "WR"),
            RosterEntry("boom", "t1", WEEK, "BN"),
        ]
        return values, entries

    def opponent(self, points: float):
        return [
            valuation("o1", "QB", points * 0.4, team="ZZZ", opponent="YYY"),
            valuation("o2", "WR", points * 0.3, team="ZZZ", opponent="YYY"),
            valuation("o3", "WR", points * 0.3, team="ZZZ", opponent="YYY"),
        ]

    def test_a_heavy_underdog_takes_the_volatile_lineup(self):
        values, entries = self.board(safe_points=12.0, boom_points=11.0)
        plan = recommend_for_win(
            values, entries, self.SETTINGS, self.opponent(120.0)
        )

        assert plan.differs, "a big underdog should be willing to trade points for variance"
        assert plan.recommended.total < plan.max_ev.total
        assert plan.recommended_outcome.mine.sd > plan.max_ev_outcome.mine.sd
        assert plan.win_probability_gain > 0
        assert "underdog" in plan.reason

    def test_a_heavy_favourite_keeps_the_steady_lineup(self):
        values, entries = self.board(safe_points=12.0, boom_points=13.0)
        plan = recommend_for_win(values, entries, self.SETTINGS, self.opponent(20.0))

        # Ahead by miles, the job is to avoid a disaster, so the boom-bust upgrade that
        # max-EV would take is either declined or does not raise the odds enough to report.
        assert plan.recommended_outcome.mine.sd <= plan.max_ev_outcome.mine.sd + 1e-9

    def test_an_even_matchup_leaves_the_projected_best_lineup_alone(self):
        values, entries = self.board(safe_points=12.0, boom_points=11.0)
        plan = recommend_for_win(values, entries, self.SETTINGS, self.opponent(47.0))
        assert not plan.differs
        assert plan.swaps == ()

    def test_a_tiny_modelled_gain_never_triggers_a_swap(self):
        values, entries = self.board(safe_points=12.0, boom_points=11.99)
        plan = recommend_for_win(
            values,
            entries,
            self.SETTINGS,
            self.opponent(50.0),
            config=WinProbConfig(edge=0.5),  # nothing can clear a 50-point edge
        )
        assert not plan.differs

    def test_both_lineups_are_always_reported(self):
        values, entries = self.board(safe_points=12.0, boom_points=11.0)
        plan = recommend_for_win(values, entries, self.SETTINGS, self.opponent(120.0))
        # Reporting only the recommendation hides what it costs, and the trade is the
        # entire content of the advice.
        assert plan.max_ev_outcome.win_probability >= 0.0
        assert plan.recommended_outcome.win_probability >= 0.0
        assert plan.points_given_up >= 0.0

    def test_the_result_is_deterministic(self):
        values, entries = self.board(safe_points=12.0, boom_points=11.0)
        first = recommend_for_win(values, entries, self.SETTINGS, self.opponent(120.0))
        second = recommend_for_win(values, entries, self.SETTINGS, self.opponent(120.0))
        assert first.recommended.starter_keys == second.recommended.starter_keys
        assert round(first.recommended_outcome.win_probability, 6) == round(
            second.recommended_outcome.win_probability, 6
        )

    def test_a_locked_starter_is_never_swapped_out(self):
        values, entries = self.board(safe_points=12.0, boom_points=11.0)
        plan = recommend_for_win(
            values,
            entries,
            self.SETTINGS,
            self.opponent(120.0),
            locked={2: "safe"},  # his game has kicked off
        )
        assert "safe" in plan.recommended.starter_keys

    def test_a_bye_player_is_never_swapped_in(self):
        values = {
            "wr1": valuation("wr1", "WR", 10.0),
            "bye": valuation("bye", "WR", 0.0, is_bye=True),
        }
        entries = [
            RosterEntry("wr1", "t1", WEEK, "WR"),
            RosterEntry("bye", "t1", WEEK, "BN"),
        ]
        plan = recommend_for_win(
            values, entries, settings_for([("WR", 1), ("BN", 1)]), self.opponent(200.0)
        )
        assert "bye" not in plan.recommended.starter_keys


class TestOpponentLineup:
    def test_the_opponent_is_assumed_to_start_their_best(self):
        values = {
            "a": valuation("a", "WR", 18.0),
            "b": valuation("b", "WR", 5.0),
        }
        # They have the better player benched. We assume they will notice.
        entries = [
            RosterEntry("a", "t2", WEEK, "BN"),
            RosterEntry("b", "t2", WEEK, "WR"),
        ]
        starters = opponent_lineup(values, entries, settings_for([("WR", 1), ("BN", 1)]))
        assert [v.player_key for v in starters] == ["a"]

    def test_their_stashed_players_are_not_counted(self):
        values = {
            "a": valuation("a", "WR", 5.0),
            "stash": valuation("stash", "WR", 30.0),
        }
        entries = [
            RosterEntry("a", "t2", WEEK, "WR"),
            RosterEntry("stash", "t2", WEEK, "IR"),
        ]
        starters = opponent_lineup(
            values, entries, settings_for([("WR", 1), ("BN", 1), ("IR", 1)])
        )
        assert [v.player_key for v in starters] == ["a"]

    def test_an_unvalued_opponent_roster_produces_nothing(self):
        entries = [RosterEntry("mystery", "t2", WEEK, "WR")]
        assert opponent_lineup({}, entries, settings_for([("WR", 1)])) == []
