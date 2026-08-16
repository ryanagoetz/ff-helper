"""The assistant: everything wired together behind one object.

Holds the valuation model (which is fixed once the snapshot is loaded) and the live draft
board (which changes constantly), and answers the only question that matters during a
draft: given what is gone, who should I take?
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from ff_helper.draft.state import DraftState
from ff_helper.engine import replacement
from ff_helper.engine.replacement import ReplacementLevels
from ff_helper.engine.vona import Recommendation, recommend
from ff_helper.rankings.blend import BlendResult, PlayerValuation, blend
from ff_helper.rankings.cache import Snapshot
from ff_helper.rankings.players import PlayerRegistry
from ff_helper.rankings.sources import yahoo_adp
from ff_helper.yahoo.models import League


@dataclass
class Assistant:
    league: League
    state: DraftState
    registry: PlayerRegistry
    valuations: BlendResult
    levels: ReplacementLevels
    lock: threading.Lock
    notes: list[str]

    # -- construction ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        league: League,
        state: DraftState,
        snapshot: Snapshot,
        *,
        lock: threading.Lock | None = None,
    ) -> Assistant:
        if league.settings is None:
            raise ValueError("League settings are required to value players.")

        registry = PlayerRegistry(snapshot.players)

        # Yahoo ADP comes from the player pool itself rather than a separate fetch.
        rows = list(snapshot.rows) + yahoo_adp.from_players(snapshot.players)
        grouped, report = registry.crosswalk(rows)

        valuations = blend(registry, grouped, league.settings)
        levels = replacement.compute(
            list(valuations.valuations.values()), league.settings, league.num_teams
        )

        notes = list(snapshot.notes) + list(valuations.notes)
        if report.unmatched:
            notes.append(f"{len(report.unmatched)} source rows did not match a Yahoo player")

        state.rounds = league.settings.roster_size or state.rounds

        return cls(
            league=league,
            state=state,
            registry=registry,
            valuations=valuations,
            levels=levels,
            lock=lock or threading.Lock(),
            notes=notes,
        )

    # -- views -------------------------------------------------------------------------

    @property
    def position_of(self) -> dict[str, str]:
        return {key: value.position for key, value in self.valuations.valuations.items()}

    def available(self) -> list[PlayerValuation]:
        drafted = self.state.drafted_player_keys
        return [
            valuation for key, valuation in self.valuations.valuations.items() if key not in drafted
        ]

    def recommendations(self, limit: int = 8) -> list[Recommendation]:
        """The ranked short list for *your* next turn.

        The horizon is always your upcoming pick and the one after it, even when someone
        else is on the clock. Anchoring it to the live pick instead would make the board
        lurch every time your turn came around: at pick 4 the next-pick horizon is one
        pick away, every survival probability is ~1, and the ranking collapses to raw
        value -- then flips to a scarcity ranking the instant pick 5 lands. Same question,
        wildly different answer, purely because of when you looked.
        """
        with self.lock:
            current = self.state.current_pick
            # The next turn that is mine, which is `current` itself when it is my turn.
            my_turn = next((pick for pick in self.state.my_picks if pick >= current), None)
            target = my_turn if my_turn is not None else current
            next_pick = self.state.next_pick_after(target)
            roster = self.state.my_roster_counts(self.position_of)
            available = self.available()

        if self.league.settings is None or not available:
            return []

        return recommend(
            available,
            self.levels,
            self.league.settings,
            roster,
            current_pick=current,
            next_pick=next_pick,
            limit=limit,
        )

    def search(self, query: str, limit: int = 10) -> list[PlayerValuation]:
        """Name search over undrafted players, for the manual override box."""
        needle = query.strip().lower()
        if not needle:
            return []
        # Hold the lock while reading the board: the poller writes to it from another
        # thread, and an unlocked read can return a player the poller just marked drafted.
        with self.lock:
            available = self.available()
        matches = [v for v in available if needle in v.name.lower()]
        matches.sort(key=lambda v: v.adp)
        return matches[:limit]

    def snapshot_state(self) -> dict:
        """A JSON-ready view of the board for the UI."""
        with self.lock:
            state = self.state
            now = time.time()
            staleness = state.staleness(now)
            board = sorted(state.board.values(), key=lambda pick: -pick.pick)[:12]
            recent = [
                {
                    "pick": pick.pick,
                    "round": pick.round,
                    "team": self._team_name(pick.team_key),
                    "player": self._player_name(pick.player_key),
                    "manual": pick.pick in state.manual,
                }
                for pick in board
            ]
            my_team = state.my_team
            roster_counts = state.my_roster_counts(self.position_of)
            my_roster = (
                [
                    {
                        "pick": pick.pick,
                        "player": self._player_name(pick.player_key),
                        "position": self.position_of.get(pick.player_key, "?"),
                    }
                    for pick in state.picks_by_team(my_team.team_key)
                ]
                if my_team
                else []
            )

            return {
                "league": self.league.name,
                "draft_status": state.draft_status,
                "current_pick": state.current_pick,
                "total_picks": state.total_picks,
                "round": (state.current_pick - 1) // max(state.num_teams, 1) + 1,
                "is_my_turn": state.is_my_turn,
                "picks_until_my_turn": state.picks_until_my_turn,
                "my_slot": state.my_slot,
                "next_pick": state.next_pick_after(state.current_pick),
                "on_the_clock": (
                    state.team_on_the_clock().name if state.team_on_the_clock() else None
                ),
                "recent_picks": recent,
                "my_roster": my_roster,
                "roster_counts": roster_counts,
                "staleness": staleness,
                "sync_error": state.last_sync_error,
                "superseded": state.superseded[-5:],
                "notes": self.notes,
            }

    def _player_name(self, player_key: str) -> str:
        valuation = self.valuations.valuations.get(player_key)
        if valuation is not None:
            return valuation.name
        player = self.registry.by_key.get(player_key)
        return player.full_name if player else player_key

    def _team_name(self, team_key: str) -> str:
        team = next((t for t in self.state.teams if t.team_key == team_key), None)
        return team.name if team else team_key
