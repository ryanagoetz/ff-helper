"""The exact lineup optimizer, and the in-season engine built on it."""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from ff_helper.engine.lineup import Lineup, optimal_lineup, starting_slots
from ff_helper.engine.weekly import (
    current_lineup,
    inactive_check,
    start_sit,
)
from ff_helper.season.valuation import WeeklyValuation
from ff_helper.yahoo.models import LeagueSettings, RosterEntry, RosterSlot


@dataclass(frozen=True)
class Player:
    """The minimum that satisfies ``engine.lineup.LineupPlayer``."""

    player_key: str
    eligible_positions: tuple[str, ...]
    projected_points: float


def settings_for(slots: list[tuple[str, int]]) -> LeagueSettings:
    return LeagueSettings(
        roster_slots=tuple(RosterSlot(position, count) for position, count in slots),
        stat_modifiers={},
        is_auction=False,
    )


# The layout of the draft record in data/drafts/2025-shiva-snake.json.
SHIVA_SLOTS = [
    ("QB", 1), ("WR", 2), ("RB", 2), ("TE", 1),
    ("W/R", 1), ("K", 1), ("DEF", 1), ("BN", 6), ("IR", 1),
]
# The synthetic league in tests/helpers.py.
SYNTHETIC_SLOTS = [
    ("QB", 1), ("RB", 2), ("WR", 2), ("TE", 1),
    ("W/R/T", 1), ("K", 1), ("DEF", 1), ("BN", 6),
]
# Two flex slots whose eligibility sets are not nested. This is the layout greedy
# gets wrong, and Yahoo permits it.
SIBLING_FLEX_SLOTS = [("W/R", 1), ("W/T", 1)]


def greedy_lineup_points(roster, settings: LeagueSettings) -> float:
    """The implementation this replaced, kept verbatim as a differential baseline."""
    remaining = sorted(roster, key=lambda v: -v.projected_points)
    total = 0.0
    for slot in settings.starting_slots:
        if len(slot.eligible_positions) != 1:
            continue
        position = next(iter(slot.eligible_positions))
        for _ in range(slot.count):
            chosen = next(
                (v for v in remaining if v.eligible_positions[0] == position), None
            )
            if chosen is not None:
                total += chosen.projected_points
                remaining.remove(chosen)
    for slot in settings.starting_slots:
        if len(slot.eligible_positions) == 1:
            continue
        for _ in range(slot.count):
            chosen = next(
                (v for v in remaining if v.eligible_positions[0] in slot.eligible_positions),
                None,
            )
            if chosen is not None:
                total += chosen.projected_points
                remaining.remove(chosen)
    return total


class TestTheGreedyCounterexample:
    """The bug that motivated making this exact."""

    def test_greedy_leaves_seven_points_on_the_field(self):
        settings = settings_for(SIBLING_FLEX_SLOTS)
        roster = [
            Player("wr", ("WR",), 20.0),
            Player("rb", ("RB",), 12.0),
            Player("te", ("TE",), 5.0),
        ]
        # Greedy: W/R takes the best eligible (WR, 20), W/T is left the TE (5).
        assert greedy_lineup_points(roster, settings) == pytest.approx(25.0)
        # Optimal: W/R takes the RB (12) so W/T can have the WR (20).
        assert optimal_lineup(roster, settings).total == pytest.approx(32.0)

    def test_and_the_assignment_says_why(self):
        settings = settings_for(SIBLING_FLEX_SLOTS)
        roster = [
            Player("wr", ("WR",), 20.0),
            Player("rb", ("RB",), 12.0),
            Player("te", ("TE",), 5.0),
        ]
        board = optimal_lineup(roster, settings)
        placed = {slot.position: key for slot, key in board.starters}
        assert placed == {"W/R": "rb", "W/T": "wr"}
        assert board.bench == ("te",)


