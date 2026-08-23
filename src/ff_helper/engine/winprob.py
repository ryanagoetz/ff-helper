"""How much a lineup might score, and how often it beats the other one.

Projections are point estimates, and a point estimate cannot answer the question a weekly
matchup actually poses. Two lineups projected at 116 and 118 are not ranked by those
numbers if the first is a pile of steady veterans and the second is three boom-bust
receivers -- which of them you want depends entirely on whether you are favoured. This
module supplies the distribution the projection leaves out.

**Two sources of spread, kept apart.** ``WeeklyValuation.points_stdev`` measures how much
the *sources disagree* about a player. That is model uncertainty, and with a single
projection source it is legitimately zero. It is not the same quantity as how much a
player's real score varies week to week, which is enormous and exists even when every
source agrees perfectly. Adding them in quadrature is the honest composition; treating
either alone as "the" variance is the mistake this separation exists to prevent.

**The coefficients of variation below are the weakest numbers in the module.** They are
judgement calibrated against nothing, and they are meant to be replaced: once several
weeks of snapshots hold projections next to realized stats, fitting them is a regression
per position. ``fitted_cv`` reads a fitted table when one exists and falls back to these,
so the replacement is a data file rather than a code change.

**Correlation is cheap and matters more than the marginals.** Without it, a lineup's
variance is understated, every "risky" lineup looks safer than it is, and the two-stage
optimizer above this collapses back to maximizing points. Two levels are modelled: a
factor per NFL game (everyone in a shootout scores more) and a factor per passing offense
(a quarterback and his own receivers rise and fall together). Both matchup sides are
simulated in one pass so that a player of yours and a player of theirs in the same game
are correlated -- which is exactly the case where independent sampling misleads most,
because it is the case where a big game helps you both.

**Scores are lognormal, not gamma.** The plan called for a shifted gamma; a Gaussian
copula onto a gamma needs an inverse CDF this project has no dependency for, and adding
scipy for it would be a poor trade. A lognormal is right-skewed, strictly positive,
exactly moment-matchable, and takes the correlated normal directly -- so the correlation
composes for free. The known cost is a slightly heavy right tail, and that a defense which
scores negative cannot be represented.
"""

from __future__ import annotations

import json
import math
import random
import zlib
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field

from ff_helper.config import cache_dir
from ff_helper.season.valuation import WeeklyValuation

# Outcome variance as a fraction of the mean, by position. Receivers swing hardest among
# the skill positions because their scoring is touchdown-dependent and target counts are
# lumpy; quarterbacks are the steadiest because passing volume is not; defenses are the
# wildest thing anybody starts on purpose.
#
# Placeholders. See the module docstring -- these are the first thing to re-fit.
_WEEKLY_CV: dict[str, float] = {
    "QB": 0.36,
    "RB": 0.52,
    "WR": 0.58,
    "TE": 0.62,
    "K": 0.55,
    "DEF": 0.85,
}
_DEFAULT_CV = 0.55

# Correlation of the normal driving each player's score.
#   same game, either team      -> _GAME_RHO
#   same team's passing offense -> _GAME_RHO + _STACK_EXTRA
_GAME_RHO = 0.15
_STACK_EXTRA = 0.20

# Which positions ride the passing-offense factor. A running back in the same offense is
# closer to uncorrelated with his quarterback than the receivers are -- often negatively,
# since a team that throws all game is not handing off -- so he gets the game factor only.
_PASS_GAME = frozenset({"QB", "WR", "TE"})

DEFAULT_ROLLOUTS = 20000
# Used while searching lineups, where the comparison is between two very similar options
# and the absolute number matters less than the ordering.
SEARCH_ROLLOUTS = 2000

_FITTED_CV_FILE = "weekly-cv.json"


def fitted_cv() -> dict[str, float]:
    """Position CVs, fitted from stored weeks when available.

    Falls back to the hardcoded table, which is what makes this safe to ship before any
    fitting has happened: the model works from day one and improves when there is
    evidence, rather than waiting for it.
    """
    path = cache_dir() / _FITTED_CV_FILE
    if not path.exists():
        return dict(_WEEKLY_CV)
    try:
        fitted = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(_WEEKLY_CV)

    table = dict(_WEEKLY_CV)
    for position, value in (fitted or {}).items():
        # A fitted CV outside this range is a fitting bug, not a discovery.
        if isinstance(value, int | float) and 0.05 <= float(value) <= 2.0:
            table[str(position)] = float(value)
    return table


