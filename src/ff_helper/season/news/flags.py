"""The closed vocabulary of things news is allowed to say about a projection.

This file is the whole safety argument for letting article text near a lineup decision.

An LLM reading a beat report and revising a number would be more flexible than anything
here and completely untestable: you could never say why a projection moved, never
reproduce it, and never check whether the reading was any good. So the pipeline is split.
Free text produces *typed facts* from this list; each fact carries a multiplier that is a
constant **in this file**; and anything the list has no word for becomes a caveat printed
beside the recommendation rather than a number folded into it.

Four invariants, each of which is tested:

1. **No multiplier is computed.** Not by a model, not by a regression, not by a heuristic.
   They are literals below, chosen once, arguable in review, and changeable only here.
2. **Same-family flags do not stack.** "Questionable" and "limited snaps" describe one
   underlying injury from two angles; multiplying them would charge for it twice. Within a
   family exactly one flag applies -- the most pessimistic one.
3. **The product is clamped** to [0.0, 1.5]. Worst case, everything the news layer knows
   can halve a projection or add half again, with every contributing sentence printed.
4. **RULED_OUT is absorbing.** Zero short-circuits, and no amount of good news lifts it.

The pessimism in (2) is deliberate and asymmetric, in the same spirit as the rest of the
app taking sides on which failure it prefers. Starting a player who does not play is a
guaranteed zero in a slot you had alternatives for. Benching a player who goes off costs
you the difference against your actual starter, which is smaller and rarer. When two
readings of the same injury disagree, the tool takes the one that risks the smaller loss.

The multipliers themselves are judgement, not measurement, and they are the first thing
that should be re-fitted once enough weeks are on disk. Until then the acceptance test is
blunt: if applying flags does not reduce projection error against realized scores, they
ship as caveats with every multiplier set to 1.0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Flag names. Strings rather than an Enum because they are written to disk in an
# append-only log that has to stay readable years later, and an Enum member that gets
# renamed silently orphans every record that used it.
RULED_OUT = "RULED_OUT"
DOUBTFUL = "DOUBTFUL"
QUESTIONABLE = "QUESTIONABLE"
LIMITED_SNAPS = "LIMITED_SNAPS"
PROMOTED_TO_STARTER = "PROMOTED_TO_STARTER"
DEMOTED_FROM_STARTER = "DEMOTED_FROM_STARTER"
BACKFIELD_COMMITTEE = "BACKFIELD_COMMITTEE"
LEAD_BACK = "LEAD_BACK"
TEAMMATE_OUT_BENEFICIARY = "TEAMMATE_OUT_BENEFICIARY"
WEATHER_SEVERE = "WEATHER_SEVERE"

# Families. One flag per family applies; families multiply with each other.
AVAILABILITY = "availability"
ROLE = "role"
CONTEXT = "context"

# The clamp. Every adjustment the news layer can make lives inside this band, so the worst
# a misread sentence can do is bounded and visible.
TOTAL_FLOOR = 0.0
TOTAL_CEILING = 1.5


@dataclass(frozen=True)
class FlagSpec:
    name: str
    multiplier: float
    family: str
    # Weeks the flag stays live, counted from the week it was captured in. An injury
    # designation is re-issued weekly; a change of role persists until contradicted.
    expires_after_weeks: int
    patterns: tuple[re.Pattern[str], ...]
    # Positions the flag can apply to. Empty means any. Severe weather is a passing-game
    # story; it does very little to a workhorse back's carries.
    positions: frozenset[str] = field(default_factory=frozenset)
    # Set for flags that are never matched from text directly. TEAMMATE_OUT_BENEFICIARY is
    # inferred from another player's RULED_OUT, so no phrasing should ever produce it.
    derived_only: bool = False


def _patterns(*phrases: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(phrase, re.IGNORECASE) for phrase in phrases)


VOCABULARY: dict[str, FlagSpec] = {
    RULED_OUT: FlagSpec(
        name=RULED_OUT,
        multiplier=0.0,
        family=AVAILABILITY,
        expires_after_weeks=1,
        patterns=_patterns(
            r"\bruled out\b",
            r"\bwill not play\b",
            r"\bwon'?t play\b",
            r"\bhas been declared inactive\b",
            r"\bis inactive\b",
            r"\bplaced on (?:the )?(?:injured reserve|ir)\b",
            r"\bto (?:the )?injured reserve\b",
            r"\bwill miss (?:the|this|sunday'?s) (?:game|week)\b",
            r"\bout for (?:the season|sunday|the game)\b",
        ),
    ),
    DOUBTFUL: FlagSpec(
        name=DOUBTFUL,
        multiplier=0.35,
        family=AVAILABILITY,
        expires_after_weeks=1,
        patterns=_patterns(r"\bdoubtful\b"),
    ),
    QUESTIONABLE: FlagSpec(
        name=QUESTIONABLE,
        multiplier=0.80,
        family=AVAILABILITY,
        expires_after_weeks=1,
        patterns=_patterns(
            r"\bquestionable\b",
            r"\bgame[- ]time decision\b",
            r"\bdid not practice\b",
        ),
    ),
    LIMITED_SNAPS: FlagSpec(
        name=LIMITED_SNAPS,
        multiplier=0.75,
        family=AVAILABILITY,
        expires_after_weeks=1,
        patterns=_patterns(
            r"\bsnap count\b",
            r"\bpitch count\b",
            r"\bsnap limit\b",
            r"\beased back\b",
            r"\bon a limited\b",
            r"\bwork(?:ed|ing)? back into\b",
        ),
    ),
    PROMOTED_TO_STARTER: FlagSpec(
        name=PROMOTED_TO_STARTER,
        multiplier=1.30,
        family=ROLE,
        expires_after_weeks=3,
        patterns=_patterns(
            r"\bwill start\b",
            r"\bnamed the (?:starter|starting)\b",
            r"\bwill be the starter\b",
            r"\btakes over as\b",
            r"\bgets the start\b",
            r"\bpromoted to (?:the )?start(?:er|ing)\b",
        ),
    ),
    DEMOTED_FROM_STARTER: FlagSpec(
        name=DEMOTED_FROM_STARTER,
        multiplier=0.60,
        family=ROLE,
        expires_after_weeks=3,
        patterns=_patterns(
            r"\bbenched\b",
            r"\blost (?:his|the) (?:starting )?job\b",
            r"\bloses (?:his|the) (?:starting )?job\b",
            r"\bdemoted\b",
            r"\bmoved to (?:the )?(?:second|back)up\b",
        ),
    ),
    BACKFIELD_COMMITTEE: FlagSpec(
        name=BACKFIELD_COMMITTEE,
        multiplier=0.85,
        family=ROLE,
        expires_after_weeks=3,
        patterns=_patterns(
            r"\bcommittee\b",
            r"\bsplit (?:the )?(?:carries|touches|work)\b",
            r"\btimeshare\b",
            r"\btime ?share\b",
            r"\brotation\b",
        ),
    ),
    LEAD_BACK: FlagSpec(
        name=LEAD_BACK,
        multiplier=1.20,
        family=ROLE,
        expires_after_weeks=3,
        patterns=_patterns(
            r"\bbell ?cow\b",
            r"\blead back\b",
            r"\bworkhorse\b",
            r"\bevery[- ]down (?:back|role)\b",
        ),
    ),
    TEAMMATE_OUT_BENEFICIARY: FlagSpec(
        name=TEAMMATE_OUT_BENEFICIARY,
        multiplier=1.15,
        family=CONTEXT,
        expires_after_weeks=1,
        patterns=(),
        derived_only=True,
    ),
    WEATHER_SEVERE: FlagSpec(
        name=WEATHER_SEVERE,
        multiplier=0.85,
        family=CONTEXT,
        expires_after_weeks=1,
        patterns=(),
        # A quarterback and his receivers lose a great deal in a gale; a running back
        # often gains volume. Applying it to everyone would price the wrong thing.
        positions=frozenset({"QB", "WR", "TE"}),
        # Not matched from prose, and this is a correction rather than an omission.
        #
        # Weather is a property of a *game*, and this extractor is scoped to a player and
        # a sentence. Articles reflect that: "Forecasters expect sustained winds of 25 mph
        # in Buffalo" names no player at all, and the players it bears on turn up two
        # sentences later. Bridging that gap needs a rule like "apply to anyone mentioned
        # nearby", which is precisely the kind of guessing the extractor refuses to do
        # everywhere else -- and a mis-attributed weather flag would quietly cut a
        # quarterback in a dome.
        #
        # The right input is a forecast keyed to the game, joined on the player's team.
        # The flag stays defined so that source has somewhere to land; until then it never
        # fires, which is the honest state of affairs rather than a half-working one.
        derived_only=True,
    ),
}

# Names only, for validating anything read back off disk.
KNOWN_FLAGS = frozenset(VOCABULARY)

# Flags that can be matched from article text, in the order they are tried. Availability
# first so the strongest statement about a player wins before weaker ones are considered.
_MATCH_ORDER = (
    RULED_OUT,
    DOUBTFUL,
    QUESTIONABLE,
    LIMITED_SNAPS,
    DEMOTED_FROM_STARTER,
    PROMOTED_TO_STARTER,
    BACKFIELD_COMMITTEE,
    LEAD_BACK,
)


class UnknownFlag(ValueError):
    """A flag name with no entry here. Loud, because it would otherwise price as 1.0."""


def spec_for(flag: str) -> FlagSpec:
    try:
        return VOCABULARY[flag]
    except KeyError as exc:
        raise UnknownFlag(
            f"{flag!r} is not a known news flag. Known: {', '.join(sorted(KNOWN_FLAGS))}"
        ) from exc


def multiplier_for(flag: str) -> float:
    """The one place a flag's multiplier comes from. Never passed in, always looked up."""
    return spec_for(flag).multiplier


