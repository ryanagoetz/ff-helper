"""The in-season engine: which lineup to start, and what is about to go wrong with it.

Sibling of ``vona.py`` and ``auction.py``, sharing the layer beneath them and nothing
above it. The draft engines price scarcity -- of picks, of dollars. There is no scarcity
here: every player you own is available to you every week, and the only question is which
nine of them to put in the nine slots. That makes the weekly problem an assignment
problem rather than a search, which is why it is exact.

Two answers come out of this module, and they are deliberately different questions:

**start_sit** compares the lineup you have set against the best one available and reports
the difference as a list of changes. It reports *both* totals always. A recommendation
that only shows its own answer gives you no way to judge whether it is worth acting on --
"start Odunze over Hubbard, +0.3" and "start Odunze over Hubbard, +6.1" deserve very
different responses from you, and only the second is worth a Sunday morning.

**inactive_check** ignores optimality entirely and asks one question: is anybody in the
lineup that will actually score this week not going to play? That is a different failure
from a suboptimal start, it resolves on a much shorter clock, and conflating the two would
bury a zero behind a list of half-point upgrades.

Neither writes anything to Yahoo. Every change here is one you make yourself.
"""

from __future__ import annotations

import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ff_helper.engine import winprob
from ff_helper.engine.lineup import Lineup, LineupSlot, optimal_lineup, starting_slots
from ff_helper.season.valuation import WeeklyValuation
from ff_helper.yahoo.models import BENCH_SLOTS, LeagueSettings, RosterEntry

# A change worth less than this is noise dressed as advice. Weekly projections are not
# accurate to a tenth of a point, and a digest that lists six sub-point swaps trains you
# to skim past the one that matters.
MIN_REPORTABLE_GAIN = 0.5


@dataclass(frozen=True)
class LineupChange:
    """One swap to make, with the size of the gain and why it is there."""

    player_in: str
    player_out: str  # "" when the slot was empty
    slot: str
    points_delta: float
    reason: str


@dataclass(frozen=True)
class StartSit:
    current: Lineup
    optimal: Lineup
    changes: tuple[LineupChange, ...]
    # Players on the roster with no projection this week. They are excluded from the
    # optimizer rather than treated as zero, because "unknown" and "will not score" lead
    # to opposite decisions and only one of them is a reason to bench somebody.
    unvalued: tuple[str, ...] = ()

    @property
    def points_left_on_bench(self) -> float:
        return max(0.0, self.optimal.total - self.current.total)

    @property
    def is_already_optimal(self) -> bool:
        return not self.changes


@dataclass(frozen=True)
class InactiveRisk:
    """A player in the lineup who may not score, and the best thing to do about it."""

    player_key: str
    name: str
    slot: str
    reason: str  # "bye" | "out" | "doubtful" | "questionable"
    status_full: str
    projected_points: float
    replacement_key: str = ""
    replacement_name: str = ""
    replacement_points: float = 0.0

    @property
    def is_certain(self) -> bool:
        """True when he definitely will not play. Different urgency from 'might not'."""
        return self.reason in {"bye", "out"}

    @property
    def gain_from_replacing(self) -> float:
        return self.replacement_points - self.projected_points


def current_lineup(
    valuations: Mapping[str, WeeklyValuation],
    entries: Sequence[RosterEntry],
    settings: LeagueSettings,
) -> Lineup:
    """The lineup as it is set right now, read from Yahoo's ``selected_position``.

    Built by walking the league's slots in order and taking the first unclaimed roster
    entry sitting in each. A player whose slot does not correspond to anything in the
    league's settings falls to the bench rather than being dropped, so the totals still
    add up if a league changes its roster shape mid-season.

    An entry with no slot reported is *not* treated as benched -- see ``RosterEntry`` --
    but it does not hold a starting slot either, so it lands on the bench here. The
    distinction that matters (Yahoo said nothing vs. you sat him) is preserved upstream.
    """
    slots = starting_slots(settings)
    unclaimed = list(entries)
    starters: list[tuple[LineupSlot, str]] = []
    empty: list[LineupSlot] = []
    used: set[str] = set()
    total = 0.0

    for slot in slots:
        match = next(
            (
                entry
                for entry in unclaimed
                if entry.selected_position == slot.position and entry.player_key not in used
            ),
            None,
        )
        if match is None:
            empty.append(slot)
            continue
        starters.append((slot, match.player_key))
        used.add(match.player_key)
        valuation = valuations.get(match.player_key)
        if valuation is not None:
            total += valuation.projected_points

    bench = tuple(entry.player_key for entry in entries if entry.player_key not in used)
    return Lineup(starters=tuple(starters), bench=bench, empty=tuple(empty), total=total)


