"""Combine sources into one valuation per player.

The central design decision here is that **value and timing are kept apart**.

* *Value* answers "how good is this player" and is measured in fantasy points under your
  league's scoring. Point totals from different projection sources are directly
  comparable once re-scored, so they are averaged on their natural scale -- converting
  them to z-scores first would discard the magnitude information that VOR depends on.
* *Timing* answers "when will he actually be gone" and is measured in ADP, with a
  standard deviation. It says nothing about quality.

Collapsing the two into a single "rank" is the thing almost every cheat sheet does, and it
is why they cannot tell you whether to take the good player now or the scarce one.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ff_helper.engine.scoring import score_row
from ff_helper.rankings.players import PlayerRegistry, SourceRow
from ff_helper.yahoo.models import DEFAULT_AUCTION_BUDGET, LeagueSettings, YahooPlayer

# How much each ADP source counts toward the blended mean. Yahoo dominates because the
# draft happens on Yahoo, against opponents looking at Yahoo's own rankings.
ADP_WEIGHTS = {"yahoo": 0.65, "ffc": 0.35}

# ADP uncertainty grows with ADP: pick 1 is nearly deterministic, pick 150 is a coin flip
# across a wide range. Fitted loosely to published ADP spreads; used only when a source
# does not supply a real standard deviation.
_STDEV_COEFFICIENT = 0.32
_STDEV_EXPONENT = 0.87
_STDEV_FLOOR = 1.5

# Expected games missed by injury designation, out of a 17-game season. Deliberately
# coarse: the point is that IR is a different *magnitude* of problem than Questionable,
# not that we can predict a recovery week. Q and D are game-time tags and cost nothing
# here; the engines keep a residual penalty for the risk a status cannot price.
_EXPECTED_GAMES_MISSED = {"IR": 10.0, "PUP": 6.0, "NFI": 6.0, "O": 3.0, "SUSP": 3.0}
_SEASON_GAMES = 17.0

# Fallback spread of a projection when only one source supplied one, as a fraction of
# the projection itself -- roughly the disagreement seen between major sources.
_STDEV_POINTS_FRACTION = 0.12
# A typical FantasyPros expert-rank spread; a player argued about twice as hard gets a
# proportionally wider fallback points spread, clamped so one outlier cannot run wild.
_ECR_STD_NEUTRAL = 5.0
_ECR_STD_SCALE_MIN = 0.6
_ECR_STD_SCALE_MAX = 2.0


def availability_of(status: str) -> float:
    """Fraction of the season a player with this status is expected to be active."""
    missed = _EXPECTED_GAMES_MISSED.get(status.upper(), 0.0)
    return (_SEASON_GAMES - missed) / _SEASON_GAMES


def estimate_adp_stdev(adp: float) -> float:
    """Approximate the spread of draft positions for a player with this mean ADP."""
    return max(_STDEV_FLOOR, _STDEV_COEFFICIENT * (adp**_STDEV_EXPONENT))


@dataclass(frozen=True)
class PlayerValuation:
    """Everything the recommendation engine needs about one player."""

    player_key: str
    name: str
    position: str
    team: str
    projected_points: float
    adp: float
    adp_stdev: float
    bye_week: int | None = None
    status: str = ""
    ecr: float | None = None
    tier: int | None = None
    # Average auction price across sources -- what the room pays, not what he is worth.
    market_cost: float | None = None
    # True when projected_points was interpolated rather than projected directly.
    points_estimated: bool = False
    # True when adp was derived from value rank because no source listed the player.
    adp_estimated: bool = False
    sources: tuple[str, ...] = ()
    # Fraction of the season he is expected to play, from his injury status; his
    # projected_points have already been scaled by it.
    availability: float = 1.0
    # Spread of the source projections, in points. Where sources genuinely disagree this
    # is measured; otherwise it is a fraction of the projection widened by how hard the
    # experts argue about him. The upside model reads it; nothing else does.
    points_stdev: float = 0.0

    @property
    def is_injured(self) -> bool:
        return self.status.upper() in {"IR", "O", "PUP", "NFI", "SUSP"}


@dataclass
class BlendResult:
    valuations: dict[str, PlayerValuation] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def by_position(self, position: str) -> list[PlayerValuation]:
        return sorted(
            (v for v in self.valuations.values() if v.position == position),
            key=lambda v: -v.projected_points,
        )

    @property
    def ordered(self) -> list[PlayerValuation]:
        return sorted(self.valuations.values(), key=lambda v: v.adp)


def blend(
    registry: PlayerRegistry,
    grouped: dict[str, list[SourceRow]],
    settings: LeagueSettings,
) -> BlendResult:
    """Fold all source rows for each player into a single valuation."""
    result = BlendResult()

    partial: dict[str, dict] = {}
    for player_key, rows in grouped.items():
        player = registry.by_key.get(player_key)
        if player is None:
            continue
        partial[player_key] = _combine(player, rows, settings)

    _fill_missing_points(partial, result)
    _fill_missing_adp(partial, result)

    for player_key, data in partial.items():
        if data["projected_points"] is None or data["adp"] is None:
            # Nothing to say about this player; leaving them out is better than
            # recommending someone we cannot value.
            continue
        # Injury applied last, after interpolation: a curve read off healthy players
        # must not inherit anyone's absence, and an absence scales spread and points
        # alike. Everything downstream -- VOR, dollars, plans -- sees it automatically.
        availability = availability_of(data["status"])
        points = data["projected_points"] * availability
        stdev = data["points_stdev"]
        if stdev is None:
            stdev = _STDEV_POINTS_FRACTION * data["projected_points"]
        result.valuations[player_key] = PlayerValuation(
            player_key=player_key,
            name=data["name"],
            position=data["position"],
            team=data["team"],
            projected_points=points,
            adp=data["adp"],
            adp_stdev=data["adp_stdev"],
            bye_week=data["bye_week"],
            status=data["status"],
            ecr=data["ecr"],
            tier=data["tier"],
            market_cost=data["market_cost"],
            points_estimated=data["points_estimated"],
            adp_estimated=data["adp_estimated"],
            sources=tuple(sorted(data["sources"])),
            availability=availability,
            points_stdev=stdev * availability,
        )

    return result


def _combine(player: YahooPlayer, rows: list[SourceRow], settings: LeagueSettings) -> dict:
    """Merge one player's rows, re-scoring projections under the league's own scoring."""
    point_totals: list[float] = []
    adp_values: list[tuple[str, float]] = []
    stdevs: list[float] = []
    ecrs: list[float] = []
    ecr_stds: list[float] = []
    tiers: list[int] = []
    costs: list[float] = []

    for row in rows:
        # Only stat lines are usable. A source's own point total bakes in that source's
        # scoring assumptions, which is exactly what we are trying to replace, so it is
        # deliberately ignored -- such players get interpolated in _fill_missing_points.
        if row.stats:
            points = score_row(row.stats, settings)
            if points is not None:
                point_totals.append(points)

        if row.adp is not None:
            adp_values.append((row.source, row.adp))
        if row.adp_stdev is not None:
            stdevs.append(row.adp_stdev)
        if row.ecr is not None:
            ecrs.append(row.ecr)
        if row.ecr_std is not None:
            ecr_stds.append(row.ecr_std)
        if row.tier is not None:
            tiers.append(row.tier)
        if row.auction_cost is not None and row.auction_cost > 0:
            cost = row.auction_cost
            if row.source == "yahoo":
                # Yahoo's average_cost is measured across default $200 rooms. A CSV cost
                # is left alone: the user exports it for their own league.
                cost *= settings.auction_budget / DEFAULT_AUCTION_BUDGET
            costs.append(cost)

    adp = _weighted_adp(adp_values)

    # Measured disagreement when two or more sources projected him; otherwise a fraction
    # of the projection, widened by how hard the experts argue about his rank. None here
    # means "no projection yet" -- interpolated players get their fallback at build time.
    points_stdev: float | None = None
    if len(point_totals) >= 2:
        points_stdev = statistics.stdev(point_totals)
    elif point_totals:
        scale = 1.0
        if ecr_stds:
            scale = statistics.fmean(ecr_stds) / _ECR_STD_NEUTRAL
            scale = max(_ECR_STD_SCALE_MIN, min(_ECR_STD_SCALE_MAX, scale))
        points_stdev = _STDEV_POINTS_FRACTION * point_totals[0] * scale

    return {
        "name": player.full_name,
        "position": player.primary_position,
        "team": player.team_abbr.upper(),
        "bye_week": player.bye_week,
        "status": player.status,
        "projected_points": statistics.fmean(point_totals) if point_totals else None,
        "points_stdev": points_stdev,
        "points_estimated": False,
        "adp": adp,
        "adp_stdev": statistics.fmean(stdevs)
        if stdevs
        else (estimate_adp_stdev(adp) if adp is not None else None),
        "adp_estimated": False,
        "ecr": statistics.fmean(ecrs) if ecrs else None,
        "tier": min(tiers) if tiers else None,
        "market_cost": statistics.fmean(costs) if costs else None,
        "sources": {row.source for row in rows},
    }


def _weighted_adp(values: list[tuple[str, float]]) -> float | None:
    if not values:
        return None
    numerator = 0.0
    denominator = 0.0
    for source, adp in values:
        weight = ADP_WEIGHTS.get(source, 0.2)
        numerator += weight * adp
        denominator += weight
    return numerator / denominator if denominator else None


def _fill_missing_points(partial: dict[str, dict], result: BlendResult) -> None:
    """Estimate points for players with no stat projection (kickers, defenses, deep bench).

    Rather than dropping them -- you do have to draft a kicker eventually -- we read their
    position's points-versus-positional-rank curve off the players we *can* project, and
    interpolate at the missing player's consensus rank. It says: "consensus calls him the
    14th tight end, and the 14th tight end scores about this much."
    """
    by_position: dict[str, list[dict]] = {}
    for data in partial.values():
        by_position.setdefault(data["position"], []).append(data)

    for position, group in by_position.items():
        known = sorted(
            (d for d in group if d["projected_points"] is not None),
            key=lambda d: -d["projected_points"],
        )
        missing = [d for d in group if d["projected_points"] is None]
        if not missing:
            continue

        if not known:
            # No projections at all for this position (typical for K and DEF). Fall back
            # to a flat, low value so they sort by ADP among themselves and never
            # outrank a projected skill player.
            for data in missing:
                data["projected_points"] = 0.0
                data["points_estimated"] = True
            result.notes.append(
                f"{position}: no stat projections available; ranked by ADP only "
                f"({len(missing)} players)"
            )
            continue

        curve = [d["projected_points"] for d in known]
        for data in missing:
            rank = _consensus_rank(data, group)
            data["projected_points"] = _interpolate(curve, rank)
            data["points_estimated"] = True

        result.notes.append(
            f"{position}: interpolated projections for {len(missing)} players "
            f"from {len(known)} projected"
        )


def _consensus_rank(data: dict, group: list[dict]) -> int:
    """Positional rank by ECR, falling back to ADP, falling back to last."""

    def sort_key(entry: dict) -> tuple[int, float]:
        if entry["ecr"] is not None:
            return (0, entry["ecr"])
        if entry["adp"] is not None:
            return (1, entry["adp"])
        return (2, float("inf"))

    ordered = sorted(group, key=sort_key)
    for index, entry in enumerate(ordered):
        if entry is data:
            return index
    return len(ordered) - 1


def _interpolate(curve: list[float], rank: int) -> float:
    """Read a value off a descending points curve at a given rank."""
    if not curve:
        return 0.0
    if rank < len(curve):
        return curve[rank]
    # Past the end of the curve, decay from the last known value rather than clamping,
    # so deep players still order sensibly among themselves.
    tail = curve[-1]
    overshoot = rank - len(curve) + 1
    return max(0.0, tail - overshoot * 0.5)


def _fill_missing_adp(partial: dict[str, dict], result: BlendResult) -> None:
    """Give an ADP to players no source listed, so they can still be evaluated.

    These get a deliberately wide standard deviation: we genuinely do not know when they
    will go, and the survival model should reflect that rather than fake confidence.
    """
    missing = [d for d in partial.values() if d["adp"] is None]
    if not missing:
        return

    known_adps = [d["adp"] for d in partial.values() if d["adp"] is not None]
    floor = max(known_adps) if known_adps else 200.0

    ranked = sorted(
        missing,
        key=lambda d: -(d["projected_points"] if d["projected_points"] is not None else -1),
    )
    for offset, data in enumerate(ranked, start=1):
        data["adp"] = floor + offset
        data["adp_stdev"] = estimate_adp_stdev(data["adp"]) * 1.5
        data["adp_estimated"] = True

    result.notes.append(f"{len(missing)} players had no ADP from any source; estimated from value")
