"""One week's value for one player, re-scored under your league.

The in-season sibling of ``rankings/blend.py``, and a separate dataclass rather than a
reuse of ``PlayerValuation`` because that one requires ``adp`` and ``adp_stdev``. Draft
position is not a fact about week 7; faking it to satisfy a constructor would put two
meaningless numbers in front of every weekly decision.

What carries over unchanged is the rule that matters most: **a source's own point total is
discarded and the stat line is re-scored under your league's modifiers.** A weekly
projection of 14.2 points assumed somebody's PPR setting, and start-or-sit decisions turn
on smaller margins than a draft pick does.

Three things are modelled separately here on purpose, because collapsing them is how a
lineup gets picked for the wrong reason:

- **Points** -- what the stat line is worth under your scoring.
- **Play probability** -- whether he takes the field at all, which is a per-*game*
  question and therefore not the same as ``blend.availability_of``'s per-season one.
- **Adjustments** -- what the news did to the number, each carrying the sentence that
  caused it. Empty until the news bridge lands; the field exists now so that when it does,
  there is exactly one way to move a projection and it leaves a record.

``points_stdev`` here means *source disagreement* and nothing else. How much a player's
real weekly score varies around his projection is a different quantity with a different
cause, and it belongs to ``engine/winprob.py`` -- mixing them would make a player two
sources happen to agree on look like a safe start.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ff_helper.engine.scoring import score_row
from ff_helper.rankings.players import PlayerRegistry
from ff_helper.rankings.sources.weekly_csv import WeeklyProjection
from ff_helper.season.news.store import NewsStore
from ff_helper.yahoo.models import LeagueSettings, YahooPlayer

# Chance a player with this status takes the field *this week*. Deliberately not derived
# from ``blend._EXPECTED_GAMES_MISSED``, which answers "what fraction of a season does he
# play" and correctly says a questionable tag costs nothing across seventeen games. Across
# one game it costs plenty, and a shared constant would force one of the two to be wrong.
#
# "D" and "Q" are the judgement calls. A doubtful player who does suit up is usually
# limited, so 0.35 prices the snap count as well as the coin flip; questionable is close
# to a coin flip on activity but barely reduces output when active, hence 0.80 rather
# than 0.50. Both are candidates for fitting once enough weeks are on disk.
_PLAY_PROBABILITY: dict[str, float] = {
    "O": 0.0,
    "IR": 0.0,
    "IR+": 0.0,
    "IR-R": 0.0,
    "NA": 0.0,
    "PUP": 0.0,
    "NFI": 0.0,
    "SUSP": 0.0,
    "D": 0.35,
    "Q": 0.80,
    "P": 0.95,
}


def play_probability(status: str) -> float:
    """Chance this player is active this week, from his Yahoo injury status.

    Unknown statuses read as healthy. That is the safe direction: an unrecognized tag
    silently zeroing a starter would be a far worse failure than one that does nothing,
    and the tag itself is still printed beside the recommendation.
    """
    return _PLAY_PROBABILITY.get(status.strip().upper(), 1.0)


@dataclass(frozen=True)
class Adjustment:
    """One news-driven change to a projection, with its evidence attached.

    The quote is not decoration. A multiplier you cannot trace back to a sentence is a
    number nobody can argue with, and the whole reason news is allowed near a projection
    at all is that every move it makes is visible and reversible.
    """

    flag: str
    multiplier: float
    quote: str
    url: str = ""
    source: str = ""
    observed_at: float | None = None


@dataclass(frozen=True)
class WeeklyValuation:
    """What one player is worth in one week, under this league's scoring."""

    player_key: str
    name: str
    position: str
    team: str
    week: int
    projected_points: float
    # Every real position he can be slotted at. Kept as a tuple rather than collapsed to
    # ``position`` because a WR/RB is worth more to a lineup than either alone, and the
    # optimizer is the thing that cashes that in.
    eligible_positions: tuple[str, ...] = ()
    # Spread of *source* opinion. Zero with a single source; see the module docstring for
    # why this is not the same as how much he actually varies week to week.
    points_stdev: float = 0.0
    # The re-scored stat line, before availability and before news.
    base_points: float = 0.0
    # After Yahoo's injury status but before any article touched it. This is the number
    # the news section compares against: printing base_points there would credit news with
    # a cut that Yahoo's own designation had already made.
    pre_news_points: float = 0.0
    adjustments: tuple[Adjustment, ...] = ()
    opponent: str = ""
    is_home: bool | None = None
    is_bye: bool = False
    status: str = ""
    status_full: str = ""
    percent_owned: float | None = None
    availability: float = 1.0
    # True when the number came from the exporter's own point total rather than a stat
    # line re-scored under your league. Always worth printing.
    points_estimated: bool = False
    sources: tuple[str, ...] = ()

    @property
    def is_playable(self) -> bool:
        """False for a bye or a player already ruled out -- never start him, never rank him."""
        return not self.is_bye and self.availability > 0.0

    @property
    def effective_multiplier(self) -> float:
        """Everything that scaled the stat line: injury status and news together."""
        if not self.base_points:
            return 1.0
        return self.projected_points / self.base_points

    @property
    def news_multiplier(self) -> float:
        """What the *news* did, with Yahoo's own injury status factored out.

        Not the product of ``adjustments``. The availability half of the news is
        reconciled against Yahoo's designation by taking the more pessimistic rather than
        multiplied, so the product of the flags can differ from the factor applied -- an
        article calling a player questionable whom Yahoo already lists as questionable
        changes nothing, and printing 0.80 there would claim a cut that did not happen.
        """
        if not self.pre_news_points:
            return 1.0
        return self.projected_points / self.pre_news_points