def player_sigma(
    valuation: WeeklyValuation, *, cv: dict[str, float] | None = None
) -> float:
    """Standard deviation of this player's score *given that he plays*.

    Model uncertainty and outcome variance added in quadrature. The conditioning matters:
    whether he plays is handled separately as zero-inflation, and folding it in here would
    make a doubtful player look merely volatile rather than possibly absent.
    """
    table = cv or fitted_cv()
    mean = _conditional_mean(valuation)
    outcome = table.get(valuation.position, _DEFAULT_CV) * mean
    model = valuation.points_stdev
    return math.sqrt(outcome * outcome + model * model)


def _conditional_mean(valuation: WeeklyValuation) -> float:
    """His projection if he plays, undoing the availability discount already applied.

    ``projected_points`` is already multiplied by availability, so using it directly would
    charge for the absence twice -- once by shrinking the mean and again by zero-inflating
    the draws.
    """
    if valuation.is_bye or valuation.availability <= 0.0:
        return 0.0
    return valuation.projected_points / valuation.availability


@dataclass(frozen=True)
class ScoreDistribution:
    """A simulated total, kept as sorted samples rather than a fitted shape."""

    mean: float
    sd: float
    samples: tuple[float, ...] = field(default=(), repr=False)

    def quantile(self, q: float) -> float:
        if not self.samples:
            return self.mean
        index = min(len(self.samples) - 1, max(0, int(q * len(self.samples))))
        return self.samples[index]

    def p_above(self, value: float) -> float:
        """Share of outcomes strictly above ``value``."""
        if not self.samples:
            return 0.0
        return 1.0 - bisect_left(self.samples, value) / len(self.samples)

    @property
    def floor(self) -> float:
        return self.quantile(0.10)

    @property
    def ceiling(self) -> float:
        return self.quantile(0.90)


@dataclass(frozen=True)
class MatchupOutcome:
    mine: ScoreDistribution
    theirs: ScoreDistribution
    win_probability: float
    margin_mean: float
    margin_sd: float

    @property
    def is_favoured(self) -> bool:
        return self.win_probability >= 0.5


def _game_key(valuation: WeeklyValuation) -> str:
    """An identifier for the NFL game a player is in, from either side of it.

    Two players in the same game must land on the same key regardless of which team's row
    named the opponent, so the pair is sorted. Without an opponent there is nothing to
    join on and the player is treated as being in a game of his own -- uncorrelated, which
    understates rather than invents.
    """
    team = (valuation.team or "").upper()
    opponent = (valuation.opponent or "").upper()
    if not team or not opponent:
        return f"solo:{valuation.player_key}"
    return "|".join(sorted((team, opponent)))


def simulate_matchup(
    mine: Sequence[WeeklyValuation],
    theirs: Sequence[WeeklyValuation],
    *,
    rollouts: int = DEFAULT_ROLLOUTS,
    seed: int | None = None,
    cv: dict[str, float] | None = None,
) -> MatchupOutcome:
    """Both lineups simulated together, sharing the games they are actually in.

    Simulating each side separately and comparing would treat your receiver and their
    quarterback in the same game as independent, which is precisely backwards -- a
    shootout lifts both of you and changes the margin far less than the totals.

    The seed defaults to a hash of both starter sets, so the same matchup gives the same
    number every time it is asked. A win probability that drifts between two runs of the
    same digest is one nobody can act on.
    """
    table = cv or fitted_cv()
    if seed is None:
        keys = sorted(v.player_key for v in [*mine, *theirs])
        seed = zlib.crc32(",".join(keys).encode())
    rng = random.Random(seed)

    my_players = [_prepare(v, table) for v in mine]
    their_players = [_prepare(v, table) for v in theirs]
    games = sorted({p.game for p in [*my_players, *their_players]})
    offenses = sorted({p.offense for p in [*my_players, *their_players] if p.offense})

    my_samples: list[float] = []
    their_samples: list[float] = []

    game_weight = math.sqrt(_GAME_RHO)
    stack_weight = math.sqrt(_STACK_EXTRA)

    for _ in range(rollouts):
        game_factor = {key: rng.gauss(0.0, 1.0) for key in games}
        offense_factor = {key: rng.gauss(0.0, 1.0) for key in offenses}
        my_samples.append(
            _score(my_players, game_factor, offense_factor, rng, game_weight, stack_weight)
        )
        their_samples.append(
            _score(their_players, game_factor, offense_factor, rng, game_weight, stack_weight)
        )

    mine_dist = _distribution(my_samples)
    theirs_dist = _distribution(their_samples)

    margins = [a - b for a, b in zip(my_samples, their_samples, strict=True)]
    wins = sum(1 for margin in margins if margin > 0.0)
    # A tie is a tie, not a win. Yahoo settles them by rule rather than by points, and
    # counting them as wins would flatter every projection by a fraction of a percent.
    ties = sum(1 for margin in margins if margin == 0.0)
    win_probability = (wins + 0.5 * ties) / len(margins) if margins else 0.0

    return MatchupOutcome(
        mine=mine_dist,
        theirs=theirs_dist,
        win_probability=win_probability,
        margin_mean=_mean(margins),
        margin_sd=_sd(margins),
    )


