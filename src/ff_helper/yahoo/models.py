"""Typed views over the Yahoo Fantasy API.

These are deliberately plain dataclasses rather than pydantic models: they are constructed
only by ``parse.py``, which is the one place allowed to know how ugly Yahoo's JSON is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Yahoo NFL stat IDs we can score from projections. Anything not listed here (kicker
# distance buckets, team-defense tiers) is not projectable with enough accuracy to
# matter, so those positions fall back to a source's own projected point total.
STAT_PASS_YDS = 4
STAT_PASS_TD = 5
STAT_INT = 6
STAT_RUSH_YDS = 9
STAT_RUSH_TD = 10
STAT_REC = 11
STAT_REC_YDS = 12
STAT_REC_TD = 13
STAT_RET_TD = 15
STAT_TWO_PT = 16
STAT_FUM_LOST = 18

# Maps our normalized projection column names onto Yahoo stat IDs.
PROJECTION_STAT_IDS: dict[str, int] = {
    "pass_yds": STAT_PASS_YDS,
    "pass_td": STAT_PASS_TD,
    "int": STAT_INT,
    "rush_yds": STAT_RUSH_YDS,
    "rush_td": STAT_RUSH_TD,
    "rec": STAT_REC,
    "rec_yds": STAT_REC_YDS,
    "rec_td": STAT_REC_TD,
    "ret_td": STAT_RET_TD,
    "two_pt": STAT_TWO_PT,
    "fum_lost": STAT_FUM_LOST,
}

# Roster slots that do not represent a startable scoring position.
BENCH_SLOTS = {"BN", "IR", "IR+", "NA"}

# Which real positions can fill each flex-style slot.
FLEX_ELIGIBILITY: dict[str, frozenset[str]] = {
    "W/R": frozenset({"WR", "RB"}),
    "W/T": frozenset({"WR", "TE"}),
    "W/R/T": frozenset({"WR", "RB", "TE"}),
    "Q/W/R/T": frozenset({"QB", "WR", "RB", "TE"}),
    "FLEX": frozenset({"WR", "RB", "TE"}),
    "SUPERFLEX": frozenset({"QB", "WR", "RB", "TE"}),
}


@dataclass(frozen=True)
class RosterSlot:
    position: str
    count: int

    @property
    def is_starting(self) -> bool:
        return self.position not in BENCH_SLOTS

    @property
    def eligible_positions(self) -> frozenset[str]:
        """Real positions that can start in this slot."""
        return FLEX_ELIGIBILITY.get(self.position, frozenset({self.position}))


# Yahoo's default auction budget. Used only when the league settings do not state one.
DEFAULT_AUCTION_BUDGET = 200


@dataclass(frozen=True)
class LeagueSettings:
    roster_slots: tuple[RosterSlot, ...]
    stat_modifiers: dict[int, float]
    is_auction: bool
    auction_budget: int = DEFAULT_AUCTION_BUDGET
    # In-season settings. All optional, because the draft path never needed them and an
    # older league that does not publish one must not become unparseable. Raw Yahoo
    # spellings are kept rather than normalized here -- ``season.schedule`` is the one
    # place allowed to interpret them, the same way parse.py is the one place allowed to
    # know how ugly Yahoo's JSON is.
    waiver_type: str = ""  # "R" rolling list | "C" continual
    waiver_rule: str = ""  # "gametime" | "continual" | "all" ...
    uses_faab: bool = False
    faab_budget: int | None = None
    trade_end_date: str = ""  # ISO date, Yahoo's own spelling
    playoff_start_week: int | None = None
    num_playoff_teams: int | None = None

    @property
    def starting_slots(self) -> tuple[RosterSlot, ...]:
        return tuple(slot for slot in self.roster_slots if slot.is_starting)

    def starters_at(self, position: str) -> int:
        """How many of `position` a single team must start, counting flex slots.

        Flex slots are counted toward every position that can fill them, which
        intentionally overcounts -- replacement level is then softened in
        ``engine.replacement`` rather than pretending a flex belongs to one position.
        """
        return sum(
            slot.count for slot in self.starting_slots if position in slot.eligible_positions
        )

    @property
    def bench_size(self) -> int:
        return sum(slot.count for slot in self.roster_slots if slot.position == "BN")

    @property
    def roster_size(self) -> int:
        return sum(slot.count for slot in self.roster_slots if slot.position not in {"IR", "IR+"})


@dataclass(frozen=True)
class League:
    league_key: str
    league_id: str
    name: str
    num_teams: int
    season: str
    draft_status: str  # "predraft" | "drafting" | "postdraft"
    scoring_type: str
    settings: LeagueSettings | None = None
    # Where the season currently is. The draft path never asked, so these default to None
    # and every in-season caller must handle their absence rather than assume week 1.
    current_week: int | None = None
    start_week: int | None = None
    end_week: int | None = None
    is_finished: bool = False

    @property
    def is_drafting(self) -> bool:
        return self.draft_status == "drafting"

    @property
    def draft_complete(self) -> bool:
        return self.draft_status == "postdraft"


@dataclass(frozen=True)
class Team:
    team_key: str
    team_id: str
    name: str
    is_mine: bool = False
    draft_position: int | None = None


@dataclass(frozen=True)
class DraftPick:
    pick: int
    round: int
    team_key: str
    player_key: str
    cost: int | None = None

    def __lt__(self, other: DraftPick) -> bool:
        return self.pick < other.pick


@dataclass(frozen=True)
class KeptPlayer:
    """A player already on a roster before the draft starts.

    Before a draft, the only way a player sits on a team is if they were kept, which makes
    pre-draft rosters a reliable keeper source without needing a dedicated endpoint.
    """

    player_key: str
    team_key: str
    # Auction keeper salary, where the league assigns one.
    cost: int | None = None
    # Snake keeper round cost (the pick forfeited to keep them), where applicable.
    round: int | None = None
    # "yahoo" or "csv" -- shown in the UI so you can see where a keeper came from.
    source: str = "yahoo"


@dataclass(frozen=True)
class RosterEntry:
    """A player on a roster *during* the season, with the slot he is actually in.

    Deliberately a sibling of ``KeptPlayer`` rather than an extension of it. ``KeptPlayer``
    is premised on "before a draft, the only way a player sits on a team is if they were
    kept", which is what makes the keeper path safe to trust; that premise is false in
    week 6. The field that matters here is ``selected_position``, which ``parse_roster``
    discards because a keeper has no meaningful one.
    """

    player_key: str
    team_key: str
    week: int
    # The slot Yahoo has him in: "QB", "W/R/T", "BN", "IR" ...
    selected_position: str = ""

    @property
    def is_starting(self) -> bool:
        return bool(self.selected_position) and self.selected_position not in BENCH_SLOTS


@dataclass(frozen=True)
class Matchup:
    """One week's head-to-head, from one team's point of view.

    Stored once per *team*, not once per pairing -- so both sides of a game appear as two
    entries. Redundant on disk and worth it: every caller starts from "which team am I",
    and a pairing representation makes that a search instead of a lookup.
    """

    week: int
    team_key: str
    opponent_key: str
    points: float | None = None
    opponent_points: float | None = None
    projected_points: float | None = None
    opponent_projected: float | None = None
    is_playoffs: bool = False
    status: str = ""  # "preevent" | "midevent" | "postevent"

    @property
    def is_final(self) -> bool:
        return self.status == "postevent"


@dataclass(frozen=True)
class Transaction:
    """A completed add/drop/trade. The waiver market's revealed preference.

    ``bid`` is the winning FAAB amount where the league uses one -- the in-season analog of
    an auction sale price, and the input that lets ``engine.auction.room_premiums`` learn
    what this specific room overpays for.
    """

    transaction_key: str
    type: str  # "add" | "drop" | "add/drop" | "trade" | "commish"
    status: str
    timestamp: float | None = None
    added: tuple[str, ...] = ()  # player keys
    dropped: tuple[str, ...] = ()
    team_key: str = ""
    bid: int | None = None
    source_type: str = ""  # "waivers" | "freeagents"


@dataclass(frozen=True)
class DraftAnalysis:
    """Yahoo's own ADP data -- the single best predictor of a Yahoo draft room."""

    average_pick: float | None = None
    average_round: float | None = None
    average_cost: float | None = None
    percent_drafted: float | None = None