@dataclass
class WeeklyBlendResult:
    valuations: dict[str, WeeklyValuation] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def by_position(self, position: str) -> list[WeeklyValuation]:
        return sorted(
            (v for v in self.valuations.values() if position in v.eligible_positions),
            key=lambda v: -v.projected_points,
        )

    @property
    def ordered(self) -> list[WeeklyValuation]:
        return sorted(self.valuations.values(), key=lambda v: -v.projected_points)


def weekly_blend(
    registry: PlayerRegistry,
    projections: list[WeeklyProjection],
    settings: LeagueSettings,
    week: int,
    *,
    trust_source_points: frozenset[str] = frozenset(),
    news: NewsStore | None = None,
    as_of: float | None = None,
) -> WeeklyBlendResult:
    """Fold this week's projections into one valuation per player.

    ``trust_source_points`` names positions where the exporter's own point total may be
    used when no scoreable stat line exists. It is empty by default, and that default is
    the load-bearing part: accepting someone else's total is accepting someone else's
    scoring, which is the thing this whole layer exists to replace.

    The honest exception is kickers and defenses, whose scoring categories have no Yahoo
    stat IDs here at all -- so they have no stat line to re-score and fall through to
    nothing. Passing ``frozenset({"K", "DEF"})`` trades exactness for coverage on the two
    positions where the alternative is having no opinion whatsoever. It is opt-in, and it
    reports itself.

    ``news`` applies the captured article flags. ``as_of`` restricts them to what had been
    seen by a moment in time, which is what lets a backtest ask what Tuesday's digest would
    have said without Thursday's news leaking into the answer. Passing no store at all is
    the switch for running with news off entirely.
    """
    result = WeeklyBlendResult()

    # The crosswalk speaks SourceRow, so hand it the rows and map its answer back to the
    # wrappers by identity. Matching on name instead would have to reconstruct the
    # report's key format, and re-deriving a grouping the crosswalk already computed is
    # how the two quietly disagree about a player later.
    by_row: dict[int, WeeklyProjection] = {id(p.row): p for p in projections}
    grouped, report = registry.crosswalk([p.row for p in projections])

    trusted_used: list[str] = []
    unscoreable: list[str] = []

    for player_key, rows in grouped.items():
        entries = [by_row[id(row)] for row in rows if id(row) in by_row]
        if not entries:
            continue
        player = registry.by_key.get(player_key)
        if player is None:
            continue

        valuation = _combine(
            player,
            entries,
            settings,
            week,
            trust_source_points=trust_source_points,
            trusted_used=trusted_used,
            unscoreable=unscoreable,
            news=news,
            as_of=as_of,
        )
        if valuation is not None:
            result.valuations[player_key] = valuation

    if report.unmatched:
        result.notes.append(
            f"{len(report.unmatched)} weekly projection rows did not match a Yahoo player"
        )
    if trusted_used:
        result.notes.append(
            f"WARNING: {len(trusted_used)} players were valued from the export's own point "
            f"total rather than your league's scoring ({', '.join(sorted(trusted_used)[:6])}"
            + (" ..." if len(trusted_used) > 6 else "")
            + ") -- that total carries the exporter's scoring, not yours"
        )
    if unscoreable:
        plural = "player has" if len(unscoreable) == 1 else "players have"
        result.notes.append(
            f"{len(unscoreable)} {plural} no scoreable stat line this week and went "
            f"unvalued ({', '.join(sorted(unscoreable)[:6])}"
            + (" ..." if len(unscoreable) > 6 else "")
            + ") -- pass trust_source_points to value them from the export's own total"
        )

    return result