class TestDifferentialAgainstGreedy:
    """Optimal must never be worse, and must tie on the layouts we actually play."""

    def rosters(self, seed: int, count: int = 400):
        generator = random.Random(seed)
        shape = ["QB"] * 3 + ["RB"] * 6 + ["WR"] * 7 + ["TE"] * 3 + ["K"] * 2 + ["DEF"] * 2
        for _ in range(count):
            yield [
                Player(f"p{index}", (position,), round(generator.uniform(0.0, 25.0), 1))
                for index, position in enumerate(shape)
            ]

    @pytest.mark.parametrize("slots", [SHIVA_SLOTS, SYNTHETIC_SLOTS])
    def test_ties_exactly_on_nested_layouts(self, slots):
        """Both leagues in this repo have one flex, so the refactor changes no number.

        This is the evidence that replacing greedy with an exact assignment is
        behaviour-preserving where it has ever been used -- the new correctness only
        shows up on layouts neither league has.
        """
        settings = settings_for(slots)
        for roster in self.rosters(seed=11):
            assert optimal_lineup(roster, settings).total == pytest.approx(
                greedy_lineup_points(roster, settings)
            )

    def test_never_worse_on_a_layout_greedy_gets_wrong(self):
        settings = settings_for(
            [("QB", 1), ("WR", 2), ("RB", 2), ("TE", 1), ("W/R", 1), ("W/T", 1), ("BN", 6)]
        )
        better = 0
        for roster in self.rosters(seed=12):
            greedy = greedy_lineup_points(roster, settings)
            exact = optimal_lineup(roster, settings).total
            assert exact >= greedy - 1e-9
            if exact > greedy + 1e-9:
                better += 1
        assert better > 0, "the sibling-flex layout should expose greedy somewhere"


class TestOptimalLineup:
    def test_multi_position_eligibility_is_used(self):
        """A WR/RB can take the RB slot so a better receiver keeps the WR one."""
        settings = settings_for([("RB", 1), ("WR", 1)])
        roster = [
            Player("swing", ("WR", "RB"), 10.0),
            Player("wr", ("WR",), 14.0),
        ]
        board = optimal_lineup(roster, settings)
        placed = {slot.position: key for slot, key in board.starters}
        assert placed == {"RB": "swing", "WR": "wr"}
        assert board.total == pytest.approx(24.0)

    def test_an_unfillable_slot_is_reported_not_scored_zero(self):
        settings = settings_for([("QB", 1), ("TE", 1)])
        board = optimal_lineup([Player("qb", ("QB",), 21.0)], settings)

        assert board.total == pytest.approx(21.0)
        assert [slot.position for slot in board.empty] == ["TE"]
        assert len(board.starters) == 1

    def test_a_negative_projection_is_left_out_because_empty_scores_more(self):
        settings = settings_for([("QB", 1)])
        board = optimal_lineup([Player("qb", ("QB",), -2.0)], settings)
        assert board.total == pytest.approx(0.0)
        assert [slot.position for slot in board.empty] == ["QB"]

    def test_bench_is_everyone_not_starting(self):
        settings = settings_for([("WR", 1)])
        roster = [Player("a", ("WR",), 10.0), Player("b", ("WR",), 4.0)]
        board = optimal_lineup(roster, settings)
        assert board.starter_keys == ("a",)
        assert board.bench == ("b",)

    def test_an_empty_roster_leaves_every_slot_empty(self):
        settings = settings_for([("QB", 1), ("WR", 2)])
        board = optimal_lineup([], settings)
        assert board.total == 0.0
        assert len(board.empty) == 3
        assert board.starters == ()

    def test_value_of_lets_a_caller_optimize_something_else(self):
        """The hook the in-season engine reuses to optimize for something else later."""
        settings = settings_for([("WR", 1)])
        roster = [Player("safe", ("WR",), 12.0), Player("boom", ("WR",), 10.0)]

        assert optimal_lineup(roster, settings).starter_keys == ("safe",)
        # A different objective over the same roster must reach a different answer, or
        # the hook is decorative. (Kept positive: a negative objective correctly prefers
        # an empty slot, which is a different behaviour being tested elsewhere.)
        ceiling = optimal_lineup(
            roster, settings, value_of=lambda player: 30.0 - player.projected_points
        )
        assert ceiling.starter_keys == ("boom",)

    def test_slots_expand_one_per_starting_spot(self):
        slots = starting_slots(settings_for([("WR", 2), ("BN", 5)]))
        assert [slot.position for slot in slots] == ["WR", "WR"]
        assert [slot.index for slot in slots] == [0, 1]


