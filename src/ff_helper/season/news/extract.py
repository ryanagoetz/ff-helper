"""Turn article text into typed facts, deterministically and without much cleverness.

The design constraint is that this must be *checkable*. Every flag it produces carries the
sentence that produced it, and a human reading the digest has to be able to look at that
sentence and agree or disagree in one second. That rules out anything whose output cannot
be traced to a span of the source, which is most of what you would reach for first.

So: split into sentences, find player names, look for a trigger phrase, emit a typed flag.
No NLP dependency, no model, no embeddings, no scoring. Regex and the name crosswalk that
already exists.

**Player resolution goes through ``PlayerRegistry``**, which is the single most valuable
thing being reused here -- not because it saves code, but because of what it refuses. It
already declines to resolve an ambiguous abbreviated name (the Bijan/Brian Robinson case
its own docstring describes). "B. Robinson is out" attaching to the wrong Robinson would
be a confident, invisible lie that zeroes a good starter, and the crosswalk's existing
unwillingness to guess is exactly the behaviour wanted.

**The real failure modes are negation and tense**, not name matching:

    "Kelce was not ruled out"          -- the trigger is there, the fact is the opposite
    "was ruled out last week, but has been a full participant since"

Both are handled the cheap way: a window before the trigger is checked for negation or
past-tense markers, and a hit demotes the sentence from a flag to a caveat. That is
deliberately conservative -- it turns some real facts into caveats, which costs a little
accuracy and no correctness. The reverse mistake applies a multiplier that should not be
there, and the digest states it as fact.

**One flag per player per sentence.** A sentence saying two things about one player is
usually saying one thing twice.
"""

from __future__ import annotations

import re

from ff_helper.rankings.players import SourceRow
from ff_helper.season.news import flags as flag_vocabulary
from ff_helper.season.news.store import Article, Caveat, NewsFlag

# Sentence splitting. Deliberately crude: a split that is slightly wrong costs a little
# context around a trigger, where a dependency would cost a dependency.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")

# A capitalized run that might be a name. Allows the punctuation real names carry --
# apostrophes, hyphens, periods in initials -- and the generational suffixes.
_NAME_RUN = re.compile(
    r"\b(?:[A-Z][A-Za-z'’\-\.]+(?:\s+(?:Jr\.?|Sr\.?|II|III|IV))?)"
    r"(?:\s+[A-Z][A-Za-z'’\-\.]+(?:\s+(?:Jr\.?|Sr\.?|II|III|IV))?){0,2}\b"
)

# Words before a trigger that invert it. Checked in a window rather than the whole
# sentence, because "he is not expected back, and is ruled out" negates nothing.
_NEGATION = re.compile(
    r"\b(?:not|never|no longer|avoided|denied|dodged|escaped|isn'?t|wasn'?t|"
    r"won'?t be|hasn'?t been|doubt(?:s)? (?:that|whether))\b",
    re.IGNORECASE,
)

# Markers that the trigger describes a previous week rather than this one.
_PAST_TENSE = re.compile(
    r"\b(?:last week|previously|had been|has since|since returned|earlier this season|"
    r"a week ago|in week \d+|returned from|back from)\b",
    re.IGNORECASE,
)

# How far back from a trigger to look for the words above. Wide enough to catch the
# clause it belongs to, narrow enough not to reach into the previous one.
_WINDOW = 60

# Sentence fragments that are page furniture rather than reporting. A nav bar caught in an
# innerText blob should not become a caveat on every player it names.
_BOILERPLATE = re.compile(
    r"\b(?:sign in|subscribe|cookie|newsletter|advertisement|all rights reserved|"
    r"terms of service|privacy policy)\b",
    re.IGNORECASE,
)


def extract(
    article: Article,
    registry,
    *,
    position_of: dict[str, str] | None = None,
) -> tuple[list[NewsFlag], list[Caveat]]:
    """Pull typed flags and caveats out of one article.

    ``position_of`` maps player key to position, used only to keep position-scoped flags
    (severe weather) off players they should not apply to.
    """
    positions = position_of or {}
    found: dict[tuple[str, str], NewsFlag] = {}
    caveats: list[Caveat] = []
    seen_caveat: set[tuple[str, str]] = set()

    for sentence in _sentences(article.text):
        if _BOILERPLATE.search(sentence):
            continue

        mentioned = _players_in(sentence, registry)
        if not mentioned:
            continue

        for player_key in mentioned:
            flag = flag_vocabulary.match(sentence, position=positions.get(player_key, ""))
            if flag is None:
                key = (player_key, sentence)
                if key not in seen_caveat:
                    seen_caveat.add(key)
                    caveats.append(
                        Caveat(
                            article_id=article.article_id,
                            player_key=player_key,
                            quote=sentence,
                            week=article.week,
                            captured_at=article.captured_at,
                        )
                    )
                continue

            if _is_negated(sentence, flag) or _is_past_tense(sentence, flag):
                # The trigger is present but the sentence does not assert it about this
                # week. Demoted, not dropped -- you still want to see it.
                key = (player_key, sentence)
                if key not in seen_caveat:
                    seen_caveat.add(key)
                    caveats.append(
                        Caveat(
                            article_id=article.article_id,
                            player_key=player_key,
                            quote=sentence,
                            week=article.week,
                            captured_at=article.captured_at,
                        )
                    )
                continue

            slot = (player_key, flag)
            if slot in found:
                continue  # first statement of a fact wins; the rest is repetition
            found[slot] = NewsFlag(
                article_id=article.article_id,
                player_key=player_key,
                flag=flag,
                quote=sentence,
                week=article.week,
                captured_at=article.captured_at,
                confidence="explicit",
            )

    extracted = list(found.values())
    extracted.extend(_beneficiaries(article, extracted, registry, positions))
    return extracted, caveats


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_END.split(text or "")]
    return [part for part in parts if part]


