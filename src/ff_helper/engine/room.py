"""Learn how *this* room drafts, from the picks it has already made.

ADP is an average over thousands of rooms. The one you are sitting in has twelve
particular people, and by round five they have told you things no average can: they take
quarterbacks a round early, they let tight ends slide, they draft by their queue and
ignore the news. The auction engine already learns its room's paying habits
(``auction.room_premiums``); this is the snake analog -- measured in picks instead of
dollars.

Every completed pick is one observation: ``pick number - blended ADP``. Positive means
the room lets players of that kind last longer than the sheet says; negative means it
reaches. The estimator is shrunk exactly like the auction one: the room-wide tendency
starts at zero and moves as picks accumulate, each position's tendency starts at the
room-wide one, and everything is clamped -- one player sliding forty picks on injury
news is a faller, not a tendency.

The output is an ADP shift consumed by the survival model, applied *before* the
pick-budget normalization: a room that is uniformly slow mostly cancels back out in the
normalizer, while a per-position skew -- the actual signal -- survives it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ff_helper.rankings.blend import PlayerValuation
from ff_helper.yahoo.models import DraftPick

# How many picks' worth of belief in "the room drafts at ADP" the estimate starts with.
# A position needs more evidence than the room overall before its shift moves, because
# per-position pick counts are small and two early quarterbacks are an anecdote.
_TENDENCY_PRIOR_GLOBAL = 12.0
_TENDENCY_PRIOR_POSITION = 6.0

# One observation can contribute at most this many picks of deviation. Beyond it, the
# pick says something about that player (injury slide, homer reach), not the room.
_OBSERVATION_CLAMP = 25.0

# The final shift never exceeds this many picks in either direction. A model output
# should nudge effective ADP, not rewrite the board.
_SHIFT_CLAMP = 10.0


@dataclass(frozen=True)
class PickObservation:
    """One completed pick, reduced to what tendency estimation needs."""

    position: str
    # pick number minus blended ADP: positive = went later than the sheet expected.
    deviation: float


@dataclass(frozen=True)
class RoomTendencies:
    """How this room's picks run relative to ADP, in picks.

    ``scale`` is a v2 hook: a room could also be more or less *predictable* than ADP's
    spread implies (tighter or wider than the sheet's stdev). Estimating that well needs
    more picks than a draft usually offers, so it is fixed at 1.0 for now and consumers
    treat it as such.
    """

    overall: float = 0.0
    by_position: dict[str, float] = field(default_factory=dict)
    observed: int = 0
    scale: float = 1.0

    def shift(self, position: str) -> float:
        return self.by_position.get(position, self.overall)


def _clamped(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def room_tendencies(observations: list[PickObservation]) -> RoomTendencies:
    """Estimate the room's drafting tendencies from the picks so far.

    Shrinkage identical in shape to the auction ``room_premiums``: an empty or young
    draft reports zero shift rather than the noise of its first few picks.
    """
    deviations = [
        (observation.position, _clamped(observation.deviation, _OBSERVATION_CLAMP))
        for observation in observations
    ]

    overall = sum(deviation for _, deviation in deviations) / (
        _TENDENCY_PRIOR_GLOBAL + len(deviations)
    )
    overall = _clamped(overall, _SHIFT_CLAMP)

    by_position: dict[str, list[float]] = {}
    for position, deviation in deviations:
        by_position.setdefault(position, []).append(deviation)

    return RoomTendencies(
        overall=overall,
        by_position={
            position: _clamped(
                (_TENDENCY_PRIOR_POSITION * overall + sum(group))
                / (_TENDENCY_PRIOR_POSITION + len(group)),
                _SHIFT_CLAMP,
            )
            for position, group in by_position.items()
        },
        observed=len(deviations),
    )


def observations_from_board(
    picks: Iterable[DraftPick],
    valuations: dict[str, PlayerValuation],
    *,
    exclude_team: str | None = None,
) -> list[PickObservation]:
    """Turn the live board into tendency observations.

    Players the blend could not place are skipped: an *estimated* ADP would let the
    model observe its own guess as if the room had confirmed it. ``exclude_team``
    should be *my* team: the estimator predicts opponent behavior, and learning from
    my own picks feeds the engine's advice back into the model that produces it.
    """
    observations: list[PickObservation] = []
    for pick in picks:
        if exclude_team is not None and pick.team_key == exclude_team:
            continue
        valuation = valuations.get(pick.player_key)
        if valuation is None or valuation.adp_estimated:
            continue
        observations.append(
            PickObservation(
                position=valuation.position,
                deviation=pick.pick - valuation.adp,
            )
        )
    return observations