def start_sit(
    valuations: Mapping[str, WeeklyValuation],
    entries: Sequence[RosterEntry],
    settings: LeagueSettings,
    *,
    locked: Mapping[int, str] | None = None,
    min_gain: float = MIN_REPORTABLE_GAIN,
) -> StartSit:
    """The best available lineup, and the changes that get you there from where you are.

    ``locked`` names slots that can no longer be changed because the game has kicked off.
    Nothing in this app knows kickoff times yet -- Yahoo does not publish them on any
    endpoint we read -- so the caller supplies them. Left empty, this answers the Tuesday
    question ("what should I set") rather than the Sunday one ("what can I still fix").

    Players on IR are excluded: they occupy no startable slot and offering one as a change
    would produce advice Yahoo will not accept.
    """
    rosterable = [
        entry
        for entry in entries
        if entry.selected_position not in {"IR", "IR+", "IR-R", "NA"}
    ]
    pool = [
        valuations[entry.player_key]
        for entry in rosterable
        if entry.player_key in valuations
    ]
    unvalued = tuple(
        entry.player_key for entry in rosterable if entry.player_key not in valuations
    )

    current = current_lineup(valuations, entries, settings)
    best = optimal_lineup(pool, settings, locked=locked)
    changes = _changes(valuations, current, best, min_gain=min_gain)
    return StartSit(current=current, optimal=best, changes=changes, unvalued=unvalued)


def _changes(
    valuations: Mapping[str, WeeklyValuation],
    current: Lineup,
    best: Lineup,
    *,
    min_gain: float,
) -> tuple[LineupChange, ...]:
    """Turn two lineups into the swaps between them.

    Compared as *sets of starters*, not slot by slot. The optimizer is free to move a
    receiver from the WR slot to the flex without changing who plays, and reporting that
    as a change would be describing bookkeeping as advice.
    """
    incoming = [key for key in best.starter_keys if key not in set(current.starter_keys)]
    outgoing = [key for key in current.starter_keys if key not in set(best.starter_keys)]

    incoming.sort(key=lambda key: -_points(valuations, key))
    outgoing.sort(key=lambda key: _points(valuations, key))

    changes: list[LineupChange] = []
    for index, player_in in enumerate(incoming):
        player_out = outgoing[index] if index < len(outgoing) else ""
        delta = _points(valuations, player_in) - _points(valuations, player_out)
        slot = best.slot_of(player_in)
        if player_out and delta < min_gain:
            continue
        changes.append(
            LineupChange(
                player_in=player_in,
                player_out=player_out,
                slot=slot.position if slot else "",
                points_delta=delta,
                reason=_reason(valuations, player_in, player_out, delta),
            )
        )
    return tuple(changes)


def _points(valuations: Mapping[str, WeeklyValuation], player_key: str) -> float:
    valuation = valuations.get(player_key)
    return valuation.projected_points if valuation else 0.0


def _name(valuations: Mapping[str, WeeklyValuation], player_key: str) -> str:
    valuation = valuations.get(player_key)
    return valuation.name if valuation else player_key


