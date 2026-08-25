"""The assistant: everything wired together behind one object.

Holds the valuation model (which is fixed once the snapshot is loaded) and the live draft
board (which changes constantly), and answers the only question that matters during a
draft: given what is gone, who should I take?
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from ff_helper import config
from ff_helper.draft.state import DraftState
from ff_helper.engine import lineup, nomination, replacement
from ff_helper.engine.auction import (
    AuctionRecommendation,
    DollarValues,
    Sale,
    compute_par_values,
    inflation_factor,
    recommend_auction,
)
from ff_helper.engine.replacement import ReplacementLevels
from ff_helper.engine.room import observations_from_board, room_tendencies
from ff_helper.engine.simulate import SimulationConfig, SimulationResult, simulate_market
from ff_helper.engine.vona import Recommendation, recommend
from ff_helper.rankings.blend import BlendResult, PlayerValuation, blend
from ff_helper.rankings.cache import Snapshot
from ff_helper.rankings.players import PlayerRegistry
from ff_helper.rankings.sources import yahoo_adp
from ff_helper.yahoo.models import League

# High enough to leave the pool untruncated wherever a whole-board run is wanted.
_WHOLE_POOL = 10_000

log = logging.getLogger(__name__)


class _NominationModelError(Exception):
    """Raised when the nomination model itself fails, so the endpoint can tell it apart.

    The shared engine beneath it raises whatever it raises, uncaught, which is the point:
    a buy-side bug must not be reported to the drafter as "the nomination model is
    unavailable, the board above is unaffected" while the board is in fact broken too.
    """


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
    # Monte Carlo rollouts per snake recommendation; 0 keeps the analytic model alone.
    # ``build`` reads FF_MC_ROLLOUTS; tests and scripts may set it directly.
    mc_rollouts: int = 0
    # The last whole-pool auction run, keyed by ``DraftState.board_key``. Both the buy list
    # and the nomination list need the *same* board priced, and the page asks for both on
    # every 2-second poll. Measured on this league's 524-player opening board, one poll
    # serving both panels: 30.9 ms when a sale has landed, 2.6 ms when the poller merely
    # re-sent an unchanged list -- which is most polls, since sales arrive far slower than
    # 2 s. That second figure is the one a counter could not deliver: ``apply_sync`` fires
    # on every tick regardless, so a bumped-by-hand key was cold every time and the saving
    # existed only in tests. See ``DraftState.board_key``. ``limit`` only slices at the end
    # of ``recommend_auction``, so one full list serves every caller.
    _auction_cache: tuple[int, list[AuctionRecommendation]] | None = field(
        default=None, repr=False, compare=False
    )
    # Guards *computing* that cache, never the board. Held across the engine call so a
    # concurrent second caller waits and takes the result instead of duplicating it --
    # which is the whole saving, since the page fires both requests at once.
    #
    # It is emphatically not ``self.lock``: holding the board lock across the engine is the
    # thing this repo forbids, because the poller thread would block behind it. Lock order
    # is always _engine_lock -> lock and never the reverse, and the poller takes only
    # ``lock``, so the two cannot deadlock.
    _engine_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

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
            mc_rollouts=config.mc_rollouts(),
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
            roster_byes = self._my_roster_byes()

            # My remaining turns after this one, for the needs-to-picks plan; the full
            # list feeds the pick-budget normalizer (my picks are not opponent removals).
            all_my_picks = list(self.state.my_picks)
            future_picks = [pick for pick in all_my_picks if pick > target]

            # For each future turn: what share of the intervening picks belongs to teams
            # that still need each position as a starter? ADP assumes every room needs
            # everything; this room's rosters say otherwise, and the survival model uses
            # the difference.
            position_demand = self._position_demand(current, future_picks, position_of)

            # What the picks so far say about how this room drifts from ADP -- the snake
            # analog of the auction engine's live room premiums. My own picks are
            # excluded: they reflect this engine's advice, and a model that learns
            # from its own output drifts wherever it was already leaning.
            my_team = self.state.my_team
            tendencies = room_tendencies(
                observations_from_board(
                    self.state.board.values(),
                    self.valuations.valuations,
                    exclude_team=my_team.team_key if my_team else None,
                )
            )

            # Inputs the market simulator needs are copied under the lock; the
            # simulation itself runs outside it, like ``recommend`` below -- both are
            # pure functions of the copies, and neither should block the poller.
            market_inputs = None
            if self.mc_rollouts > 0:
                sim_targets = sorted(
                    {p for p in [next_pick, *future_picks] if p is not None and p > current}
                )
                if sim_targets:
                    market_inputs = (
                        sim_targets,
                        *self._market_inputs(current, sim_targets, position_of),
                    )

        if self.league.settings is None or not available:
            return []

        market: SimulationResult | None = None
        if market_inputs is not None:
            sim_targets, pick_owner, team_rosters = market_inputs
            try:
                market = simulate_market(
                    available,
                    self.levels,
                    self.league.settings,
                    current_pick=current,
                    my_picks=all_my_picks,
                    targets=sim_targets,
                    pick_owner=pick_owner,
                    team_rosters=team_rosters,
                    tendencies=tendencies,
                    config=SimulationConfig(rollouts=self.mc_rollouts),
                )
            except Exception:  # noqa: BLE001 -- advice must survive a simulator bug
                market = None

        return recommend(
            available,
            self.levels,
            self.league.settings,
            roster,
            current_pick=current,
            next_pick=next_pick,
            future_picks=future_picks,
            position_demand=position_demand,
            my_picks=all_my_picks,
            tendencies=tendencies,
            num_teams=self.state.num_teams,
            market=market,
            roster_byes=roster_byes,
            limit=limit,
        )

    def _my_roster_byes(self) -> dict[str, list[int]]:
        """Bye weeks I already roster, by position, keepers included.

        Caller must hold the lock. Feeds the bye-stack penalty: a second player at a
        thin position sharing a bye costs a week where that slot scores zero.
        """
        team = self.state.my_team
        if team is None:
            return {}
        keys = [pick.player_key for pick in self.state.picks_by_team(team.team_key)]
        keys += [keeper.player_key for keeper in self.state.keepers_for(team.team_key)]
        byes: dict[str, list[int]] = {}
        for key in keys:
            valuation = self.valuations.valuations.get(key)
            if valuation is not None and valuation.bye_week is not None:
                byes.setdefault(valuation.position, []).append(valuation.bye_week)
        return byes

    def _market_inputs(
        self,
        current: int,
        targets: list[int],
        position_of: dict[str, str],
    ) -> tuple[dict[int, str], dict[str, dict[str, int]]]:
        """Pick ownership and every team's roster counts, for the market simulator.

        Caller must hold the lock: both come off the live board. Picks past the end of
        the draft (or before Yahoo publishes the order) simply have no owner, and the
        simulator drafts them with a generic team that needs everything.
        """
        pick_owner: dict[int, str] = {}
        for number in range(current, max(targets)):
            team = self.state.team_for_pick(number)
            if team is not None:
                pick_owner[number] = team.team_key
        team_rosters = {
            team.team_key: self.state.roster_counts(team.team_key, position_of)
            for team in self.state.teams
        }
        return pick_owner, team_rosters

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
        return self._priced_board()[:limit]

    def _priced_board(self, alongside=None):
        """The whole pool, priced once per board state.

        ``limit`` is a slice at the very end of ``recommend_auction`` and changes nothing
        above it, so one full run serves the buy list, the nomination list and the backtest
        alike. Keyed on ``DraftState.board_key``, a fingerprint of the board itself.

        ``alongside`` is a callable run inside the *same* lock hold that reads the key, and
        its result comes back beside the board. That is not a convenience: the nomination
        list needs rival budgets and roster holes drawn from the identical board these
        prices describe. Reading them in a separate acquisition let the two straddle a sale
        -- demonstrated, a rival whose real ``max_bid`` was 0 still named in "Aimed at",
        which is the one column that panel exists for. ``auction_recommendations`` never had
        the split; this restores the same one-snapshot guarantee to the other caller.
        """
        assert self.dollars is not None and self.league.settings is not None

        with self._engine_lock:
            with self.lock:
                key = self.state.board_key
                extra = alongside() if alongside is not None else None
                cached = self._auction_cache
                if cached is not None and cached[0] == key:
                    return (cached[1], extra) if alongside is not None else cached[1]
                inputs = self._auction_inputs()
            if inputs is None:
                return ([], extra) if alongside is not None else []
            picks = recommend_auction(*inputs[0], **inputs[1])
            with self.lock:
                # Only cache if the board has not moved while the engine ran. A sale landing
                # mid-computation makes these numbers one pick stale; serving them once is
                # the staleness every poll already carries, but storing them under the *new*
                # key would pin that staleness until the next sale.
                if self.state.board_key == key:
                    self._auction_cache = (key, picks)
            return (picks, extra) if alongside is not None else picks

    def _auction_inputs(self):
        """Everything ``recommend_auction`` needs, read off the board. Caller holds the lock."""
        position_of = self.position_of
        roster = self.state.my_roster_counts(position_of)
        available = self.available()
        roster_byes = self._my_roster_byes()
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
            # Par is the fallback basis, and it is the only one whenever no source
            # priced the player -- the usual case offline, where an auction column in
            # the projections CSV is the only way a cost arrives at all.
            # ``auction.PriceBasis``'s room tier leans on this: it reads the premium as
            # a per-position *ratio* rather than a price level, which is why a pool of
            # mixed bases (some sales measured against a sheet cost, some against par)
            # degrades its precision rather than its meaning.
            expected = valuation.market_cost
            if expected is None:
                expected = self.dollars.value_of(pick.player_key)
            sales.append(
                Sale(position=valuation.position, price=float(pick.cost), expected=expected)
            )

        if not available:
            return None

        return (
            (available, self.levels, self.dollars, self.league.settings, roster),
            {
                "money_remaining": money_remaining,
                "slots_remaining": slots_remaining,
                "my_max_bid": my_max_bid,
                "my_budget_remaining": my_budget,
                "league_position_counts": league_counts,
                "sales": sales,
                "roster_byes": roster_byes,
                "limit": _WHOLE_POOL,
            },
        )

    def _rival_seats(self, position_of: dict[str, str]) -> list[nomination.RivalSeat]:
        """Every opponent's roster, budget and ceiling. Caller must hold the lock.

        The per-team parallel to ``auction_recommendations``' league-wide flatten, which is
        deliberately left exactly as it is: that dict sizes the competition for a position's
        *leftovers* and feeds the smart cap, and widening it would move numbers on the buy
        side that nothing here is allowed to move. (``slot_ladder``'s docstring notes that
        genuine unmet starting demand needs per-team counts, which these now make available
        -- that is a separate change, gated on its own backtest.)

        ``_position_demand`` already walks every opponent through ``assign_lineup`` for the
        snake path and is likewise untouched, so this diff reaches no snake code at all.
        """
        seats: list[nomination.RivalSeat] = []
        for team in self.state.teams:
            if team.is_mine:
                continue
            seats.append(
                nomination.RivalSeat(
                    team_key=team.team_key,
                    name=team.name,
                    roster_counts=self.state.roster_counts(team.team_key, position_of),
                    max_bid=self.state.max_bid(team.team_key),
                )
            )
        return seats

    def nomination_list(
        self, limit: int = 6, *, nomination_only: bool = False
    ) -> list[nomination.NominationCandidate]:
        """Whose name to say next -- the other half of an auction.

        ``auction_recommendations`` answers "who should I bid on". This answers "whose name
        should I say", which is mostly a question about other people's money.

        The engine runs over the **whole** pool, not a short list. The best drain is by
        construction a player the buy list ranks low -- that is what "I do not want him"
        means -- so a short list here would hand the model exactly the players it exists to
        steer you away from nominating, and the bug would render perfectly.

        It reads the *same* priced board the buy list does, through ``_priced_board``'s
        revision cache, rather than running the engine again. Two things follow, and both
        matter. The poll stays at one ~45 ms engine pass on this league's 524-player opening
        board instead of two. And both panels necessarily quote the same numbers for the
        same player -- two prices five seconds apart is the mistake ``evaluate``'s docstring
        already argues against, and here they would have been two prices *simultaneously*.

        Sharing the result does not weaken the isolation this module wanted: the shared
        object is the buy-side output, produced by buy-side code, so a bug in the nomination
        model still cannot reach ``/api/recommend``.
        """
        if not self.is_auction or self.dollars is None or self.league.settings is None:
            return []

        def board_facts():
            """Read under the same lock hold that fingerprints the board. See _priced_board."""
            position_of = self.position_of
            my_team = self.state.my_team
            if my_team is None:
                return None
            return (
                self.state.my_roster_counts(position_of),
                self.state.budget_remaining(my_team.team_key),
                self.state.slots_remaining(my_team.team_key),
                self.state.league_money_remaining(),
                self._rival_seats(position_of),
            )

        # The same priced board the buy list reads, not a second run of it. Shared through
        # ``_priced_board``'s cache, so when the page fires both requests at once the second
        # one waits on ``_engine_lock`` and takes the first one's result.
        picks, facts = self._priced_board(alongside=board_facts)
        if not picks or facts is None:
            return []
        my_roster, my_budget, my_slots, league_money, rivals = facts

        if not nomination_only:
            return nomination.recommend_nominations(
                picks,
                self.league.settings,
                my_roster,
                rivals,
                my_budget_remaining=my_budget,
                my_slots_remaining=my_slots,
                league_money_remaining=league_money,
                limit=limit,
            )

        # ``nomination_only`` marks where the isolation boundary actually is. Everything
        # above this point is the *shared* board -- the same ``recommend_auction`` pass
        # ``/api/recommend`` serves -- so a failure there must surface the way it does on the
        # buy side rather than being reported as a broken nomination model. Only the call
        # below is this module's own work, and only it is the endpoint's to catch.
        try:
            return nomination.recommend_nominations(
                picks,
                self.league.settings,
                my_roster,
                rivals,
                my_budget_remaining=my_budget,
                my_slots_remaining=my_slots,
                league_money_remaining=league_money,
                limit=limit,
            )
        except Exception:
            log.exception("nomination model failed")
            raise _NominationModelError from None

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
