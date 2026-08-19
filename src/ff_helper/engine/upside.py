"""Upside: once the bench is what you are drafting, variance is the point.

A starting lineup wants the highest *expected* points, and the engines score that
already. A late bench spot wants something different: a player who might crack your
lineup at all. A steady backup projected for 110 will never start over anyone; a
volatile rookie projected for 105 might be a league-winner. Expected value cannot see
that difference -- spread can.

So this is a small, deliberately bounded bonus for *excess* projection spread -- spread
beyond the ordinary disagreement every projection carries -- that phases in as the
roster fills. Early it is exactly zero: a coin-flip must never outrank a starter-caliber
pick. Late it tops out at a few VOR points: enough to break ties toward darts, never
enough to overrule the projections.
"""

from __future__ import annotations

from ff_helper.rankings.blend import _STDEV_POINTS_FRACTION, PlayerValuation

# The ordinary spread every projection carries -- imported from blend rather than
# restated, because the model only works while the two agree: if blend's fallback
# fraction ever exceeded this baseline, every interpolated K/DEF/bench player would
# clear it at once and collect the full dart cap.
_BASELINE_FRACTION = _STDEV_POINTS_FRACTION

# Roster fullness where the bonus starts ramping in and where it reaches full weight.
# Below the start, starting lineups are still being built and the bonus must be zero.
_PHASE_START = 0.5
_PHASE_FULL = 0.8

# Excess spread converts to bonus VOR points at this rate before the cap.
_EXCESS_RATE = 0.5


def upside_bonus(
    valuation: PlayerValuation, *, roster_fullness: float, cap: float = 6.0
) -> float:
    """Bonus VOR points for excess projection spread, phased in as the roster fills.

    ``roster_fullness`` is players held over roster size, 0..1. The bonus is zero
    through the starter-building half of a draft, ramps across the bench rounds, and is
    capped at ``cap`` so no dart can outrank a genuinely better projection by much.
    """
    if valuation.projected_points <= 0:
        return 0.0
    weight = (roster_fullness - _PHASE_START) / (_PHASE_FULL - _PHASE_START)
    weight = max(0.0, min(1.0, weight))
    if weight == 0.0:
        return 0.0
    excess = valuation.points_stdev - _BASELINE_FRACTION * valuation.projected_points
    if excess <= 0.0:
        return 0.0
    return min(cap, excess * _EXCESS_RATE) * weight
