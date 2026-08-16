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

    @property
    def primary_position(self) -> str:
        """The position we rank this player at."""
        for position in self.eligible_positions:
            if position not in BENCH_SLOTS:
                return position
        return self.display_position
