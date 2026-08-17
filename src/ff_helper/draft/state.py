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

from collections import Counter
from dataclasses import dataclass, field

from ff_helper.yahoo.models import DraftPick, KeptPlayer, League, Team


@dataclass(frozen=True)
class BridgeSale:
    """One completed sale, already resolved to keys.

    Resolution happens before this point on purpose: an unresolvable buyer must never
    reach the board, because money charged to no team never leaves the room, and the
    inflation model then reads the league as richer than it is and overstates every
    remaining price.
    """

    player_key: str
    team_key: str
    cost: int | None = None


@dataclass
class BridgeDiff:
    """What one reading of the draft room changed."""

    applied: list[DraftPick] = field(default_factory=list)
    corrected: list[DraftPick] = field(default_factory=list)
    removed: list[DraftPick] = field(default_factory=list)
    unchanged: int = 0
    rejected: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.applied or self.corrected or self.removed)


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

    # Draft rounds -- how many picks each team makes. In a keeper league this is smaller
    # than roster_size, because kept players fill spots without using a pick.
    rounds: int = 15
    # Total roster spots per team, keepers included. Drives budget and slot maths.
    # Left at 0 it follows ``rounds``, so constructing a state with a custom round count
    # cannot silently leave the two disagreeing (which would make slots_remaining, and
    # therefore max_bid, measure against the wrong roster).
    roster_size: int = 0
    snake: bool = True

    # Live draft status (predraft / drafting / postdraft). It lives here rather than on
    # League because League is a frozen snapshot of what the API returned, and this is the
    # one field that changes underneath us while the app is running.
    _draft_status: str | None = None

    # Picks Yahoo has confirmed, keyed by overall pick number.
    synced: dict[int, DraftPick] = field(default_factory=dict)
    # Picks entered by hand, same keying. Used when the feed lags or stalls.
    manual: dict[int, DraftPick] = field(default_factory=dict)
    # Picks read off the Yahoo draft room by the bridge, same keying.
    bridge: dict[int, DraftPick] = field(default_factory=dict)

    # player_key -> the pick number that player was given, for bridge-sourced sales.
    #
    # This exists because a pick number is not an identity in an auction; it is a display
    # ordinal. The sale's identity is the player. Keying only by pick number means that if
    # the same player ever arrives under a different number -- the room reorders, a row
    # misparses once, the reader restarts and renumbers -- he lands on the board twice and
    # ``spent`` charges his buyer twice, because it sums board entries and never
    # deduplicates by player. Allocating a number once per player and reusing it makes
    # that impossible rather than unlikely.
    bridge_order: dict[str, int] = field(default_factory=dict)

    # Players already rostered before the draft. Kept separate from picks on purpose:
    # they occupy roster spots and money without occupying a pick number, and conflating
    # the two would corrupt the snake pick maths.
    keepers: list[KeptPlayer] = field(default_factory=list)

    # Bookkeeping surfaced in the UI.
    last_sync: float | None = None
    last_sync_error: str | None = None
    superseded: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.roster_size:
            self.roster_size = self.rounds

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
        """Merged view. Yahoo wins where both have an opinion.

        Precedence is manual < bridge < synced. The API is the system of record and wins
        outright. The bridge outranks a hand-entered pick because it is reading the room
        rather than remembering it -- but only ever for a pick number it was allocated,
        and ``apply_bridge`` refuses to allocate one that a human already owns for that
        player, so the two cannot silently shadow each other.
        """
        merged = dict(self.manual)
        merged.update(self.bridge)
        merged.update(self.synced)
        return merged

    def pick_for_player(self, player_key: str) -> int | None:
        """The pick number a player already occupies on the board, if any."""
        for number, pick in self.board.items():
            if pick.player_key == player_key:
                return number
        return None

    def next_free_pick(self) -> int:
        """Smallest pick number no writer has claimed.

        Shared by every writer so that a hand-entered sale and a bridge-read one can never
        be allocated the same slot. ``record_manual`` used to default to ``current_pick``,
        which is derived from how many picks exist rather than which numbers are free --
        with a bridge running that collides routinely, and a collision makes the human's
        entry vanish from the merged board with no money charged and no warning.
        """
        taken = set(self.synced) | set(self.manual) | set(self.bridge)
        number = 1
        while number in taken:
            number += 1
        return number

    @property
    def drafted_player_keys(self) -> set[str]:
        """Everyone unavailable: drafted this year, plus anyone kept.

        Keepers belong here because the effect is identical -- you cannot have them -- and
        leaving them out would mean recommending a player who was never on the board.
        """
        return {pick.player_key for pick in self.board.values()} | {
            keeper.player_key for keeper in self.keepers
        }

    def keepers_for(self, team_key: str) -> list[KeptPlayer]:
        """A team's keepers, minus any the draft feed has since reported as a pick.

        Yahoo lists kept players inside ``draftresults`` in some keeper leagues, and a
        pick can also be entered by hand. Counting a player as both a keeper and a pick
        would charge their salary twice and take two roster spots for one player, so the
        board wins wherever the two overlap.
        """
        on_board = {pick.player_key for pick in self.board.values()}
        return [
            keeper
            for keeper in self.keepers
            if keeper.team_key == team_key and keeper.player_key not in on_board
        ]

    def keeper_counts(self) -> dict[str, int]:
        """Keepers per team, counting every team -- including those who kept nobody.

        The zeros matter: a league where half the teams kept two and half kept none is
        uneven, and leaving the empty teams out of the tally would make it look uniform.
        Keepers the board has since claimed are excluded, matching ``keepers_for``.
        """
        on_board = {pick.player_key for pick in self.board.values()}
        counts = Counter(
            keeper.team_key for keeper in self.keepers if keeper.player_key not in on_board
        )
        return {team.team_key: counts.get(team.team_key, 0) for team in self.teams}

    def apply_keepers(self, kept: list[KeptPlayer]) -> None:
        """Attach keepers and shorten the draft accordingly.

        Kept players do not use a pick, so a 15-spot roster with 2 keepers drafts 13
        rounds. Where teams keep different numbers the most common count is used, since
        the snake pick maths needs one answer; ``keepers.from_yahoo`` warns when that
        happens. Note this stays approximate for the whole draft: nothing derives pick
        numbers back out of the feed, so a rival's countdown can be off even though
        ``my_picks`` uses my own keeper count and ``total_picks`` is summed per team.
        ``total_picks`` stays exact regardless, so an uneven league cannot end the draft
        early.

        Duplicates are dropped on the way in. One player cannot fill two roster spots or
        spend two salaries, so a list that names them twice -- a CSV listing a player under
        both teams of a trade, or a roster read taken after the draft opened -- must not be
        stored twice.
        """
        seen: set[str] = set()
        deduped: list[KeptPlayer] = []
        for keeper in kept:
            if keeper.player_key in seen:
                continue
            seen.add(keeper.player_key)
            deduped.append(keeper)

        self.keepers = deduped
        if not deduped:
            self.rounds = self.roster_size
            return

        counts = list(self.keeper_counts().values()) or [0]
        typical = Counter(counts).most_common(1)[0][0]
        self.rounds = max(1, self.roster_size - typical)

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
        """Picks that will actually be made, keepers excluded.

        Summed per team rather than ``num_teams * rounds``: ``rounds`` collapses uneven
        keeper counts to one number, and under-counting here makes ``is_complete`` true
        while picks are still coming -- which stops the poller mid-draft.
        """
        if not self.teams:
            return self.num_teams * self.rounds
        return sum(max(0, self.roster_size - count) for count in self.keeper_counts().values())

    @property
    def is_complete(self) -> bool:
        return self.picks_made >= self.total_picks

    # -- my turn -----------------------------------------------------------------------

    @property
    def my_picks(self) -> list[int]:
        """My turns, using *my* round count rather than the league-wide mode.

        If I kept fewer players than most of the league I draft more times than ``rounds``
        says, and using the collapsed number would drop my last picks off the board
        entirely -- no next pick, no countdown, and a VONA horizon computed against a gap
        that does not exist.
        """
        slot = self.my_slot
        if slot is None:
            return []
        team = self.my_team
        rounds = self.rounds
        if team is not None:
            rounds = max(1, self.roster_size - len(self.keepers_for(team.team_key)))
        return picks_for_slot(slot, self.num_teams, rounds, snake=self.snake)

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
        """Positions held by a team, using a player_key -> position map.

        Includes keepers: a kept running back fills a starting slot exactly as a drafted
        one does, so leaving them out would have the engine push a position you are
        already full at.
        """
        counts: dict[str, int] = {}
        player_keys = [pick.player_key for pick in self.picks_by_team(team_key)]
        player_keys += [keeper.player_key for keeper in self.keepers_for(team_key)]

        for player_key in player_keys:
            position = position_of.get(player_key)
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

    def record_manual(
        self,
        player_key: str,
        *,
        pick: int | None = None,
        cost: int | None = None,
        team_key: str | None = None,
    ) -> DraftPick:
        """Mark a player drafted by hand, for when the feed stalls mid-draft.

        In an auction the ``cost`` and ``team_key`` matter as much as the player: budgets
        drive every dollar value, so a pick recorded without its price would quietly
        corrupt the inflation model.
        """
        # A free slot, not ``current_pick``. The old default counted how many picks exist
        # rather than which numbers are unclaimed, so with a bridge running it landed on a
        # slot the bridge already held -- and since the bridge outranks manual in ``board``,
        # the sale disappeared entirely: no money charged, no roster spot, no warning.
        target = pick if pick is not None else self.next_free_pick()

        if team_key is None and not self.is_auction:
            # Snake drafts have a deterministic owner for every pick number.
            slot = self._slot_for_pick(target)
            team = next((t for t in self.teams if t.draft_position == slot), None)
            team_key = team.team_key if team else ""

        entry = DraftPick(
            pick=target,
            round=(target - 1) // self.num_teams + 1,
            team_key=team_key or "",
            player_key=player_key,
            cost=cost,
        )
        self.manual[target] = entry
        return entry

    def apply_bridge(
        self,
        sales: list[BridgeSale],
        *,
        timestamp: float,
        shrink_tolerance: int = 2,
    ) -> BridgeDiff:
        """Fold in a full reading of the draft room. A snapshot, not an append.

        Every call carries the complete set of sales the reader can see, so this has to be
        able to *remove* as well as add -- otherwise a sale that changes pick number
        between readings stays on the board twice and its buyer is charged twice.

        A payload that shrinks is refused rather than applied. Sales do not un-happen, so a
        board that lost rows almost always means the reading failed -- a filter got
        applied, the tab navigated, a selector stopped matching -- and acting on it would
        hand money back to teams that already spent it. ``shrink_tolerance`` allows a
        row or two of jitter; beyond that the whole payload is rejected and the board is
        left exactly as it was.
        """
        seen: dict[str, BridgeSale] = {}
        for sale in sales:
            # Last wins: a repeated player in one payload is a reading artefact, and
            # charging him twice is the failure this whole method exists to prevent.
            seen[sale.player_key] = sale

        removed: list[DraftPick] = []
        gone = [key for key in self.bridge_order if key not in seen]
        if len(gone) > shrink_tolerance:
            return BridgeDiff(
                rejected=(
                    f"reading dropped {len(gone)} of {len(self.bridge_order)} sales; "
                    "treating it as a failed read rather than removing them"
                )
            )

        applied: list[DraftPick] = []
        corrected: list[DraftPick] = []
        unchanged = 0

        for player_key, sale in seen.items():
            number = self.bridge_order.get(player_key)
            if number is None:
                # A pick the human already entered for this player keeps its number, so
                # the two reconcile into one entry instead of racing for a slot.
                number = self.pick_for_player(player_key) or self.next_free_pick()
                self.bridge_order[player_key] = number

            entry = DraftPick(
                pick=number,
                round=(number - 1) // self.num_teams + 1,
                team_key=sale.team_key,
                player_key=player_key,
                cost=sale.cost,
            )
            previous = self.bridge.get(number)
            self.bridge[number] = entry
            # A hand-entered pick for this player has served its purpose.
            self.manual.pop(number, None)

            if previous is None:
                applied.append(entry)
            elif previous.cost != entry.cost or previous.team_key != entry.team_key:
                corrected.append(entry)
            else:
                unchanged += 1

        for player_key in gone:
            number = self.bridge_order.pop(player_key)
            entry = self.bridge.pop(number, None)
            if entry is not None:
                removed.append(entry)

        self.last_sync = timestamp
        self.last_sync_error = None
        return BridgeDiff(
            applied=applied, corrected=corrected, removed=removed, unchanged=unchanged
        )

    # -- auction budgets ---------------------------------------------------------------

    @property
    def is_auction(self) -> bool:
        settings = self.league.settings
        return bool(settings and settings.is_auction)

    @property
    def budget(self) -> int:
        settings = self.league.settings
        return settings.auction_budget if settings else 0

    def spent(self, team_key: str) -> int:
        """Dollars a team has already committed, keeper salaries included.

        A keeper's salary is money that team can no longer bid with, so omitting it would
        overstate their budget and, through the inflation model, over-value everyone left.

        Every player is charged exactly once, at the best price known for them. That last
        part matters: ``keepers_for`` lets the board win on overlap so a kept player never
        occupies two roster spots, but Yahoo lists kept players in ``draftresults`` with no
        sale price -- because no sale happened -- so taking the board's cost blindly would
        drop the salary to zero. Deduplicating must not turn double-counting into
        zero-counting.
        """
        salaries = {
            keeper.player_key: keeper.cost
            for keeper in self.keepers
            if keeper.team_key == team_key and keeper.cost is not None
        }

        drafted = 0
        for pick in self.board.values():
            if pick.team_key != team_key:
                continue
            cost = pick.cost if pick.cost is not None else salaries.get(pick.player_key)
            drafted += cost or 0

        kept = sum(keeper.cost or 0 for keeper in self.keepers_for(team_key))
        return drafted + kept

    def budget_remaining(self, team_key: str) -> int:
        return self.budget - self.spent(team_key)

    def slots_filled(self, team_key: str) -> int:
        """Roster spots taken, keepers included -- they take up a spot like anyone else."""
        return len(self.picks_by_team(team_key)) + len(self.keepers_for(team_key))

    def slots_remaining(self, team_key: str) -> int:
        """Open roster spots. Measured against roster_size, not rounds.

        Using rounds here would double-count keepers: they are already in slots_filled,
        and rounds has been reduced to exclude them.
        """
        return max(0, self.roster_size - self.slots_filled(team_key))

    def max_bid(self, team_key: str) -> int:
        """The most a team can bid and still fill every roster spot at $1 apiece.

        This is a hard constraint, not advice: bid past it and you cannot complete a
        legal roster.
        """
        remaining_slots = self.slots_remaining(team_key)
        if remaining_slots <= 0:
            return 0
        return max(0, self.budget_remaining(team_key) - (remaining_slots - 1))

    def my_max_bid(self) -> int:
        team = self.my_team
        return self.max_bid(team.team_key) if team else 0

    def league_money_remaining(self) -> int:
        """Total dollars still unspent across every team -- the pool chasing what is left."""
        return sum(self.budget_remaining(team.team_key) for team in self.teams)

    def league_slots_remaining(self) -> int:
        return sum(self.slots_remaining(team.team_key) for team in self.teams)

    def undo_last_manual(self) -> DraftPick | None:
        if not self.manual:
            return None
        latest = max(self.manual)
        return self.manual.pop(latest)

    def staleness(self, now: float) -> float | None:
        """Seconds since the last successful sync, or None if we have never synced."""
        return None if self.last_sync is None else now - self.last_sync
