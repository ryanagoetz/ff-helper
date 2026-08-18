"""Shared synthetic-league builders.

A full offline league -- player pool, source rows, settings, teams -- used by the web,
auction, keeper, bridge, and backtest tests. Kept out of any test module so importing it
never runs another file's fixtures.
"""

from __future__ import annotations

from ff_helper.rankings.cache import Snapshot
from ff_helper.rankings.players import SourceRow
from ff_helper.yahoo.models import (
    STAT_REC,
    STAT_REC_TD,
    STAT_REC_YDS,
    STAT_RUSH_TD,
    STAT_RUSH_YDS,
    DraftAnalysis,
    League,
    LeagueSettings,
    RosterSlot,
    Team,
    YahooPlayer,
)

NUM_TEAMS = 12
MY_SLOT = 5

# A plausible positional shape: plenty of WR/RB, a steep TE cliff, flat QBs.
POSITION_POOL = {
    "QB": (18, 300.0, 6.0),
    "RB": (60, 290.0, 4.2),
    "WR": (70, 285.0, 3.6),
    "TE": (20, 230.0, 11.0),
    "K": (14, 0.0, 0.0),
    "DEF": (14, 0.0, 0.0),
}


def build_snapshot() -> Snapshot:
    players: list[YahooPlayer] = []
    rows: list[SourceRow] = []
    adp_counter = 1.0

    # Interleave positions so ADP order looks like a real board rather than blocks.
    buckets = {pos: [] for pos in POSITION_POOL}
    for position, (count, top, decay) in POSITION_POOL.items():
        for index in range(count):
            buckets[position].append((index, top - index * decay))

    ordering: list[tuple[str, int, float]] = []
    for position in ("RB", "WR", "QB", "TE"):
        for index, points in buckets[position]:
            ordering.append((position, index, points))
    ordering.sort(key=lambda item: -item[2])
    for position in ("K", "DEF"):
        ordering.extend((position, index, points) for index, points in buckets[position])

    for position, index, points in ordering:
        key = f"461.p.{position}{index}"
        adp = adp_counter
        adp_counter += 1.0
        players.append(
            YahooPlayer(
                player_key=key,
                player_id=f"{position}{index}",
                full_name=f"{position} Player{index}",
                team_abbr="FA",
                display_position=position,
                eligible_positions=(position,),
                bye_week=(index % 14) + 4,
                draft_analysis=DraftAnalysis(average_pick=adp),
            )
        )
        rows.append(
            SourceRow(
                name=f"{position} Player{index}",
                position=position,
                team="FA",
                source="ffc",
                adp=adp,
                adp_stdev=max(1.5, adp * 0.3),
                ecr=adp,
                tier=index // 5 + 1,
            )
        )
        if position in {"QB", "RB", "WR", "TE"}:
            # Give the skill positions real stat lines so scoring has something to work on.
            rows.append(
                SourceRow(
                    name=f"{position} Player{index}",
                    position=position,
                    team="FA",
                    source="fantasypros",
                    stats={
                        "rush_yds": points * 2.0 if position == "RB" else 0.0,
                        "rush_td": points / 40.0 if position == "RB" else 0.0,
                        "rec": points / 3.0 if position in {"WR", "TE", "RB"} else 0.0,
                        "rec_yds": points * 2.4 if position in {"WR", "TE"} else 0.0,
                        "rec_td": points / 35.0 if position in {"WR", "TE"} else 0.0,
                    },
                )
            )

    return Snapshot(league_key="461.l.1", fetched_at=0.0, players=players, rows=rows)


def build_league() -> League:
    settings = LeagueSettings(
        roster_slots=(
            RosterSlot("QB", 1),
            RosterSlot("RB", 2),
            RosterSlot("WR", 2),
            RosterSlot("TE", 1),
            RosterSlot("W/R/T", 1),
            RosterSlot("K", 1),
            RosterSlot("DEF", 1),
            RosterSlot("BN", 6),
        ),
        stat_modifiers={
            STAT_RUSH_YDS: 0.1,
            STAT_RUSH_TD: 6.0,
            STAT_REC: 1.0,
            STAT_REC_YDS: 0.1,
            STAT_REC_TD: 6.0,
        },
        is_auction=False,
    )
    return League(
        league_key="461.l.1",
        league_id="1",
        name="Synthetic League",
        num_teams=NUM_TEAMS,
        season="2026",
        draft_status="drafting",
        scoring_type="head",
        settings=settings,
    )


def build_teams(*, my_slot: int = MY_SLOT, num_teams: int = NUM_TEAMS) -> list[Team]:
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