def _players_in(sentence: str, registry) -> list[str]:
    """Every distinct player the crosswalk will confidently resolve in this sentence.

    Longest candidate runs are tried first so "Kenneth Walker III" is attempted before
    "Kenneth Walker", and a resolved span is consumed so one name cannot produce two
    players. Anything the registry declines to resolve is simply not a mention -- there is
    no fuzzy fallback here on purpose, because a wrong attribution is worse than a miss.
    """
    found: list[str] = []
    claimed: list[tuple[int, int]] = []

    candidates = sorted(
        _NAME_RUN.finditer(sentence),
        key=lambda m: (m.start() - m.end(), m.start()),  # longest first, then leftmost
    )
    for candidate in candidates:
        span = (candidate.start(), candidate.end())
        if any(start < span[1] and span[0] < end for start, end in claimed):
            continue

        name = candidate.group(0).strip()
        if len(name.split()) < 2:
            # A bare surname is not enough to identify anyone, and a bare first name is
            # worse. The registry would happily match "Chase" to one player today and a
            # different one after a waiver add.
            continue

        player = registry.find(SourceRow(name=name, position="", team="", source="news"))
        if player is None:
            continue
        claimed.append(span)
        if player.player_key not in found:
            found.append(player.player_key)
    return found


def _is_negated(sentence: str, flag: str) -> bool:
    return _preceded_by(sentence, flag, _NEGATION)


def _is_past_tense(sentence: str, flag: str) -> bool:
    return _preceded_by(sentence, flag, _PAST_TENSE)


def _preceded_by(sentence: str, flag: str, marker: re.Pattern[str]) -> bool:
    """Is the trigger for ``flag`` preceded, within the window, by one of these markers?"""
    spec = flag_vocabulary.spec_for(flag)
    for pattern in spec.patterns:
        hit = pattern.search(sentence)
        if hit is None:
            continue
        window = sentence[max(0, hit.start() - _WINDOW) : hit.start()]
        if marker.search(window):
            return True
        # A marker immediately after the trigger negates it too: "ruled out last week".
        tail = sentence[hit.end() : hit.end() + _WINDOW]
        if marker is _PAST_TENSE and marker.search(tail):
            return True
    return False


def _beneficiaries(
    article: Article,
    extracted: list[NewsFlag],
    registry,
    positions: dict[str, str],
) -> list[NewsFlag]:
    """Derive the teammate-benefits flag from somebody else's absence.

    Only fires for a same-team, same-position player, and never for the ruled-out player
    himself. The quote is the *causing* sentence, prefixed so the digest does not appear
    to claim the article said something about the beneficiary that it did not.

    This is the one inferred flag, and it is marked as such: the article never mentions
    the player it applies to, so the reader deserves to know the tool joined two facts
    rather than read one.
    """
    out_keys = [flag.player_key for flag in extracted if flag.flag == flag_vocabulary.RULED_OUT]
    if not out_keys:
        return []

    quotes = {
        flag.player_key: flag.quote
        for flag in extracted
        if flag.flag == flag_vocabulary.RULED_OUT
    }
    already = {(flag.player_key, flag.flag) for flag in extracted}

    derived: list[NewsFlag] = []
    for out_key in out_keys:
        absent = registry.by_key.get(out_key)
        if absent is None or not absent.team_abbr:
            continue
        position = absent.primary_position
        # Only positions where one player's absence genuinely redistributes to a
        # teammate. A quarterback going out changes the offense; it does not hand his
        # backup the same production, and pretending otherwise is the classic mistake.
        if position not in {"RB", "WR", "TE"}:
            continue

        for player in registry.by_key.values():
            if player.player_key == out_key:
                continue
            if player.team_abbr != absent.team_abbr:
                continue
            if player.primary_position != position:
                continue
            slot = (player.player_key, flag_vocabulary.TEAMMATE_OUT_BENEFICIARY)
            if slot in already:
                continue
            already.add(slot)
            derived.append(
                NewsFlag(
                    article_id=article.article_id,
                    player_key=player.player_key,
                    flag=flag_vocabulary.TEAMMATE_OUT_BENEFICIARY,
                    quote=f"[{absent.full_name} ruled out] {quotes.get(out_key, '')}",
                    week=article.week,
                    captured_at=article.captured_at,
                    confidence="inferred",
                )
            )
    return derived