def team_distribution(
    starters: Sequence[WeeklyValuation],
    *,
    rollouts: int = DEFAULT_ROLLOUTS,
    seed: int | None = None,
    cv: dict[str, float] | None = None,
) -> ScoreDistribution:
    """One lineup's total, for when there is no opponent to compare against."""
    outcome = simulate_matchup([*starters], [], rollouts=rollouts, seed=seed, cv=cv)
    return outcome.mine


@dataclass(frozen=True)
class _Prepared:
    play_probability: float
    mu: float  # lognormal location
    sigma_log: float
    game: str
    offense: str


def _prepare(valuation: WeeklyValuation, table: dict[str, float]) -> _Prepared:
    """Moment-match a lognormal to this player's conditional mean and sd."""
    mean = _conditional_mean(valuation)
    if mean <= 0.0:
        return _Prepared(0.0, 0.0, 0.0, _game_key(valuation), "")

    sd = player_sigma(valuation, cv=table)
    # exp(mu + s^2/2) = mean and the variance identity below give the exact moments.
    variance_ratio = (sd / mean) ** 2
    sigma_log = math.sqrt(math.log1p(variance_ratio))
    mu = math.log(mean) - 0.5 * sigma_log * sigma_log

    offense = ""
    if valuation.position in _PASS_GAME and valuation.team:
        offense = valuation.team.upper()

    return _Prepared(
        play_probability=0.0 if valuation.is_bye else valuation.availability,
        mu=mu,
        sigma_log=sigma_log,
        game=_game_key(valuation),
        offense=offense,
    )


def _score(
    players: Sequence[_Prepared],
    game_factor: dict[str, float],
    offense_factor: dict[str, float],
    rng: random.Random,
    game_weight: float,
    stack_weight: float,
) -> float:
    total = 0.0
    for player in players:
        if player.sigma_log == 0.0 and player.mu == 0.0:
            continue
        # Zero-inflation: he either plays or he does not, and the draw is made before the
        # score. A doubtful player is not a small score, he is a coin flip on zero.
        if player.play_probability < 1.0 and rng.random() >= player.play_probability:
            continue

        shared = game_weight * game_factor[player.game]
        if player.offense:
            shared += stack_weight * offense_factor[player.offense]
        idiosyncratic = math.sqrt(max(0.0, 1.0 - shared_variance(player)))
        z = shared + idiosyncratic * rng.gauss(0.0, 1.0)
        total += math.exp(player.mu + player.sigma_log * z)
    return total


def shared_variance(player: _Prepared) -> float:
    """How much of this player's normal is explained by factors he shares with others."""
    return _GAME_RHO + (_STACK_EXTRA if player.offense else 0.0)


def _distribution(samples: list[float]) -> ScoreDistribution:
    if not samples:
        return ScoreDistribution(mean=0.0, sd=0.0, samples=())
    return ScoreDistribution(
        mean=_mean(samples),
        sd=_sd(samples),
        samples=tuple(sorted(samples)),
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sd(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)
