"""Tests for the draft board and the poll/manual reconciliation."""

from __future__ import annotations

import threading

import pytest

from ff_helper.draft.state import DraftState, pick_number, picks_for_slot
from ff_helper.draft.sync import DraftSync
from ff_helper.yahoo.models import DraftPick, League, Team


def league(num_teams=12, status="drafting") -> League:
    return League(
        league_key="461.l.1",
        league_id="1",
        name="Test",
        num_teams=num_teams,
        season="2026",
        draft_status=status,
        scoring_type="head",
    )


def teams(num_teams=12, my_slot=5) -> list[Team]:
    return [
        Team(
            team_key=f"461.l.1.t.{i}",
            team_id=str(i),
            name=f"Team {i}",
            is_mine=(i == my_slot),
            draft_position=i,
        )
        for i in range(1, num_teams + 1)
    ]


def state(num_teams=12, my_slot=5, rounds=15) -> DraftState:
    return DraftState(league=league(num_teams), teams=teams(num_teams, my_slot), rounds=rounds)


class TestSnakeMath:
    def test_odd_rounds_run_forward(self):
        assert pick_number(1, 1, 12) == 1
        assert pick_number(1, 5, 12) == 5
        assert pick_number(3, 5, 12) == 29

    def test_even_rounds_run_backward(self):
        assert pick_number(2, 1, 12) == 24
        assert pick_number(2, 12, 12) == 13
        assert pick_number(2, 5, 12) == 20

    def test_slot_five_in_a_twelve_team_league(self):
        assert picks_for_slot(5, 12, 4) == [5, 20, 29, 44]

    def test_non_snake_runs_forward_every_round(self):
        assert pick_number(2, 5, 12, snake=False) == 17


class TestTurnTracking:
    def test_current_pick_starts_at_one(self):
        assert state().current_pick == 1

    def test_identifies_my_turn(self):
        board = state(my_slot=5)
        for pick in range(1, 5):
            board.synced[pick] = DraftPick(pick=pick, round=1, team_key="x", player_key=f"p{pick}")
        assert board.current_pick == 5
        assert board.is_my_turn

    def test_gap_between_picks_alternates_in_a_snake(self):
        board = state(my_slot=5)
        # Slot 5 picks at 5 and 20 (15 apart), then 20 and 29 (9 apart). That asymmetry
        # is the whole reason a player worth passing at one turn is worth taking at the next.
        assert board.next_pick_after(5) == 20
        assert board.next_pick_after(20) == 29

    def test_no_next_pick_after_the_last_round(self):
        board = state(my_slot=5, rounds=2)
        assert board.next_pick_after(20) is None

    def test_picks_until_my_turn(self):
        board = state(my_slot=5)
        assert board.picks_until_my_turn == 4
        board.synced[1] = DraftPick(pick=1, round=1, team_key="x", player_key="p1")
        assert board.picks_until_my_turn == 3

    def test_team_on_the_clock_reverses_in_even_rounds(self):
        board = state(num_teams=12, my_slot=5)
        for pick in range(1, 13):
            board.synced[pick] = DraftPick(pick=pick, round=1, team_key="x", player_key=f"p{pick}")
        # Pick 13 opens round 2, which starts with slot 12.
        assert board.current_pick == 13
        assert board.team_on_the_clock().draft_position == 12

    def test_my_slot_is_none_before_yahoo_publishes_the_order(self):
        # Guessing a slot here would silently corrupt every next-pick calculation.
        board = DraftState(
            league=league(),
            teams=[Team(team_key="t.1", team_id="1", name="Mine", is_mine=True)],
        )
        assert board.my_slot is None
        assert board.my_picks == []
        assert board.is_my_turn is False