def _reason(
    valuations: Mapping[str, WeeklyValuation],
    player_in: str,
    player_out: str,
    delta: float,
) -> str:
    """One line saying why, leading with the disqualifying fact when there is one.

    A bye or a ruled-out player is the whole reason for the change; the point difference
    is a consequence. Leading with the number in that case buries the part you would want
    to check for yourself.
    """
    if not player_out:
        return "that slot is empty and scoring zero"

    out = valuations.get(player_out)
    if out is not None and out.is_bye:
        return f"{out.name} is on bye"
    if out is not None and out.availability == 0.0:
        return f"{out.name} is {out.status_full or out.status or 'out'}"

    incoming = valuations.get(player_in)
    detail = ""
    if out is not None and out.availability < 1.0:
        detail = f"; {out.name} is {out.status_full or out.status}"
    elif incoming is not None and incoming.adjustments:
        # Surface the sentence that moved him -- news that changes a lineup has to be
        # checkable, not just applied.
        detail = f"; {incoming.adjustments[0].quote}"
    return f"+{delta:.1f} projected{detail}"


def inactive_check(
    valuations: Mapping[str, WeeklyValuation],
    entries: Sequence[RosterEntry],
    settings: LeagueSettings,
    *,
    lineup: Lineup | None = None,
) -> tuple[InactiveRisk, ...]:
    """Anyone in the lineup that will actually score who may not play, worst first.

    ``lineup`` defaults to the one you have set, which is what will actually score. The
    digest passes the *recommended* one instead, because it is simultaneously telling you
    to field that -- and a replacement offered here must not be a player the lineup advice
    has already deployed elsewhere, or following both would start him twice.

    A named replacement is the point. "Chase is questionable" is a fact you already had
    from Yahoo; "Chase is questionable, and Odunze at 8.7 is the best legal swap" is the
    thing you can act on in the ninety seconds before kickoff.
    """
    board = lineup or current_lineup(valuations, entries, settings)
    started = set(board.starter_keys)
    bench = [
        valuations[entry.player_key]
        for entry in entries
        if entry.player_key not in started
        and entry.player_key in valuations
        and entry.selected_position not in {"IR", "IR+", "IR-R", "NA"}
    ]

    risks: list[InactiveRisk] = []
    for slot, player_key in board.starters:
        valuation = valuations.get(player_key)
        if valuation is None:
            continue
        reason = _risk_reason(valuation)
        if reason is None:
            continue

        replacement = _best_replacement(bench, slot)
        risks.append(
            InactiveRisk(
                player_key=player_key,
                name=valuation.name,
                slot=slot.position,
                reason=reason,
                status_full=valuation.status_full or valuation.status,
                projected_points=valuation.projected_points,
                replacement_key=replacement.player_key if replacement else "",
                replacement_name=replacement.name if replacement else "",
                replacement_points=replacement.projected_points if replacement else 0.0,
            )
        )

    # Certain absences first, then by how much the swap is worth. A bye you have not
    # noticed outranks a questionable tag you have, however many points each is worth.
    return tuple(
        sorted(risks, key=lambda risk: (not risk.is_certain, -risk.gain_from_replacing))
    )


def _risk_reason(valuation: WeeklyValuation) -> str | None:
    if valuation.is_bye:
        return "bye"
    status = valuation.status.strip().upper()
    if valuation.availability == 0.0:
        return "out"
    if status == "D":
        return "doubtful"
    if status in {"Q", "P"}:
        return "questionable"
    return None


def _best_replacement(
    bench: Sequence[WeeklyValuation], slot: LineupSlot
) -> WeeklyValuation | None:
    """The best benched player who can legally fill this slot and is himself playable."""
    eligible = [
        valuation
        for valuation in bench
        if slot.eligible & set(valuation.eligible_positions) and valuation.is_playable
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda valuation: valuation.projected_points)


# --------------------------------------------------------------------------------------
# Win probability
# --------------------------------------------------------------------------------------

# How much modelled win probability a swap must gain before it is worth telling you about.
# The threshold is the design decision here, not the search.
#
# An exact win-probability optimizer would be chasing an optimization gap far smaller than
# the estimation error underneath it: the position CVs are unfitted judgement, the
# correlation constants are rounder still, and the opponent's lineup is a guess. A swap
# that gains 0.3pp of *modelled* win probability is noise dressed as insight, and a digest
# that reports it every week teaches you to ignore the one week it matters.
#
# Set here so that the tool says "start your projected-best lineup" almost always, and
# speaks up only when the underdog maths is genuinely large.
WINPROB_EDGE = 0.005