def _article_url(news: NewsStore, article_id: str) -> str:
    article = news.article(article_id)
    return article.url if article else ""


def _combine(
    player: YahooPlayer,
    entries: list[WeeklyProjection],
    settings: LeagueSettings,
    week: int,
    *,
    trust_source_points: frozenset[str],
    trusted_used: list[str],
    unscoreable: list[str],
    news: NewsStore | None = None,
    as_of: float | None = None,
) -> WeeklyValuation | None:
    """Merge one player's weekly rows into a single valuation."""
    position = player.primary_position
    is_bye = player.bye_week is not None and player.bye_week == week

    point_totals: list[float] = []
    fallback_totals: list[float] = []
    sources: set[str] = set()
    opponent = ""
    is_home: bool | None = None

    for entry in entries:
        sources.add(entry.row.source)
        opponent = opponent or entry.opponent
        if is_home is None:
            is_home = entry.is_home
        if entry.row.stats:
            points = score_row(entry.row.stats, settings)
            if points is not None:
                point_totals.append(points)
        if entry.row.projected_points is not None:
            fallback_totals.append(entry.row.projected_points)

    points_estimated = False
    if point_totals:
        base_points = statistics.fmean(point_totals)
    elif is_bye:
        # No stat line and he is on bye: that is a fact, not a gap. Zero is the right
        # number and it must not be confused with "we do not know".
        base_points = 0.0
    elif position in trust_source_points and fallback_totals:
        base_points = statistics.fmean(fallback_totals)
        points_estimated = True
        trusted_used.append(player.full_name)
    else:
        unscoreable.append(player.full_name)
        return None

    # Measured disagreement only. One source means zero spread of opinion, which is
    # exactly what it is -- not a claim that the player is predictable.
    points_stdev = statistics.stdev(point_totals) if len(point_totals) >= 2 else 0.0

    # Availability last, after the points are settled, mirroring blend.py: a projection
    # is what he scores when he plays, and whether he plays scales it rather than being
    # baked into it. A bye zeroes the number outright -- there is no game to be available
    # for, and multiplying by a status would leave a bye player worth a fraction.
    availability = 0.0 if is_bye else play_probability(player.status)

    # News after that, and last of all, in two parts that compose differently.
    #
    # The availability half says the same kind of thing Yahoo's injury designation says,
    # so the two are reconciled by taking the more pessimistic rather than multiplied --
    # a player Yahoo lists as questionable whom an article also calls questionable has one
    # injury and is charged for it once. Yahoo wins the floor: an article cannot talk a
    # player Yahoo has ruled out back into the lineup, because Yahoo is the source of the
    # actual inactive list and a beat writer is not, however recent.
    #
    # The role and context half says something Yahoo does not say at all -- who is getting
    # the carries, whether it is blowing a gale -- so it multiplies on top.
    adjustments: tuple[Adjustment, ...] = ()
    role_context = 1.0
    pre_news = 0.0 if is_bye else base_points * availability
    if news is not None and not is_bye:
        news_availability, role_context, live = news.adjustment_for(
            player.player_key, week, as_of=as_of
        )
        if live:
            availability = min(availability, news_availability)
            adjustments = tuple(
                Adjustment(
                    flag=flag.flag,
                    multiplier=flag.multiplier,
                    quote=flag.quote,
                    url=_article_url(news, flag.article_id),
                    source=flag.confidence,
                    observed_at=flag.captured_at,
                )
                for flag in live
            )

    projected = 0.0 if is_bye else base_points * availability * role_context

    return WeeklyValuation(
        player_key=player.player_key,
        name=player.full_name,
        position=position,
        team=player.team_abbr,
        week=week,
        projected_points=projected,
        eligible_positions=player.startable_positions,
        points_stdev=points_stdev * availability * role_context,
        base_points=base_points,
        pre_news_points=pre_news,
        opponent=opponent,
        is_home=is_home,
        is_bye=is_bye,
        status=player.status,
        status_full=player.status_full,
        percent_owned=player.percent_owned,
        availability=availability,
        points_estimated=points_estimated,
        sources=tuple(sorted(sources)),
        adjustments=adjustments,
    )