class TestLockedSlots:
    def test_a_locked_slot_is_kept_and_the_rest_optimized_around_it(self):
        settings = settings_for([("WR", 2)])
        roster = [
            Player("a", ("WR",), 20.0),
            Player("b", ("WR",), 15.0),
            Player("c", ("WR",), 5.0),
        ]
        # Slot 0 already kicked off with the worst receiver in it.
        board = optimal_lineup(roster, settings, locked={0: "c"})
        placed = {slot.index: key for slot, key in board.starters}
        assert placed == {0: "c", 1: "a"}
        assert board.total == pytest.approx(25.0)

    def test_a_locked_player_cannot_also_be_used_elsewhere(self):
        settings = settings_for([("WR", 2)])
        roster = [Player("a", ("WR",), 20.0), Player("b", ("WR",), 15.0)]
        board = optimal_lineup(roster, settings, locked={0: "a"})
        assert sorted(board.starter_keys) == ["a", "b"]

    def test_locking_an_unrostered_player_refuses(self):
        settings = settings_for([("WR", 1)])
        with pytest.raises(ValueError, match="not in the roster"):
            optimal_lineup([Player("a", ("WR",), 9.0)], settings, locked={0: "ghost"})

    def test_locking_a_slot_that_does_not_exist_refuses(self):
        settings = settings_for([("WR", 1)])
        with pytest.raises(ValueError, match="not a slot"):
            optimal_lineup([Player("a", ("WR",), 9.0)], settings, locked={9: "a"})


def valuation(
    key: str,
    name: str,
    position: str,
    points: float,
    *,
    positions: tuple[str, ...] | None = None,
    status: str = "",
    status_full: str = "",
    is_bye: bool = False,
    availability: float = 1.0,
) -> WeeklyValuation:
    return WeeklyValuation(
        player_key=key,
        name=name,
        position=position,
        team="FA",
        week=7,
        projected_points=points,
        eligible_positions=positions or (position,),
        status=status,
        status_full=status_full,
        is_bye=is_bye,
        availability=0.0 if is_bye else availability,
    )