@dataclass(frozen=True)
class YahooPlayer:
    player_key: str
    player_id: str
    full_name: str
    team_abbr: str
    display_position: str
    eligible_positions: tuple[str, ...] = ()
    bye_week: int | None = None
    status: str = ""  # "" | "Q" | "O" | "IR" | "PUP" ...
    draft_analysis: DraftAnalysis = field(default_factory=DraftAnalysis)
    # In-season detail. ``status`` alone says "Q" where a weekly decision wants to know
    # *why* -- and percent_owned is the market's read on news, which is the earliest
    # waiver signal there is.
    status_full: str = ""  # "Questionable - Hamstring"
    injury_note: str = ""
    percent_owned: float | None = None

    @property
    def primary_position(self) -> str:
        """The position we rank this player at."""
        for position in self.eligible_positions:
            if position not in BENCH_SLOTS:
                return position
        return self.display_position

    @property
    def startable_positions(self) -> tuple[str, ...]:
        """Every real position this player can be slotted at, not just the primary one.

        Yahoo routinely lists a player as WR/RB, and that flexibility is exactly what a
        lineup optimizer trades on -- collapsing it to ``primary_position`` throws away
        the option before anything gets to use it.

        Flex *slots* are stripped. Yahoo puts "W/R/T" in this list alongside "RB", but a
        slot is not a position: which slots a player can fill is derived from his real
        positions via ``FLEX_ELIGIBILITY``, and leaving the slot names in would let a
        player claim a flex he is not actually eligible for in a league that spells its
        flex differently.
        """
        real = tuple(
            position
            for position in self.eligible_positions
            if position not in BENCH_SLOTS and position not in FLEX_ELIGIBILITY
        )
        return real or (self.display_position,)