@dataclass(frozen=True)
class WinProbConfig:
    rollouts: int = winprob.DEFAULT_ROLLOUTS
    search_rollouts: int = winprob.SEARCH_ROLLOUTS
    edge: float = WINPROB_EDGE
    max_rounds: int = 2


@dataclass(frozen=True)
class MatchupPlan:
    """Both lineups, both win probabilities, and why they differ when they do."""

    max_ev: Lineup
    recommended: Lineup
    max_ev_outcome: winprob.MatchupOutcome
    recommended_outcome: winprob.MatchupOutcome
    swaps: tuple[LineupChange, ...]
    reason: str

    @property
    def differs(self) -> bool:
        return set(self.max_ev.starter_keys) != set(self.recommended.starter_keys)

    @property
    def win_probability_gain(self) -> float:
        return (
            self.recommended_outcome.win_probability
            - self.max_ev_outcome.win_probability
        )

    @property
    def points_given_up(self) -> float:
        return self.max_ev.total - self.recommended.total


def opponent_lineup(
    valuations: Mapping[str, WeeklyValuation],
    entries: Sequence[RosterEntry],
    settings: LeagueSettings,
) -> list[WeeklyValuation]:
    """What the opponent will most likely start: their best legal lineup.

    Assuming competence is the conservative error. Real leaguemates leave points on the
    bench -- that is exactly what the weekly counterfactual measures for *you* -- so this
    slightly overstates the opposition and slightly understates your win probability. The
    alternative is modelling how bad each manager is, which is a lot of machinery in
    service of being ruder about your friends than the evidence supports.
    """
    pool = [
        valuations[entry.player_key]
        for entry in entries
        if entry.player_key in valuations
        and entry.selected_position not in {"IR", "IR+", "IR-R", "NA"}
    ]
    board = optimal_lineup(pool, settings)
    by_key = {valuation.player_key: valuation for valuation in pool}
    return [by_key[key] for key in board.starter_keys if key in by_key]


def recommend_for_win(
    valuations: Mapping[str, WeeklyValuation],
    entries: Sequence[RosterEntry],
    settings: LeagueSettings,
    opponent: Sequence[WeeklyValuation],
    *,
    locked: Mapping[int, str] | None = None,
    config: WinProbConfig | None = None,
) -> MatchupPlan:
    """The lineup that most often wins *this* matchup, which is not always the biggest one.

    Two stages. The exact max-EV lineup comes first and is always reported: it is cheap,
    optimal against a well-defined objective, and the right answer most weeks. Then a
    local search asks whether trading some expected points for variance raises the chance
    of actually winning -- which it does when you are a heavy underdog, where a median
    outcome loses and only the tail is worth anything, and in reverse when you are heavily
    favoured and the job is to avoid a disaster rather than to score more.

    Only slot-for-bench swaps are searched. Reordering starters between slots cannot
    change anything: the team total is a sum over the *set* of starters, so a two-cycle
    among them produces an identical distribution.
    """
    settings_config = config or WinProbConfig()
    plan = start_sit(valuations, entries, settings, locked=locked)
    base = plan.optimal

    pool = {
        entry.player_key: valuations[entry.player_key]
        for entry in entries
        if entry.player_key in valuations
        and entry.selected_position not in {"IR", "IR+", "IR-R", "NA"}
    }

    # One seed for every comparison in the search, so candidates are judged against the
    # same simulated weeks. Re-seeding per candidate would compare each option against
    # different luck and pick whichever got the kindest draws.
    search_seed = zlib.crc32(
        ",".join(sorted([*pool, *(v.player_key for v in opponent)])).encode()
    )

    def evaluate(starters: Sequence[str], rollouts: int, seed: int) -> float:
        lineup_players = [pool[key] for key in starters if key in pool]
        return winprob.simulate_matchup(
            lineup_players, opponent, rollouts=rollouts, seed=seed
        ).win_probability

    current = base
    locked_keys = set((locked or {}).values())
    for _ in range(settings_config.max_rounds):
        baseline = evaluate(
            current.starter_keys, settings_config.search_rollouts, search_seed
        )
        best_gain = 0.0
        best: Lineup | None = None

        for candidate in _swap_candidates(current, pool, settings, locked_keys):
            gain = (
                evaluate(
                    candidate.starter_keys, settings_config.search_rollouts, search_seed
                )
                - baseline
            )
            if gain > best_gain:
                best_gain = gain
                best = candidate

        if best is None or best_gain <= settings_config.edge:
            break
        current = best

    # Judged again at full precision, against a seed the search never saw. A candidate
    # that only wins on the search's particular draws is exactly what this catches, and
    # falling back to max-EV when it fails is the conservative direction.
    report_seed = search_seed ^ 0x5EED
    base_outcome = winprob.simulate_matchup(
        [pool[k] for k in base.starter_keys if k in pool],
        opponent,
        rollouts=settings_config.rollouts,
        seed=report_seed,
    )
    def unchanged() -> MatchupPlan:
        return MatchupPlan(
            max_ev=base,
            recommended=base,
            max_ev_outcome=base_outcome,
            recommended_outcome=base_outcome,
            swaps=(),
            reason=_reason_for(base_outcome, 0.0),
        )

    if set(current.starter_keys) == set(base.starter_keys):
        return unchanged()

    current_outcome = winprob.simulate_matchup(
        [pool[k] for k in current.starter_keys if k in pool],
        opponent,
        rollouts=settings_config.rollouts,
        seed=report_seed,
    )
    gain = current_outcome.win_probability - base_outcome.win_probability
    if gain <= settings_config.edge:
        return unchanged()

    return MatchupPlan(
        max_ev=base,
        recommended=current,
        max_ev_outcome=base_outcome,
        recommended_outcome=current_outcome,
        swaps=_changes(valuations, base, current, min_gain=float("-inf")),
        reason=_reason_for(base_outcome, gain),
    )


