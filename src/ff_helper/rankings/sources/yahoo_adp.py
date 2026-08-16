"""Yahoo's own ADP, taken from the ``draft_analysis`` subresource.

This is the most important market signal in the app and is weighted accordingly. National
ADP describes drafts in general; Yahoo ADP describes drafts *on the platform your league
actually drafts on*, against opponents using Yahoo's default rankings in Yahoo's UI. When
the two disagree, Yahoo is the better predictor of what will happen in your room.

Yahoo publishes a mean pick but no standard deviation, so ``blend`` estimates one.
"""

from __future__ import annotations

from ff_helper.rankings.players import SourceRow, normalize_position, normalize_team
from ff_helper.yahoo.models import YahooPlayer

SOURCE = "yahoo"


def from_players(players: list[YahooPlayer]) -> list[SourceRow]:
    rows: list[SourceRow] = []
    for player in players:
        analysis = player.draft_analysis
        rows.append(
            SourceRow(
                name=player.full_name,
                position=normalize_position(player.primary_position),
                team=normalize_team(player.team_abbr),
                source=SOURCE,
                adp=analysis.average_pick,
                auction_cost=analysis.average_cost,
            )
        )
    return rows
