"""What roster would the engine have drafted? The end-to-end number.

Calibration says whether the probabilities were honest; this says whether following the
advice would have left you with a better team. A recorded draft is replayed with one
change: at my turns, a policy chooses the pick instead of history. Everyone else drafts
as they actually did, with one necessary adjustment -- when a policy takes a player an
opponent later took in real life, that opponent slides to their own next recorded pick
that is still available (and to best-remaining-by-ADP if their whole script is
exhausted). Displaced players stay in the pool, so the board never invents or loses
anyone.

Auctions replay differently, in ``auction_counterfactual``. There is no pick order to
follow and no "my turn": every player is biddable at every moment, so the thing a policy
chooses is not *who* but *whether* -- at each recorded sale, do I outbid?

**Prices are held at what they actually were.** Modelling how the whole price surface
moves once one sale differs is the hard problem this deliberately does not solve: money I
do not spend on a $82 back returns to the room and lifts every later price, and nothing
here simulates that. What it answers instead is the post-mortem question, which is the one
worth asking: *given the prices that actually occurred, was a better basket available to
me?* That is a partial-equilibrium answer about my own choices, not a claim about what the
room would have done. Treat a win here as "this was reachable", never as "this would have
happened".

Two consequences follow, both deliberate. Intercepting a sale leaves the displaced buyer
short a player and holding their money, which nudges league-wide money-over-slots and so
the inflation the engine sees; the effect is small because interceptions are few, and
correcting it would require exactly the price model above. And when a policy declines a
sale I actually won, the player is seated with a rival who could genuinely have outbid me
-- a stand-in for the underbidder, whose identity the record does not preserve -- or, if
no rival could, left on the recorded buyer's board unpaid. He never comes back to me: the
policy declined on hard constraints, and returning him would silently breach them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ff_helper.assistant import Assistant
from ff_helper.backtest.capture import DraftRecord, build_state
from ff_helper.engine import lineup
from ff_helper.rankings.blend import PlayerValuation
from ff_helper.rankings.cache import Snapshot
from ff_helper.yahoo.models import DraftPick, LeagueSettings

POLICIES = ("actual", "engine", "best_vor")

# Auction policies. ``engine`` and ``engine_list`` differ only in how the advice reaches
# the human, and the gap between them is exactly what a *ranking* change can move.
AUCTION_POLICIES = ("actual", "engine", "engine_list", "best_par")

# A limit high enough to leave the pool untruncated. ``engine`` looks a nominated player
# up by name the way ``Assistant.evaluate`` does, so no short list may hide him.
_WHOLE_POOL = 10_000


@dataclass(frozen=True)
class RosterResult:
    policy: str
    # Projected points of the best legal starting lineup this roster can field. THE
    # comparison number: raw roster sums reward hoarding a position no lineup can start.
    lineup_points: float
    total_vor: float
    total_points: float
    # (pick number, player name, position) for my final roster, in draft order.
    players: tuple[tuple[int, str, str], ...]
    # Auction only; snake policies spend no money and leave this at zero.
    spent: int = 0


def counterfactual(record: DraftRecord, snapshot: Snapshot, *, policy: str) -> RosterResult:
    """Replay the record with ``policy`` making my picks; score my final roster."""
    if policy not in POLICIES:
        raise ValueError(f"Unknown policy {policy!r}; expected one of {POLICIES}.")
    if record.is_auction:
        raise ValueError("Counterfactual replay supports snake drafts only.")
    my_team = record.my_team
    if my_team is None:
        raise ValueError("Record does not identify my team; nothing to compare against.")

    league, state = build_state(record)
    assistant = Assistant.build(league, state, snapshot)

    # Each opponent's actual picks, in order -- their "script" for the replay.
    scripts: dict[str, list[str]] = {}
    for pick in record.picks:
        scripts.setdefault(pick.team_key, []).append(pick.player_key)

    drafted: set[str] = set()
    for pick in record.picks:
        if pick.team_key == my_team.team_key:
            chosen = _my_choice(assistant, policy, pick.player_key, drafted)
        else:
            chosen = _scripted_choice(assistant, scripts[pick.team_key], drafted)
        drafted.add(chosen)
        state.apply_sync(
            [
                DraftPick(
                    pick=pick.pick,
                    round=pick.round,
                    team_key=pick.team_key,
                    player_key=chosen,
                )
            ],
            timestamp=0.0,
        )

    # Keepers are roster spots too, and picks_by_team does not report them. Scored without
    # them a keeper league looks short a starter: the engine policy drafts *knowing* its
    # kept running backs fill those slots, then gets scored with the slots empty, while
    # best_vor -- which ignores need and hoards -- is scored as though its picks were the
    # real starters. That penalises the engine against its own naive baseline, in the
    # backtest CLAUDE.md says gates every engine change.
    owned: list[tuple[int, str]] = [
        (pick.pick, pick.player_key) for pick in state.picks_by_team(my_team.team_key)
    ]
    owned += [(0, keeper.player_key) for keeper in state.keepers_for(my_team.team_key)]

    players: list[tuple[int, str, str]] = []
    roster: list[PlayerValuation] = []
    total_vor = 0.0
    total_points = 0.0
    for pick_number, player_key in sorted(owned):
        valuation = assistant.valuations.valuations.get(player_key)
        if valuation is not None:
            roster.append(valuation)
            total_vor += assistant.levels.vor(valuation)
            total_points += valuation.projected_points
        players.append(
            (
                pick_number,
                assistant._player_name(player_key),
                valuation.position if valuation else "?",
            )
        )
    settings = record.league.settings
    assert settings is not None  # enforced by DraftRecord construction
    return RosterResult(
        policy=policy,
        lineup_points=_lineup_points(roster, settings),
        total_vor=total_vor,
        total_points=total_points,
        players=tuple(players),
    )


def auction_counterfactual(
    record: DraftRecord,
    snapshot: Snapshot,
    *,
    policy: str,
    display_limit: int = 8,
    follow_from: int | None = None,
) -> RosterResult:
    """Replay a recorded auction, letting ``policy`` decide which sales I take.

    Walks the sales in nomination order at their recorded prices. At each one the policy
    either outbids (paying one dollar more than the price that actually won, or exactly
    what I paid where I was already the buyer) or lets it stand. See the module docstring
    for what holding prices fixed does and does not buy you.

    ``follow_from`` replays history verbatim up to that pick number and only then hands
    over to the policy. Without it a greedy policy commits its whole budget to the first
    few sales -- the studs are nominated first -- and never reaches the tight-money endgame
    where the advice is actually load-bearing. Pinning the opening lets a run ask the
    narrower and more useful question: given the hole I had already dug by pick N, was
    there a way out of it?
    """
    if policy not in AUCTION_POLICIES:
        raise ValueError(f"Unknown policy {policy!r}; expected one of {AUCTION_POLICIES}.")
    if not record.is_auction:
        raise ValueError("auction_counterfactual is for auctions; use counterfactual().")
    my_team = record.my_team
    if my_team is None:
        raise ValueError("Record does not identify my team; nothing to compare against.")

    league, state = build_state(record)
    assistant = Assistant.build(league, state, snapshot)
    settings = record.league.settings
    assert settings is not None  # enforced by DraftRecord construction

    for pick in sorted(record.picks):
        # `cost or 0` would be wrong here: Yahoo lists kept players inside draftresults
        # with no sale price at all, so a legitimate row arrives with cost=None. Read as
        # $0 it made every policy able to take a rival's stud for $1, and -- because a
        # $0 bid trips the floor in _auction_choice -- handed my own unpriced player to a
        # rival while `actual` kept him, tilting every delta in the engine's favour.
        priced = pick.cost is not None
        price = pick.cost or 0
        was_mine = pick.team_key == my_team.team_key

        # The recorded sale stands unless the policy actively changes it.
        buyer, cost = pick.team_key, pick.cost
        replaying = policy == "actual" or (follow_from is not None and pick.pick < follow_from)
        if not replaying and priced:
            # An unpriced sale is not biddable: there is no price to beat, and it is
            # nearly always a keeper folded into draftresults. Leave it where it lies.
            bid = _auction_choice(assistant, policy, pick, price, was_mine, display_limit)
            if bid is not None:
                buyer, cost = my_team.team_key, bid
            elif was_mine:
                # I passed on a player I really bought. The record does not say who the
                # underbidder was, so a rival who can actually seat him stands in -- what
                # matters is that he leaves the pool and the money stays accounted.
                #
                # If no rival can, he is seated *unpaid* rather than charged to anyone.
                # Handing him back to me would reverse the decline at the recorded price
                # and breach the two hard constraints the policy declined on: forcing that
                # path on the fixture put 16 players and $257 on a 15-slot, $200 roster.
                stand_in = _deepest_pocket(state, my_team.team_key, price)
                buyer, cost = (stand_in, price) if stand_in else (pick.team_key, None)

        state.apply_sync(
            [
                DraftPick(
                    pick=pick.pick,
                    round=pick.round,
                    team_key=buyer,
                    player_key=pick.player_key,
                    cost=cost,
                )
            ],
            timestamp=0.0,
        )

    # Every policy has to leave the room with a full roster. A policy that simply passes
    # would otherwise "win" by fielding seven players and a pile of unspent cash, and the
    # lineup metric would quietly score its empty slots as zero. The real endgame is a run
    # of dollar players out of the undrafted pool, so that is what this buys.
    if policy != "actual":
        # Numbered past the record's last sale: these are endgame buys, and borrowing a
        # low free slot would print them as if they had happened in the first round.
        last = max((pick.pick for pick in record.picks), default=0)
        _fill_roster(assistant, state, settings, my_team.team_key, start=last + 1)

    players: list[tuple[int, str, str]] = []
    roster: list[PlayerValuation] = []
    total_vor = 0.0
    total_points = 0.0
    owned: list[tuple[int, str]] = [
        (pick.pick, pick.player_key) for pick in state.picks_by_team(my_team.team_key)
    ]
    # Keepers are roster spots too, and picks_by_team does not report them. A keeper
    # league scored without them looks short a starter and reads as a worse roster.
    owned += [(0, keeper.player_key) for keeper in state.keepers_for(my_team.team_key)]
    for pick_number, player_key in sorted(owned):
        valuation = assistant.valuations.valuations.get(player_key)
        if valuation is not None:
            roster.append(valuation)
            total_vor += assistant.levels.vor(valuation)
            total_points += valuation.projected_points
        players.append(
            (
                pick_number,
                assistant._player_name(player_key),
                valuation.position if valuation else "?",
            )
        )

    return RosterResult(
        policy=policy,
        lineup_points=_lineup_points(roster, settings),
        total_vor=total_vor,
        total_points=total_points,
        players=tuple(players),
        # DraftState.spent is authoritative: it also charges keeper salaries, which are
        # money the team can no longer bid with. A hand-kept tally of board costs omitted
        # them, so the legality assertion built on this number had no teeth in a keeper
        # auction -- it could pass on a roster that was genuinely over budget.
        spent=state.spent(my_team.team_key),
    )


def _auction_choice(
    assistant: Assistant,
    policy: str,
    pick: DraftPick,
    price: int,
    was_mine: bool,
    display_limit: int,
) -> int | None:
    """What I would bid on this sale, or None to let it go.

    Two engine policies, because "following the tool" has two meanings and they measure
    different things:

    * ``engine`` looks the nominated player up, which is how an auction is actually played
      and what ``Assistant.evaluate`` exists for -- the question is never "who is best" but
      "someone just said his name, what is he worth to me". It sees every player, so what
      constrains it is purely ``bid_to``: the *valuation* and its caps.
    * ``engine_list`` buys only from the visible short list. It adds the display to the
      test, so the gap between the two is what a change to *ranking* can move and nothing
      else. Ranking a player higher cannot change what he is worth; it can only change
      whether you ever saw him.

    Requiring short-list membership of *both* would fold the two together and understate
    the engine badly -- a nominated player is rarely in anyone's top eight at that exact
    moment, so the policy would pass on nearly everything and end the draft holding cash.
    """
    state = assistant.state
    my_team = state.my_team
    if my_team is None or state.slots_remaining(my_team.team_key) <= 0:
        return None
    # One dollar more than the winning price takes the player; where I already won, the
    # price I paid is what it costs to keep him.
    bid = price if was_mine else price + 1
    if bid <= 0 or bid > state.max_bid(my_team.team_key):
        return None

    if policy == "best_par":
        if assistant.dollars is None:
            return None
        return bid if assistant.dollars.value_of(pick.player_key) >= bid else None

    limit = display_limit if policy == "engine_list" else _WHOLE_POOL
    for recommendation in assistant.auction_recommendations(limit=limit):
        if recommendation.valuation.player_key == pick.player_key:
            return bid if recommendation.bid_to >= bid else None
    return None


def _fill_roster(
    assistant: Assistant, state, settings: LeagueSettings, my_team_key: str, *, start: int
) -> int:
    """Buy dollar players until the roster is full.

    Unfilled *starting* positions go first and only then bench depth, because that is
    both what a human does with four slots and $6 left and what the lineup metric
    rewards -- a sixth running back cannot cover an empty tight end.
    """
    position_of = assistant.position_of
    # Which positions are worth bench depth at all. Flex-eligible ones first -- a spare
    # there can start any week -- but falling back to every startable position, because a
    # league with no flex slot would otherwise have an empty set and no bench rule at all.
    flex_eligible: set[str] = set()
    startable: set[str] = set()
    for slot in settings.starting_slots:
        startable |= set(slot.eligible_positions)
        if len(slot.eligible_positions) > 1:
            flex_eligible |= set(slot.eligible_positions)
    bench_worthy = flex_eligible or startable

    spent = 0
    while state.slots_remaining(my_team_key) > 0:
        counts = state.roster_counts(my_team_key, position_of)
        wanted: list[str] = []
        for slot in settings.starting_slots:
            if len(slot.eligible_positions) != 1:
                continue
            position = next(iter(slot.eligible_positions))
            if counts.get(position, 0) < slot.count:
                wanted.append(position)

        # Assistant.available() already filters against state.drafted_player_keys on this
        # same DraftState, so re-filtering here would be dead work and a second opinion
        # about what "available" means.
        available = assistant.available()
        if not available:
            break
        # Tiers, tried in order. An empty tier falls through to the next rather than
        # ending the fill: an unfillable starter slot (no quarterback left on the board)
        # must not strand the bench slots behind it.
        #
        # The order is the whole point. A position whose only starting slot is filled and
        # that no flex accepts can never start again, so a spare there is worth nothing --
        # and best-VOR would take one anyway, because kickers and defenses carry no stat
        # projection and score exactly 0.0, which *beats* a leftover receiver's negative
        # VOR. This was once a single filter with a bare `pool = available` fallback, so a
        # league with no flex slot (empty `bench_worthy`, so the comprehension always
        # yielded []) skipped straight to the last resort and hoarded kickers: 916.8 ->
        # 729.2 lineup points. The scoring tier in between is what that league lands on now.
        scoring = [v for v in available if v.projected_points > 0]
        tiers = (
            [v for v in scoring if v.position in wanted] if wanted else [],
            [v for v in available if v.position in wanted] if wanted else [],
            [v for v in scoring if v.position in bench_worthy],
            scoring,
            available,
        )
        pool = next((tier for tier in tiers if tier), [])
        if not pool:
            break
        best = max(pool, key=lambda v: assistant.levels.vor(v))

        if state.max_bid(my_team_key) < 1:
            break
        spent += 1
        state.apply_sync(
            [
                DraftPick(
                    pick=start + spent - 1,
                    round=0,
                    team_key=my_team_key,
                    player_key=best.player_key,
                    cost=1,
                )
            ],
            timestamp=0.0,
        )
    return spent


def _deepest_pocket(state, my_team_key: str, price: int) -> str | None:
    """A rival who could actually have outbid me, or None if nobody could.

    Restricted to rivals who can seat the player *and still fill their own remaining
    slots*, because an unrestricted ``max(budget_remaining)`` is not merely cosmetic
    bookkeeping. It seated players on full rosters and charged teams that could not pay:
    on this repo's own fixture it left two teams holding 19 players in a 15-man league and
    one at -$21. Those values flow into ``league_money_remaining`` (an unclamped sum) and
    ``league_slots_remaining`` (clamped at zero, so it understates), which drive the
    inflation the engine reads on every later sale -- and the error compounds, because
    cheaper inflation means more declines, which means more of these.

    The test is ``max_bid``, not ``budget_remaining``: a rival with five open slots and $30
    cannot really spend $30, because four of those slots still need a dollar each. Using
    the looser figure left teams with $0 against open slots, which is the same distortion
    one step smaller.
    """
    rivals = [
        team
        for team in state.teams
        if team.team_key != my_team_key and state.max_bid(team.team_key) >= price
    ]
    if not rivals:
        return None
    return max(rivals, key=lambda team: state.max_bid(team.team_key)).team_key


def _lineup_points(roster: list[PlayerValuation], settings: LeagueSettings) -> float:
    """Projected points of the best legal starting lineup from this roster.

    Delegates to ``engine.lineup.optimal_lineup``, which is exact. This used to fill
    dedicated slots and then flex slots greedily by points, and claimed in its docstring
    to be exact for every layout Yahoo offers -- it is not, and the counterexample is in
    that module's docstring. The number this returns can therefore only rise, never fall.

    All three policies in ``POLICIES`` are scored through here, so the comparison between
    them stays like-for-like whichever way the optimizer resolves a tie.
    """
    return lineup.optimal_lineup(roster, settings).total


def _my_choice(assistant: Assistant, policy: str, actual_key: str, drafted: set[str]) -> str:
    if policy == "actual":
        return actual_key
    if policy == "best_vor":
        available = assistant.available()
        if available:
            return max(available, key=lambda v: assistant.levels.vor(v)).player_key
        # Same exhausted-pool rule as the engine branch: falling back to the actual
        # pick is only legal if this replay has not already seated him elsewhere --
        # a duplicate would inflate exactly the baseline the engine is compared to.
        return actual_key if actual_key not in drafted else _fallback_by_adp(assistant, drafted)
    # policy == "engine"
    recommendations = assistant.snake_recommendations(limit=1)
    if recommendations:
        return recommendations[0].valuation.player_key
    # Pool exhausted from the engine's point of view (players it cannot value); fall
    # back to history rather than skipping a turn.
    return actual_key if actual_key not in drafted else _fallback_by_adp(assistant, drafted)


def _scripted_choice(assistant: Assistant, script: list[str], drafted: set[str]) -> str:
    """The opponent's next actual pick that is still available."""
    while script:
        candidate = script.pop(0)
        if candidate not in drafted:
            return candidate
    return _fallback_by_adp(assistant, drafted)


def _fallback_by_adp(assistant: Assistant, drafted: set[str]) -> str:
    available = assistant.available()
    if not available:
        raise RuntimeError("Replay exhausted the valued player pool entirely.")
    return min(available, key=lambda v: v.adp).player_key
