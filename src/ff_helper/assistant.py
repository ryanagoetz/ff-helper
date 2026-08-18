"""The assistant: everything wired together behind one object.

Holds the valuation model (which is fixed once the snapshot is loaded) and the live draft
board (which changes constantly), and answers the only question that matters during a
draft: given what is gone, who should I take?
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ff_helper.draft.state import DraftState
from ff_helper.engine import lineup, replacement
from ff_helper.engine.auction import (
    AuctionRecommendation,
    DollarValues,
    Sale,
    compute_par_values,
    inflation_factor,
    recommend_auction,
)
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
    # Populated only for auction leagues.
    dollars: DollarValues | None = None
    # Draft-room spellings of team names that differ from the league settings page, so a
    # buyer can still be resolved. A buyer we cannot resolve costs more than a player we
    # cannot resolve: the money never leaves the room and every price is overstated.
    team_aliases: dict[str, str] = field(default_factory=dict)

    @property
    def is_auction(self) -> bool:
        return self.state.is_auction

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

        state.roster_size = league.settings.roster_size or state.roster_size
        # Recompute rounds from whatever keepers were attached before build().
        state.apply_keepers(state.keepers)

        if state.keepers:
            notes.append(
                f"{len(state.keepers)} keepers held out of the pool; "
                f"drafting {state.rounds} rounds of a {state.roster_size}-man roster"
            )

            unvalued = [k for k in state.keepers if k.player_key not in valuations.valuations]
            if unvalued:
                # A keeper the snapshot never saw shows as a bare player key in the roster
                # panel and counts toward no position, so the engine keeps recommending the
                # spot it already fills.
                notes.append(
                    f"{len(unvalued)} keepers are not in the ranking snapshot, so they fill "
                    "no position in roster counts -- re-run scripts/fetch_rankings.py with a "
                    "larger --limit if this matters"
                )

            if league.settings.is_auction:
                unpriced = [k for k in state.keepers if k.cost is None]
                if unpriced:
                    # Unknown is not free. `spent()` can only treat a missing salary as $0,
                    # which leaves that money in the room: inflation reads the league as
                    # cash-rich against fewer slots and every bid ceiling comes out high.
                    notes.append(
                        f"WARNING: {len(unpriced)} of {len(state.keepers)} keepers have no "
                        "salary from Yahoo and are being counted as $0. Budgets and "
                        "inflation are overstated until you supply them with --keepers."
                    )

        dollars: DollarValues | None = None
        if league.settings.is_auction:
            # Keepers are priced out of the pool: they are owned, their salaries are no
            # longer biddable, and their slots are not open. This leaves `inflation`
            # carrying only live market movement rather than a static keeper correction.
            kept_keys = {keeper.player_key for keeper in state.keepers}
            kept_salary = sum(keeper.cost or 0 for keeper in state.keepers)
            dollars = compute_par_values(
                list(valuations.valuations.values()),
                levels,
                league.settings,
                league.num_teams,
                kept_player_keys=kept_keys,
                kept_salary=kept_salary,
            )
            biddable = league.num_teams * league.settings.auction_budget - kept_salary
            notes.append(
                f"Auction league: ${biddable} biddable across {dollars.pool_size} open "
                f"roster spots, ${dollars.dollars_per_vor:.2f} per point of VOR"
                + (f" ({len(kept_keys)} keepers priced out)" if kept_keys else "")
            )

        return cls(
            league=league,
            state=state,
            registry=registry,
            valuations=valuations,
            levels=levels,
            lock=lock or threading.Lock(),
            notes=notes,
            dollars=dollars,
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

    def recommendations(self, limit: int = 8) -> list[Recommendation] | list[AuctionRecommendation]:
        """The ranked short list, using whichever model this league's draft calls for."""
        if self.is_auction:
            return self.auction_recommendations(limit=limit)
        return self.snake_recommendations(limit=limit)

    def snake_recommendations(self, limit: int = 8) -> list[Recommendation]:
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
            position_of = self.position_of
            roster = self.state.my_roster_counts(position_of)
            available = self.available()

            # My remaining turns after this one, for the needs-to-picks plan.
            future_picks = [pick for pick in self.state.my_picks if pick > target]

            # For each future turn: what share of the intervening picks belongs to teams
            # that still need each position as a starter? ADP assumes every room needs
            # everything; this room's rosters say otherwise, and the survival model uses
            # the difference.
            position_demand = self._position_demand(current, future_picks, position_of)

        if self.league.settings is None or not available:
            return []

        return recommend(
            available,
            self.levels,
            self.league.settings,
            roster,
            current_pick=current,
            next_pick=next_pick,
            future_picks=future_picks,
            position_demand=position_demand,
            limit=limit,
        )

    def _position_demand(
        self,
        current: int,
        future_picks: list[int],
        position_of: dict[str, str],
    ) -> dict[int, dict[str, float]]:
        """Per future pick of mine: the share of intervening picks that still chase each
        position. Caller must hold the lock."""
        settings = self.league.settings
        if settings is None or not future_picks:
            return {}

        needed_by_team: dict[str, set[str]] = {}
        for team in self.state.teams:
            counts = self.state.roster_counts(team.team_key, position_of)
            open_dedicated, open_flex, _ = lineup.assign_lineup(counts, settings)
            needed = {position for position, count in open_dedicated.items() if count > 0}
            for eligible, count in open_flex:
                if count > 0:
                    needed |= eligible
            needed_by_team[team.team_key] = needed

        my_team = self.state.my_team
        my_key = my_team.team_key if my_team else None

        demand: dict[int, dict[str, float]] = {}
        for target in future_picks:
            rivals = [
                team
                for pick in range(current, target)
                if (team := self.state.team_for_pick(pick)) is not None
                and team.team_key != my_key
            ]
            if not rivals:
                continue
            shares: dict[str, float] = {}
            # Every position the league starts somewhere -- a position nobody needs any
            # more must land at share 0.0, not fall through to the neutral default.
            positions: set[str] = set()
            for slot in settings.starting_slots:
                positions |= slot.eligible_positions
            for position in positions:
                chasing = sum(
                    1 for team in rivals if position in needed_by_team.get(team.team_key, set())
                )
                shares[position] = chasing / len(rivals)
            demand[target] = shares
        return demand

    def auction_recommendations(self, limit: int = 8) -> list[AuctionRecommendation]:
        """Where your remaining dollars go furthest, right now.

        Unlike the snake path there is no "my turn" -- every player is biddable at all
        times -- so this is always live, and always constrained by what you can still pay.
        """
        if self.dollars is None or self.league.settings is None:
            return []

        with self.lock:
            position_of = self.position_of
            roster = self.state.my_roster_counts(position_of)
            available = self.available()
            money_remaining = self.state.league_money_remaining()
            slots_remaining = self.state.league_slots_remaining()
            my_max_bid = self.state.my_max_bid()
            my_team = self.state.my_team
            my_budget = self.state.budget_remaining(my_team.team_key) if my_team else None

            # Positions rostered across the whole league, keepers included -- the smart
            # cap needs to know how many teams still compete for each position's leftovers.
            league_counts: dict[str, int] = {}
            for team in self.state.teams:
                for position, count in self.state.roster_counts(team.team_key, position_of).items():
                    league_counts[position] = league_counts.get(position, 0) + count

            # Completed sales with real prices feed the room premium. Keeper salaries are
            # not sales and carry no price on the board, so they fall out naturally.
            sales: list[Sale] = []
            for pick in self.state.board.values():
                if pick.cost is None or pick.cost <= 0:
                    continue
                valuation = self.valuations.valuations.get(pick.player_key)
                if valuation is None:
                    continue
                expected = valuation.market_cost
                if expected is None:
                    expected = self.dollars.value_of(pick.player_key)
                sales.append(
                    Sale(position=valuation.position, price=float(pick.cost), expected=expected)
                )

        if not available:
            return []

        return recommend_auction(
            available,
            self.levels,
            self.dollars,
            self.league.settings,
            roster,
            money_remaining=money_remaining,
            slots_remaining=slots_remaining,
            my_max_bid=my_max_bid,
            my_budget_remaining=my_budget,
            league_position_counts=league_counts,
            sales=sales,
            limit=limit,
        )

    def current_inflation(self) -> float:
        """Live price level versus par, for display."""
        if self.dollars is None:
            return 1.0
        with self.lock:
            available = self.available()
            money = self.state.league_money_remaining()
            slots = self.state.league_slots_remaining()
        return inflation_factor(
            available, self.dollars, money_remaining=money, slots_remaining=slots
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

    def evaluate(self, query: str, limit: int = 5) -> list:
        """Value specific players by name, for when one is nominated.

        In an auction the question that actually gets asked is never "who is best" -- the
        board answers that already. It is "someone just said Kenneth Walker, what is he
        worth to me and when do I stop", and it gets asked with a clock running.

        The numbers have to be the *same* ones the recommendations use, which is why this
        scores the whole pool and then filters rather than valuing the player alone.
        Inflation is a property of the room, not of a player: it comes from the money and
        the talent still left, so a figure computed for one player in isolation would drift
        from the board on the same screen. Two prices for one player, five seconds apart,
        is worse than no price.
        """
        needle = query.strip().lower()
        if not needle:
            return []

        with self.lock:
            available = self.available()
        wanted = {v.player_key for v in available if needle in v.name.lower()}
        if not wanted:
            return []

        # Score everything, keep the matches. Cheap next to being wrong.
        ranked = (
            self.auction_recommendations(limit=len(available))
            if self.is_auction
            else self.snake_recommendations(limit=len(available))
        )
        return [pick for pick in ranked if pick.valuation.player_key in wanted][:limit]

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
                    "cost": pick.cost,
                    "team": self._team_name(pick.team_key),
                    "player": self._player_name(pick.player_key),
                    "manual": pick.pick in state.manual,
                }
                for pick in board
            ]
            my_team = state.my_team
            # Resolved once: `position_of` rebuilds a dict over every valued player on each
            # access, and the comprehensions below would otherwise rebuild it per row.
            position_of = self.position_of
            roster_counts = state.my_roster_counts(position_of)
            my_keepers = (
                [
                    {
                        "player": self._player_name(keeper.player_key),
                        "position": position_of.get(keeper.player_key, "?"),
                        "cost": keeper.cost,
                        "source": keeper.source,
                    }
                    for keeper in state.keepers_for(my_team.team_key)
                ]
                if my_team
                else []
            )
            auction = (
                {
                    "budget": state.budget,
                    "spent": state.spent(my_team.team_key) if my_team else 0,
                    "remaining": state.budget_remaining(my_team.team_key) if my_team else 0,
                    "max_bid": state.my_max_bid(),
                    "slots_filled": state.slots_filled(my_team.team_key) if my_team else 0,
                    "slots_remaining": state.slots_remaining(my_team.team_key) if my_team else 0,
                    "league_money_remaining": state.league_money_remaining(),
                    "teams": [
                        {
                            "team_key": team.team_key,
                            "name": team.name,
                            "remaining": state.budget_remaining(team.team_key),
                            "max_bid": state.max_bid(team.team_key),
                            "slots_remaining": state.slots_remaining(team.team_key),
                            "is_mine": team.is_mine,
                        }
                        for team in state.teams
                    ],
                }
                if state.is_auction
                else None
            )
            my_roster = (
                [
                    {
                        "pick": pick.pick,
                        "player": self._player_name(pick.player_key),
                        "position": position_of.get(pick.player_key, "?"),
                    }
                    for pick in state.picks_by_team(my_team.team_key)
                ]
                if my_team
                else []
            )

            return {
                "league": self.league.name,
                "draft_status": state.draft_status,
                "draft_type": "auction" if state.is_auction else "snake",
                "auction": auction,
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
                "my_keepers": my_keepers,
                "keeper_count": len(state.keepers),
                "roster_counts": roster_counts,
                "staleness": staleness,
                "sync_error": state.last_sync_error,
                "unresolved": [
                    {"name": name, "buyer": buyer, "cost": cost}
                    for name, (buyer, cost) in state.unresolved.items()
                ],
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