def expires_after(flag: str) -> int:
    return spec_for(flag).expires_after_weeks


def match(sentence: str, *, position: str = "") -> str | None:
    """The strongest flag this sentence states, or None.

    One flag per sentence. A sentence saying two things about one player is usually
    saying one thing twice ("questionable, and a game-time decision"), and taking both
    would charge for it twice -- which the family rule would undo anyway.
    """
    for flag in _MATCH_ORDER:
        spec = VOCABULARY[flag]
        if spec.positions and position and position not in spec.positions:
            continue
        if any(pattern.search(sentence) for pattern in spec.patterns):
            return flag
    return None


def combine(flags: list[str]) -> float:
    """Fold a player's live flags into one bounded multiplier.

    One flag per family (the most pessimistic), families multiplied, product clamped.
    ``RULED_OUT`` short-circuits: nothing lifts a player who is not playing.
    """
    if not flags:
        return 1.0
    if RULED_OUT in flags:
        return 0.0

    by_family: dict[str, float] = {}
    for flag in flags:
        spec = spec_for(flag)
        current = by_family.get(spec.family)
        if current is None or spec.multiplier < current:
            by_family[spec.family] = spec.multiplier

    product = 1.0
    for multiplier in by_family.values():
        product *= multiplier
    return max(TOTAL_FLOOR, min(TOTAL_CEILING, product))