def _swap_candidates(
    board: Lineup,
    pool: Mapping[str, WeeklyValuation],
    settings: LeagueSettings,
    locked_keys: set[str],
) -> list[Lineup]:
    """Every lineup one bench-for-starter swap away from this one."""
    starting = set(board.starter_keys)
    bench = [
        valuation
        for key, valuation in pool.items()
        if key not in starting and valuation.is_playable
    ]
    if not bench:
        return []

    candidates: list[Lineup] = []
    for slot, current_key in board.starters:
        if current_key in locked_keys:
            continue
        for replacement in bench:
            if not slot.eligible & set(replacement.eligible_positions):
                continue
            keys = [
                replacement.player_key if key == current_key else key
                for key in board.starter_keys
            ]
            players = [pool[key] for key in keys if key in pool]
            # Re-optimized rather than assembled by hand: swapping a receiver into a flex
            # can free a better arrangement of everyone else, and a lineup that is not
            # itself optimal for its own set would understate the candidate.
            candidates.append(optimal_lineup(players, settings))
    return candidates


def _reason_for(outcome: winprob.MatchupOutcome, gain: float) -> str:
    margin = outcome.margin_mean
    if gain <= 0.0:
        if outcome.win_probability >= 0.5:
            return (
                f"favoured by {margin:.0f}; the projected-best lineup is also the one "
                "that wins most often"
            )
        return (
            f"{abs(margin):.0f}-point underdog, but no bench swap raises your odds "
            "enough to be worth it"
        )
    if outcome.win_probability < 0.5:
        return (
            f"you are a {abs(margin):.0f}-point underdog; ceiling matters more than "
            f"median, and this raises your odds {gain:+.1%}"
        )
    return (
        f"you are favoured by {margin:.0f}; the safer lineup protects the lead "
        f"({gain:+.1%})"
    )


def slot_labels(settings: LeagueSettings) -> tuple[str, ...]:
    """Slot labels in lineup order, for anything rendering a lineup as a table."""
    return tuple(
        slot.position for slot in starting_slots(settings) if slot.position not in BENCH_SLOTS
    )