class TestStartSit:
    SETTINGS = settings_for([("QB", 1), ("WR", 2), ("W/R/T", 1), ("BN", 3)])

    def board(self):
        values = {
            "qb": valuation("qb", "The QB", "QB", 20.0),
            "wr1": valuation("wr1", "Best WR", "WR", 18.0),
            "wr2": valuation("wr2", "Second WR", "WR", 12.0),
            "wr3": valuation("wr3", "Benched WR", "WR", 15.0),
            "rb1": valuation("rb1", "The RB", "RB", 9.0),
        }
        entries = [
            RosterEntry("qb", "t1", 7, "QB"),
            RosterEntry("wr1", "t1", 7, "WR"),
            RosterEntry("wr2", "t1", 7, "WR"),
            RosterEntry("rb1", "t1", 7, "W/R/T"),
            RosterEntry("wr3", "t1", 7, "BN"),
        ]
        return values, entries

    def test_current_lineup_reads_the_slots_yahoo_reports(self):
        values, entries = self.board()
        board = current_lineup(values, entries, self.SETTINGS)
        assert board.total == pytest.approx(59.0)
        assert set(board.starter_keys) == {"qb", "wr1", "wr2", "rb1"}
        assert board.bench == ("wr3",)

    def test_the_better_bench_player_is_surfaced_as_a_change(self):
        values, entries = self.board()
        plan = start_sit(values, entries, self.SETTINGS)

        assert plan.optimal.total == pytest.approx(65.0)
        assert plan.points_left_on_bench == pytest.approx(6.0)
        assert len(plan.changes) == 1
        change = plan.changes[0]
        assert change.player_in == "wr3"
        assert change.player_out == "rb1"
        assert change.points_delta == pytest.approx(6.0)

    def test_an_already_optimal_lineup_produces_no_changes(self):
        values, entries = self.board()
        entries = [
            RosterEntry("qb", "t1", 7, "QB"),
            RosterEntry("wr1", "t1", 7, "WR"),
            RosterEntry("wr3", "t1", 7, "WR"),
            RosterEntry("wr2", "t1", 7, "W/R/T"),
            RosterEntry("rb1", "t1", 7, "BN"),
        ]
        plan = start_sit(values, entries, self.SETTINGS)
        assert plan.is_already_optimal
        assert plan.points_left_on_bench == pytest.approx(0.0)

    def test_shuffling_a_player_between_slots_is_not_reported_as_a_change(self):
        """Moving a receiver from WR to the flex changes nobody's Sunday."""
        values, entries = self.board()
        entries = [
            RosterEntry("qb", "t1", 7, "QB"),
            RosterEntry("wr1", "t1", 7, "W/R/T"),
            RosterEntry("wr3", "t1", 7, "WR"),
            RosterEntry("wr2", "t1", 7, "WR"),
            RosterEntry("rb1", "t1", 7, "BN"),
        ]
        plan = start_sit(values, entries, self.SETTINGS)
        assert plan.changes == ()

    def test_a_sub_point_upgrade_is_not_worth_reporting(self):
        values = {
            "wr1": valuation("wr1", "Starter", "WR", 10.0),
            "wr2": valuation("wr2", "Barely Better", "WR", 10.2),
        }
        entries = [
            RosterEntry("wr1", "t1", 7, "WR"),
            RosterEntry("wr2", "t1", 7, "BN"),
        ]
        plan = start_sit(values, entries, settings_for([("WR", 1), ("BN", 1)]))
        # The optimizer still knows; the digest just does not pretend 0.2 is advice.
        assert plan.optimal.total == pytest.approx(10.2)
        assert plan.changes == ()

    def test_a_bye_leads_the_reason_rather_than_the_point_difference(self):
        values = {
            "starter": valuation("starter", "On Bye", "WR", 0.0, is_bye=True),
            "bench": valuation("bench", "Available", "WR", 7.0),
        }
        entries = [
            RosterEntry("starter", "t1", 7, "WR"),
            RosterEntry("bench", "t1", 7, "BN"),
        ]
        plan = start_sit(values, entries, settings_for([("WR", 1), ("BN", 1)]))
        assert plan.changes[0].reason == "On Bye is on bye"

    def test_players_on_ir_are_never_offered(self):
        values = {
            "wr1": valuation("wr1", "Starter", "WR", 5.0),
            "stashed": valuation("stashed", "Stashed Stud", "WR", 25.0),
        }
        entries = [
            RosterEntry("wr1", "t1", 7, "WR"),
            RosterEntry("stashed", "t1", 7, "IR"),
        ]
        plan = start_sit(values, entries, settings_for([("WR", 1), ("BN", 1), ("IR", 1)]))
        # Yahoo will not accept it, so recommending it would be advice you cannot take.
        assert plan.changes == ()
        assert plan.optimal.starter_keys == ("wr1",)

    def test_a_rostered_player_with_no_projection_is_reported_not_zeroed(self):
        values = {"wr1": valuation("wr1", "Known", "WR", 8.0)}
        entries = [
            RosterEntry("wr1", "t1", 7, "WR"),
            RosterEntry("mystery", "t1", 7, "BN"),
        ]
        plan = start_sit(values, entries, settings_for([("WR", 1), ("BN", 1)]))
        assert plan.unvalued == ("mystery",)
        assert "mystery" not in plan.optimal.starter_keys


