"""Was the survival model right? Score it against a draft that actually happened.

The snake engine's advice rests on one kind of number: "the probability he is still on
the board at your next pick." Those numbers are unfalsifiable during a draft and
perfectly falsifiable afterwards -- every prediction eventually resolved to a yes or a
no. This module replays a recorded draft, collects every (prediction, outcome) pair the
engine would have produced at your turns, and reports:

* **Brier score** -- mean squared error of the probabilities; 0 is clairvoyance, 0.25 is
  what you get by shrugging "fifty-fifty" at everything.
* **Reliability bins** -- of the players given ~70% survival, did about 70% survive?
  This is the table that catches a model that is *ranked* right but *scaled* wrong.

The predictor is a parameter so competing survival models (analytic, normalized,
Monte Carlo) can be scored against the same record and compared like for like.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ff_helper.assistant import Assistant
from ff_helper.engine.room import observations_from_board, room_tendencies
from ff_helper.engine.vona import survival_normalizers, survival_probability
from ff_helper.rankings.blend import PlayerValuation
from ff_helper.yahoo.models import DraftPick


class Predictor(Protocol):
    """P(still available at target_pick) for every player available at current_pick."""

    def __call__(
        self,
        assistant: Assistant,
        available: list[PlayerValuation],
        current_pick: int,
        target_pick: int,
    ) -> dict[str, float]: ...


def analytic_predictor(
    assistant: Assistant,
    available: list[PlayerValuation],
    current_pick: int,
    target_pick: int,
) -> dict[str, float]:
    """The survival numbers the snake engine itself uses: conditional logistic survival
    with this room's positional demand and learned tendencies, normalized to the
    window's pick budget."""
    position_of = assistant.position_of
    demand = assistant._position_demand(current_pick, [target_pick], position_of)
    shares = demand.get(target_pick, {})
    tendencies = room_tendencies(
        observations_from_board(assistant.state.board.values(), assistant.valuations.valuations)
    )
    raw = {
        valuation.player_key: survival_probability(
            valuation,
            current_pick=current_pick,
            target_pick=target_pick,
            demand=shares.get(valuation.position, 1.0),
            adp_shift=tendencies.shift(valuation.position),
        )
        for valuation in available
    }
    normalizers = survival_normalizers(
        {target_pick: list(raw.values())},
        current_pick=current_pick,
        my_picks=list(assistant.state.my_picks),
    )
    beta = normalizers[target_pick]
    return {key: survival**beta for key, survival in raw.items()}


@dataclass(frozen=True)
class CalibrationSample:
    player_key: str
    position: str
    predicted: float
    survived: bool
    window: tuple[int, int]  # (my pick when predicted, my next pick)


@dataclass(frozen=True)
class CalibrationReport:
    brier: float
    n: int
    # One row per bin: (mean predicted, observed survival rate, sample count).
    bins: tuple[tuple[float, float, int], ...]
    # Per position: (brier, sample count).
    by_position: dict[str, tuple[float, int]]
    samples: tuple[CalibrationSample, ...]


def survival_calibration(
    assistant: Assistant,
    picks: list[DraftPick],
    *,
    predictor: Predictor | None = None,
    num_bins: int = 10,
) -> CalibrationReport:
    """Replay ``picks`` through the assistant's board and score survival predictions.

    At each of my turns, every available player gets a predicted survival to my *next*
    turn; the recorded draft supplies the outcome. Players I removed myself are skipped
    -- the model predicts opponent behavior, and my own choices are not its problem.
    """
    predict = predictor or analytic_predictor
    state = assistant.state
    my_team = state.my_team
    my_key = my_team.team_key if my_team else None

    ordered = sorted(picks)
    drafted_at: dict[str, int] = {pick.player_key: pick.pick for pick in ordered}
    my_turns = [pick.pick for pick in ordered if pick.team_key == my_key]

    samples: list[CalibrationSample] = []
    for pick in ordered:
        if pick.team_key == my_key:
            now = pick.pick
            target = next((turn for turn in my_turns if turn > now), None)
            if target is not None:
                available = assistant.available()
                predictions = predict(assistant, available, now, target)
                for valuation in available:
                    gone_pick = drafted_at.get(valuation.player_key)
                    if gone_pick == now:
                        # Removed by me, right now. My own choices are not evidence
                        # about opponent behavior.
                        continue
                    survived = gone_pick is None or gone_pick >= target
                    samples.append(
                        CalibrationSample(
                            player_key=valuation.player_key,
                            position=valuation.position,
                            predicted=predictions[valuation.player_key],
                            survived=survived,
                            window=(now, target),
                        )
                    )
        state.apply_sync([pick], timestamp=0.0)

    return _report(samples, num_bins=num_bins)


def _report(samples: list[CalibrationSample], *, num_bins: int) -> CalibrationReport:
    if not samples:
        return CalibrationReport(brier=0.0, n=0, bins=(), by_position={}, samples=())

    def brier_of(group: list[CalibrationSample]) -> float:
        return sum((s.predicted - (1.0 if s.survived else 0.0)) ** 2 for s in group) / len(group)

    binned: list[list[CalibrationSample]] = [[] for _ in range(num_bins)]
    for sample in samples:
        index = min(num_bins - 1, int(sample.predicted * num_bins))
        binned[index].append(sample)
    bins = tuple(
        (
            sum(s.predicted for s in group) / len(group),
            sum(1 for s in group if s.survived) / len(group),
            len(group),
        )
        for group in binned
        if group
    )

    by_position: dict[str, tuple[float, int]] = {}
    for position in sorted({s.position for s in samples}):
        group = [s for s in samples if s.position == position]
        by_position[position] = (brier_of(group), len(group))

    return CalibrationReport(
        brier=brier_of(samples),
        n=len(samples),
        bins=bins,
        by_position=by_position,
        samples=tuple(samples),
    )


# -- replay hit summary ------------------------------------------------------------------


@dataclass(frozen=True)
class TurnReport:
    """What the engine said at one of my turns, next to what actually happened."""

    pick: int
    round: int
    actual_key: str
    actual_name: str
    recommendations: tuple
    # 1-based rank of my actual pick within the engine's list, when it made the list.
    match_rank: int | None
    # Wall-clock seconds the recommendation call took; the latency budget lives here.
    elapsed: float


def turn_reports(
    assistant: Assistant,
    picks: list[DraftPick],
    *,
    limit: int = 3,
    on_turn: Callable[[TurnReport], None] | None = None,
) -> list[TurnReport]:
    """Replay ``picks``, capturing the engine's short list at each of my turns.

    ``on_turn`` lets a caller print each turn as it resolves rather than waiting for
    the whole draft; ``scripts/replay.py`` uses it for its live-style output.
    """
    state = assistant.state
    my_team = state.my_team
    my_key = my_team.team_key if my_team else None

    reports: list[TurnReport] = []
    for pick in sorted(picks):
        if pick.team_key == my_key:
            started = time.perf_counter()
            recommendations = assistant.recommendations(limit=limit)
            elapsed = time.perf_counter() - started
            match_rank = next(
                (
                    index
                    for index, rec in enumerate(recommendations, start=1)
                    if rec.valuation.player_key == pick.player_key
                ),
                None,
            )
            report = TurnReport(
                pick=pick.pick,
                round=pick.round,
                actual_key=pick.player_key,
                actual_name=assistant._player_name(pick.player_key),
                recommendations=tuple(recommendations),
                match_rank=match_rank,
                elapsed=elapsed,
            )
            reports.append(report)
            if on_turn is not None:
                on_turn(report)
        state.apply_sync([pick], timestamp=0.0)
    return reports
