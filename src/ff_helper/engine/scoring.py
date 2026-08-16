"""Convert raw stat projections into fantasy points under a specific league's scoring.

This is the step that makes the rest of the app *yours* rather than generic. A published
"projected points" number bakes in whoever published it's assumed scoring; a 0.5-PPR
league and a full-PPR league rank receiving backs very differently, and a 6-point passing
touchdown league changes where quarterbacks belong entirely. So we take per-stat
projections and re-score them with the stat modifiers pulled from your league settings.
"""

from __future__ import annotations

from ff_helper.yahoo.models import PROJECTION_STAT_IDS, LeagueSettings


def score_stats(stats: dict[str, float], modifiers: dict[int, float]) -> float | None:
    """Fantasy points for a projected stat line, or None if nothing scoreable is present.

    Returning None rather than 0.0 is deliberate: a player we have no projection for must
    be distinguishable from a player projected to score nothing.
    """
    total = 0.0
    matched = False
    for column, stat_id in PROJECTION_STAT_IDS.items():
        value = stats.get(column)
        if value is None:
            continue
        modifier = modifiers.get(stat_id)
        if modifier is None:
            continue
        total += value * modifier
        matched = True
    return total if matched else None


def score_row(stats: dict[str, float], settings: LeagueSettings) -> float | None:
    return score_stats(stats, settings.stat_modifiers)


def is_ppr(settings: LeagueSettings) -> float:
    """Points per reception, used to pick the matching format from external sources."""
    from ff_helper.yahoo.models import STAT_REC

    return settings.stat_modifiers.get(STAT_REC, 0.0)


def scoring_slug(settings: LeagueSettings) -> str:
    """Map league scoring onto the format slug used by external ADP sources."""
    ppr = is_ppr(settings)
    if ppr >= 0.75:
        return "ppr"
    if ppr >= 0.25:
        return "half-ppr"
    return "standard"