class TestReconciliation:
    def test_yahoo_picks_populate_the_board(self):
        board = state()
        picks = [DraftPick(pick=1, round=1, team_key="t.1", player_key="p.1")]
        new = board.apply_sync(picks, timestamp=100.0)
        assert len(new) == 1
        assert board.drafted_player_keys == {"p.1"}
        assert board.last_sync == 100.0

    def test_resyncing_the_same_pick_is_not_reported_as_new(self):
        board = state()
        picks = [DraftPick(pick=1, round=1, team_key="t.1", player_key="p.1")]
        board.apply_sync(picks, timestamp=100.0)
        assert board.apply_sync(picks, timestamp=101.0) == []

    def test_manual_pick_fills_the_gap_when_the_feed_lags(self):
        board = state()
        board.record_manual("p.99")
        assert board.current_pick == 2
        assert "p.99" in board.drafted_player_keys

    def test_manual_pick_is_attributed_to_the_right_team(self):
        board = state(num_teams=12, my_slot=5)
        entry = board.record_manual("p.99", pick=13)  # first pick of round 2 -> slot 12
        assert entry.team_key == "461.l.1.t.12"
        assert entry.round == 2

    def test_yahoo_supersedes_a_matching_manual_pick_silently(self):
        board = state()
        board.record_manual("p.1")
        board.apply_sync(
            [DraftPick(pick=1, round=1, team_key="t.1", player_key="p.1")], timestamp=100.0
        )
        assert board.manual == {}
        assert board.superseded == []

    def test_conflicting_manual_pick_is_corrected_and_reported(self):
        # If you guess wrong while the feed is stalled, Yahoo wins -- but you are told,
        # rather than the board quietly rewriting itself underneath you.
        board = state()
        board.record_manual("p.WRONG")
        board.apply_sync(
            [DraftPick(pick=1, round=1, team_key="t.1", player_key="p.RIGHT")], timestamp=100.0
        )
        assert board.drafted_player_keys == {"p.RIGHT"}
        assert len(board.superseded) == 1
        assert "p.WRONG" in board.superseded[0]

    def test_undo_removes_only_manual_entries(self):
        board = state()
        board.apply_sync(
            [DraftPick(pick=1, round=1, team_key="t.1", player_key="p.1")], timestamp=100.0
        )
        board.record_manual("p.2")
        assert board.undo_last_manual().player_key == "p.2"
        # The synced pick must survive an undo.
        assert board.undo_last_manual() is None
        assert board.drafted_player_keys == {"p.1"}

    def test_staleness_reports_seconds_since_last_sync(self):
        board = state()
        assert board.staleness(now=100.0) is None
        board.apply_sync([], timestamp=100.0)
        assert board.staleness(now=103.5) == pytest.approx(3.5)


class TestRosterCounts:
    def test_counts_my_positions(self):
        board = state(my_slot=5)
        board.synced[5] = DraftPick(pick=5, round=1, team_key="461.l.1.t.5", player_key="p.rb")
        board.synced[20] = DraftPick(pick=20, round=2, team_key="461.l.1.t.5", player_key="p.wr")
        board.synced[6] = DraftPick(pick=6, round=1, team_key="461.l.1.t.6", player_key="p.other")

        positions = {"p.rb": "RB", "p.wr": "WR", "p.other": "RB"}
        assert board.my_roster_counts(positions) == {"RB": 1, "WR": 1}


class FakeClient:
    def __init__(self, picks, fail_times=0):
        self.picks = picks
        self.fail_times = fail_times
        self.calls = 0

    def draft_results(self, league_key):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("Yahoo is having a moment")
        return self.picks

    def draft_status(self, league_key):
        return "drafting"


class TestSync:
    def test_poll_merges_into_state(self):
        board = state()
        picks = [DraftPick(pick=1, round=1, team_key="t.1", player_key="p.1")]
        sync = DraftSync(FakeClient(picks), board, lock=threading.Lock())

        assert len(sync.poll_once()) == 1
        assert board.drafted_player_keys == {"p.1"}
        assert sync.healthy

    def test_failures_are_recorded_without_killing_the_loop(self):
        board = state()
        sync = DraftSync(FakeClient([], fail_times=5), board, lock=threading.Lock())

        for _ in range(4):
            with pytest.raises(RuntimeError):
                sync.poll_once()
            sync.consecutive_failures += 1

        assert not sync.healthy

    def test_backoff_grows_with_consecutive_failures(self):
        board = state()
        sync = DraftSync(FakeClient([]), board, interval=2.0, lock=threading.Lock())
        assert sync._current_interval() == 2.0
        sync.consecutive_failures = 3
        assert sync._current_interval() > 2.0

    def test_predraft_polls_slowly(self):
        board = DraftState(league=league(status="predraft"), teams=teams())
        sync = DraftSync(FakeClient([]), board, interval=2.0, lock=threading.Lock())
        assert sync._current_interval() > 2.0
