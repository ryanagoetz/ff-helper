"""The authoritative draft board.

Two things write to this board: the Yahoo poller and you, typing a pick in manually. That
is a deliberate design choice rather than a fallback bolted on. Yahoo's ``draftresults``
endpoint is polled rather than pushed, and how promptly it reflects an in-progress draft is
not something we can guarantee -- so the board is the source of truth and the poller is
merely one of its writers.

Conflicts resolve toward Yahoo, because it is the system of record for what actually
happened, but a manual entry is never silently discarded: it is superseded and reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ff_helper.yahoo.models import DraftPick, League, Team


def pick_number(round_number: int, slot: int, num_teams: int, *, snake: bool = True) -> int:
    """Overall pick number for a draft slot in a given round.

    Slots are 1-indexed. In a snake draft even rounds run in reverse, which is precisely
    why the gap between your picks alternates between short and long -- and therefore why
    the same player is worth taking at one turn and worth passing at the next.
    """
    base = (round_number - 1) * num_teams
    if snake and round_number % 2 == 0:
        return base + (num_teams - slot + 1)
    return base + slot


def picks_for_slot(slot: int, num_teams: int, rounds: int, *, snake: bool = True) -> list[int]:
    return [pick_number(r, slot, num_teams, snake=snake) for r in range(1, rounds + 1)]


@dataclass
class DraftState:
    league: League
    teams: list[Team] = field(default_factory=list)
    rounds: int = 15
    snake: bool = True

    # Live draft status (predraft / drafting / postdraft). It lives here rather than on
    # League because League is a frozen snapshot of what the API returned, and this is the
    # one field that changes underneath us while the app is running.
    _draft_status: str | None = None

    # Picks Yahoo has confirmed, keyed by overall pick number.
    synced: dict[int, DraftPick] = field(default_factory=dict)
    # Picks entered by hand, same keying. Used when the feed lags or stalls.
    manual: dict[int, DraftPick] = field(default_factory=dict)

    # Bookkeeping surfaced in the UI.
    last_sync: float | None = None
    last_sync_error: str | None = None
    superseded: list[str] = field(default_factory=list)

    # -- identity ----------------------------------------------------------------------

    @property
    def draft_status(self) -> str:
        return self._draft_status or self.league.draft_status

    @draft_status.setter
    def draft_status(self, value: str) -> None:
        self._draft_status = value

    @property
    def num_teams(self) -> int:
        return self.league.num_teams or len(self.teams) or 12

    @property
    def my_team(self) -> Team | None:
        return next((team for team in self.teams if team.is_mine), None)

    @property
    def my_slot(self) -> int | None:
        team = self.my_team
        if team is None:
            return None
        if team.draft_position:
            return team.draft_position
        # Before Yahoo publishes the draft order there is no slot to report. Guessing one
        # would silently produce wrong "next pick" maths for the whole draft.
        return None

    # -- board -------------------------------------------------------------------------

    @property
    def board(self) -> dict[int, DraftPick]:
        """Merged view. Yahoo wins where both have an opinion."""
        merged = dict(self.manual)
        merged.update(self.synced)
        return merged

    @property
    def drafted_player_keys(self) -> set[str]:
        return {pick.player_key for pick in self.board.values()}

    @property
    def picks_made(self) -> int:
        board = self.board
        if not board:
            return 0
        # Count contiguously from pick 1 so a stray out-of-order entry cannot inflate it.
        count = 0
        while (count + 1) in board:
            count += 1
        return max(count, len(board))

    @property
    def current_pick(self) -> int:
        return self.picks_made + 1

    @property
    def total_picks(self) -> int:
        return self.num_teams * self.rounds

    @property
    def is_complete(self) -> bool:
        return self.picks_made >= self.total_picks

    # -- my turn -----------------------------------------------------------------------

    @property
    def my_picks(self) -> list[int]:
        slot = self.my_slot
        if slot is None:
            return []
        return picks_for_slot(slot, self.num_teams, self.rounds, snake=self.snake)

    @property
    def is_my_turn(self) -> bool:
        return self.current_pick in self.my_picks

    def next_pick_after(self, pick: int) -> int | None:
        """My next turn strictly after ``pick``, or None if I have none left."""
        return next((candidate for candidate in self.my_picks if candidate > pick), None)

    @property
    def picks_until_my_turn(self) -> int | None:
        current = self.current_pick
        if self.is_my_turn:
            return 0
        upcoming = next((p for p in self.my_picks if p >= current), None)
        return None if upcoming is None else upcoming - current

    def team_on_the_clock(self) -> Team | None:
        slot = self._slot_for_pick(self.current_pick)
        if slot is None:
            return None
        return next((team for team in self.teams if team.draft_position == slot), None)

    def _slot_for_pick(self, pick: int) -> int | None:
        if pick < 1 or pick > self.total_picks:
            return None
        num_teams = self.num_teams
        round_number = (pick - 1) // num_teams + 1
        offset = (pick - 1) % num_teams + 1
        if self.snake and round_number % 2 == 0:
            return num_teams - offset + 1
        return offset

    # -- rosters -----------------------------------------------------------------------

    def picks_by_team(self, team_key: str) -> list[DraftPick]:
        return sorted(pick for pick in self.board.values() if pick.team_key == team_key)

    def roster_counts(self, team_key: str, position_of: dict[str, str]) -> dict[str, int]:
        """Positions held by a team, using a player_key -> position map."""
        counts: dict[str, int] = {}
        for pick in self.picks_by_team(team_key):
            position = position_of.get(pick.player_key)
            if position:
                counts[position] = counts.get(position, 0) + 1
        return counts

    def my_roster_counts(self, position_of: dict[str, str]) -> dict[str, int]:
        team = self.my_team
        return self.roster_counts(team.team_key, position_of) if team else {}

    # -- writers -----------------------------------------------------------------------

    def apply_sync(self, picks: list[DraftPick], *, timestamp: float) -> list[DraftPick]:
        """Fold in what Yahoo reports. Returns picks that are new to us."""
        new_picks: list[DraftPick] = []
        for pick in picks:
            existing = self.synced.get(pick.pick)
            if existing is None or existing.player_key != pick.player_key:
                new_picks.append(pick)
            self.synced[pick.pick] = pick

            # A manual entry for the same slot has served its purpose. If we guessed the
            # wrong player, say so rather than quietly rewriting history.
            manual = self.manual.pop(pick.pick, None)
            if manual is not None and manual.player_key != pick.player_key:
                self.superseded.append(
                    f"Pick {pick.pick}: you entered {manual.player_key}, "
                    f"Yahoo reports {pick.player_key}"
                )

        self.last_sync = timestamp
        self.last_sync_error = None
        return new_picks

    def record_manual(self, player_key: str, *, pick: int | None = None) -> DraftPick:
        """Mark a player drafted by hand, for when the feed stalls mid-draft."""
        target = pick if pick is not None else self.current_pick
        slot = self._slot_for_pick(target)
        team = next((t for t in self.teams if t.draft_position == slot), None)
        entry = DraftPick(
            pick=target,
            round=(target - 1) // self.num_teams + 1,
            team_key=team.team_key if team else "",
            player_key=player_key,
        )
        self.manual[target] = entry
        return entry

    def undo_last_manual(self) -> DraftPick | None:
        if not self.manual:
            return None
        latest = max(self.manual)
        return self.manual.pop(latest)

    def staleness(self, now: float) -> float | None:
        """Seconds since the last successful sync, or None if we have never synced."""
        return None if self.last_sync is None else now - self.last_sync