class TestInactiveCheck:
    SETTINGS = settings_for([("WR", 2), ("BN", 2)])

    def test_a_bye_is_flagged_with_a_named_replacement(self):
        values = {
            "a": valuation("a", "Fine Guy", "WR", 12.0),
            "b": valuation("b", "On Bye", "WR", 0.0, is_bye=True),
            "c": valuation("c", "Bench Guy", "WR", 7.0),
        }
        entries = [
            RosterEntry("a", "t1", 7, "WR"),
            RosterEntry("b", "t1", 7, "WR"),
            RosterEntry("c", "t1", 7, "BN"),
        ]
        risks = inactive_check(values, entries, self.SETTINGS)
        assert len(risks) == 1
        assert risks[0].name == "On Bye"
        assert risks[0].reason == "bye"
        assert risks[0].is_certain
        assert risks[0].replacement_name == "Bench Guy"
        assert risks[0].gain_from_replacing == pytest.approx(7.0)

    def test_questionable_is_flagged_but_not_certain(self):
        values = {
            "a": valuation("a", "Hurt Guy", "WR", 9.0, status="Q", status_full="Q - Knee"),
            "b": valuation("b", "Fine Guy", "WR", 11.0),
        }
        entries = [
            RosterEntry("a", "t1", 7, "WR"),
            RosterEntry("b", "t1", 7, "WR"),
        ]
        risks = inactive_check(values, entries, self.SETTINGS)
        assert [risk.reason for risk in risks] == ["questionable"]
        assert risks[0].is_certain is False
        assert risks[0].status_full == "Q - Knee"

    def test_certain_absences_sort_above_uncertain_ones(self):
        values = {
            "bye": valuation("bye", "On Bye", "WR", 0.0, is_bye=True),
            "q": valuation("q", "Questionable", "WR", 14.0, status="Q"),
        }
        entries = [
            RosterEntry("bye", "t1", 7, "WR"),
            RosterEntry("q", "t1", 7, "WR"),
        ]
        risks = inactive_check(values, entries, self.SETTINGS)
        # A bye you have not noticed outranks a tag you have, whatever the points say.
        assert [risk.reason for risk in risks] == ["bye", "questionable"]

    def test_a_healthy_lineup_reports_nothing(self):
        values = {
            "a": valuation("a", "A", "WR", 10.0),
            "b": valuation("b", "B", "WR", 9.0),
        }
        entries = [RosterEntry("a", "t1", 7, "WR"), RosterEntry("b", "t1", 7, "WR")]
        assert inactive_check(values, entries, self.SETTINGS) == ()

    def test_an_unplayable_bench_player_is_never_offered_as_the_fix(self):
        values = {
            "a": valuation("a", "Out Starter", "WR", 0.0, status="O", availability=0.0),
            "b": valuation("b", "Fine", "WR", 8.0),
            "c": valuation("c", "Also On Bye", "WR", 0.0, is_bye=True),
        }
        entries = [
            RosterEntry("a", "t1", 7, "WR"),
            RosterEntry("b", "t1", 7, "WR"),
            RosterEntry("c", "t1", 7, "BN"),
        ]
        risks = inactive_check(values, entries, self.SETTINGS)
        assert risks[0].name == "Out Starter"
        assert risks[0].replacement_name == ""

    def test_it_can_be_pointed_at_a_specific_lineup(self):
        values = {
            "a": valuation("a", "Bye Guy", "WR", 0.0, is_bye=True),
            "b": valuation("b", "Fine", "WR", 8.0),
        }
        entries = [RosterEntry("a", "t1", 7, "BN"), RosterEntry("b", "t1", 7, "WR")]
        empty = Lineup(starters=(), bench=(), empty=(), total=0.0)
        # Nothing is started in the lineup handed in, so there is nothing to flag.
        assert inactive_check(values, entries, self.SETTINGS, lineup=empty) == ()