def combine_by_family(flags: list[str]) -> dict[str, float]:
    """One multiplier per family, most pessimistic within each. No cross-family product.

    Callers need the split because the families are not interchangeable. An availability
    flag says the same thing as Yahoo's injury designation, so the two must be reconciled
    rather than multiplied -- a player Yahoo lists as questionable, whom an article also
    calls questionable, has one injury and should be charged for it once. Role and
    context flags say something Yahoo does not, so they compose on top.
    """
    if not flags:
        return {}
    if RULED_OUT in flags:
        return {AVAILABILITY: 0.0}

    by_family: dict[str, float] = {}
    for flag in flags:
        spec = spec_for(flag)
        current = by_family.get(spec.family)
        if current is None or spec.multiplier < current:
            by_family[spec.family] = spec.multiplier
    return by_family


def clamp(multiplier: float) -> float:
    return max(TOTAL_FLOOR, min(TOTAL_CEILING, multiplier))


def dominant(flags: list[str]) -> list[str]:
    """The flags that actually survive ``combine``, for explaining the number.

    A digest that prints four sentences and applies two of them is worse than one that
    prints the two it used -- the reader cannot tell which is which, so they trust none.
    """
    if not flags:
        return []
    if RULED_OUT in flags:
        return [RULED_OUT]

    winner: dict[str, str] = {}
    for flag in flags:
        spec = spec_for(flag)
        current = winner.get(spec.family)
        if current is None or spec.multiplier < multiplier_for(current):
            winner[spec.family] = flag
    return [winner[family] for family in sorted(winner)]
