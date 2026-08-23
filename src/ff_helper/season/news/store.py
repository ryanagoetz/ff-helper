"""Where captured articles and the facts pulled out of them live.

Append-only JSONL, one record per line, never rewritten. Three reasons, in order of how
much they matter:

**Reproducibility.** A digest is a claim about what was knowable at a moment. "Start
Odunze" written at 6pm Tuesday and "bench Odunze" written at 9pm are both correct if news
broke at 7. A log that can be edited cannot answer which was which, and an append-only one
answers it for free -- ``as_of`` replays the store as it stood at any timestamp, which is
what makes the whole news model falsifiable rather than merely plausible.

**Crash safety.** A partial write loses the last line, not the file. The alternative --
read, mutate, rewrite -- loses everything if the process dies between truncate and write,
and the moment that is most likely is the moment the most is being written.

**Expiry is a filter, not a delete.** A flag that has aged out stops applying and stays on
disk. Deleting it would destroy exactly the record needed to ask, in December, whether
these multipliers were ever any good.

The store holds three kinds of record. ``Article`` is the raw capture, kept whole so
extraction can be re-run without re-visiting the page. ``NewsFlag`` is a typed fact with
its causing sentence attached. ``Caveat`` is everything the vocabulary had no word for --
mentioned beside a recommendation, never folded into a number.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ff_helper.config import cache_dir
from ff_helper.season.news import flags as flag_vocabulary

# Long enough to be the evidence, short enough that a digest stays readable and one
# runaway page cannot bloat the log.
MAX_QUOTE = 300


@dataclass(frozen=True)
class Article:
    """One captured page. Kept whole so extraction can be re-run without re-fetching."""

    article_id: str
    url: str
    title: str
    source: str
    captured_at: float
    week: int
    text: str
    published_at: float | None = None


@dataclass(frozen=True)
class NewsFlag:
    """A typed fact about one player, with the sentence that produced it.

    ``multiplier`` is not accepted from the caller -- it is looked up from the vocabulary
    in ``__post_init__``. That is the difference between a bounded system and one where
    any code path that can construct a flag can also invent a number.
    """

    article_id: str
    player_key: str
    flag: str
    quote: str
    week: int
    captured_at: float
    # "explicit" when the trigger phrasing matched directly, "inferred" when it came
    # through a weaker path. The digest prints inferred flags in their own section.
    confidence: str = "explicit"
    multiplier: float = field(default=1.0, compare=False)

    def __post_init__(self) -> None:
        # Validates the name and fixes the multiplier in one step. A flag name with no
        # entry would otherwise price silently as 1.0 and look like it had been applied.
        object.__setattr__(self, "multiplier", flag_vocabulary.multiplier_for(self.flag))
        object.__setattr__(self, "quote", self.quote.strip()[:MAX_QUOTE])

    @property
    def expires_week(self) -> int:
        return self.week + flag_vocabulary.expires_after(self.flag)

    def is_live(self, week: int) -> bool:
        return self.week <= week < self.expires_week

    def age_hours(self, now: float | None = None) -> float:
        return ((now if now is not None else time.time()) - self.captured_at) / 3600.0


@dataclass(frozen=True)
class Caveat:
    """A player mentioned in a way the vocabulary has no word for.

    Surfaced beside the recommendation, never applied to it. This is where everything the
    typed layer deliberately cannot express ends up, rather than being discarded -- the
    tool says "there is something here I did not price" instead of implying there was
    nothing to price.
    """

    article_id: str
    player_key: str
    quote: str
    week: int
    captured_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "quote", self.quote.strip()[:MAX_QUOTE])


def article_id_for(url: str, title: str, captured_at: float) -> str:
    """Stable id for a capture.

    Keyed on the URL when there is one so re-posting the same page is idempotent -- the
    userscript fires on navigation and a page revisited an hour later must not stack a
    second copy of every flag on it. Without a URL (a hand paste) the timestamp
    distinguishes captures, because two pastes of different text are two articles.
    """
    basis = url.strip() or f"{title.strip()}|{captured_at:.0f}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def default_path(league_key: str, season: str) -> Path:
    safe = league_key.replace("/", "_")
    return cache_dir() / f"news-{safe}-{season}.jsonl"


class NewsStore:
    """Append-only log of articles, flags and caveats for one league-season."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.articles: dict[str, Article] = {}
        self.flags: list[NewsFlag] = []
        self.caveats: list[Caveat] = []

    @classmethod
    def load(cls, league_key: str, season: str, path: Path | None = None) -> NewsStore:
        store = cls(path or default_path(league_key, season))
        if not store.path.exists():
            return store

        for line in store.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # One torn line -- almost certainly the last, from a crash mid-append --
                # must not cost the rest of the season's news.
                continue
            store._absorb(record)
        return store

    @classmethod
    def empty(cls) -> NewsStore:
        """A store backed by nothing, for callers running with news switched off."""
        return cls(Path("/dev/null"))

    def _absorb(self, record: dict) -> None:
        kind = record.get("kind")
        try:
            if kind == "article":
                article = Article(**record["data"])
                self.articles[article.article_id] = article
            elif kind == "flag":
                self.flags.append(NewsFlag(**record["data"]))
            elif kind == "caveat":
                self.caveats.append(Caveat(**record["data"]))
        except (TypeError, KeyError, flag_vocabulary.UnknownFlag):
            # A record written by a version that knew a field or a flag this one does not.
            # Skipped rather than crashing the digest; the raw line stays on disk.
            return

    def has_article(self, article_id: str) -> bool:
        return article_id in self.articles

    def append(
        self,
        article: Article,
        flags: list[NewsFlag],
        caveats: list[Caveat],
    ) -> bool:
        """Record one capture. Returns False if this article was already stored.

        Idempotent on ``article_id`` so a userscript that re-fires on the same page adds
        nothing. The flags are unchanged by a re-post rather than duplicated, which is
        what keeps ``combine`` from seeing the same fact three times.
        """
        if self.has_article(article.article_id):
            return False

        lines = [_line("article", asdict(article))]
        lines.extend(_line("flag", _flag_dict(flag)) for flag in flags)
        lines.extend(_line("caveat", asdict(caveat)) for caveat in caveats)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("".join(lines))

        self.articles[article.article_id] = article
        self.flags.extend(flags)
        self.caveats.extend(caveats)
        return True

    # -- reads -------------------------------------------------------------------------

    def flags_for(
        self, player_key: str, week: int, *, as_of: float | None = None
    ) -> list[NewsFlag]:
        """Live flags for one player, most recent first.

        ``as_of`` restricts to what had been captured by a moment in time, which is how a
        backtest asks "what would Tuesday's digest have said" without the answer being
        contaminated by Thursday's news.

        Same-flag duplicates are collapsed to the most recent. Two articles reporting one
        injury are one fact, and ``combine`` would otherwise be handed it twice -- which
        the family rule mostly absorbs, but not for flags in different families.
        """
        live = [
            flag
            for flag in self.flags
            if flag.player_key == player_key
            and flag.is_live(week)
            and (as_of is None or flag.captured_at <= as_of)
        ]
        live.sort(key=lambda flag: -flag.captured_at)

        seen: set[str] = set()
        deduped: list[NewsFlag] = []
        for flag in live:
            if flag.flag in seen:
                continue
            seen.add(flag.flag)
            deduped.append(flag)
        return deduped

    def caveats_for(
        self, player_key: str, week: int, *, as_of: float | None = None
    ) -> list[Caveat]:
        found = [
            caveat
            for caveat in self.caveats
            if caveat.player_key == player_key
            and caveat.week == week
            and (as_of is None or caveat.captured_at <= as_of)
        ]
        found.sort(key=lambda caveat: -caveat.captured_at)
        return found

    def multiplier_for(
        self, player_key: str, week: int, *, as_of: float | None = None
    ) -> tuple[float, list[NewsFlag]]:
        """The combined multiplier, and only the flags that actually produced it.

        The second half is the point. A multiplier without the sentences behind it is a
        number nobody can check, and the flags returned here are exactly the ones
        ``combine`` used -- not every flag on the player.
        """
        live = self.flags_for(player_key, week, as_of=as_of)
        if not live:
            return 1.0, []

        multiplier = flag_vocabulary.combine([flag.flag for flag in live])
        return multiplier, self._surviving(live)

    def adjustment_for(
        self, player_key: str, week: int, *, as_of: float | None = None
    ) -> tuple[float, float, list[NewsFlag]]:
        """``(availability multiplier, role/context multiplier, flags behind them)``.

        Split because the caller has to reconcile the availability half against Yahoo's
        own injury designation rather than multiply by it -- see ``combine_by_family``.
        """
        live = self.flags_for(player_key, week, as_of=as_of)
        if not live:
            return 1.0, 1.0, []

        by_family = flag_vocabulary.combine_by_family([flag.flag for flag in live])
        availability = by_family.pop(flag_vocabulary.AVAILABILITY, 1.0)

        other = 1.0
        for multiplier in by_family.values():
            other *= multiplier
        return availability, flag_vocabulary.clamp(other), self._surviving(live)

    def _surviving(self, live: list[NewsFlag]) -> list[NewsFlag]:
        surviving = set(flag_vocabulary.dominant([flag.flag for flag in live]))
        return [flag for flag in live if flag.flag in surviving]

    def article(self, article_id: str) -> Article | None:
        return self.articles.get(article_id)

    def players_with_news(self, week: int, *, as_of: float | None = None) -> set[str]:
        return {
            flag.player_key
            for flag in self.flags
            if flag.is_live(week) and (as_of is None or flag.captured_at <= as_of)
        }

    @property
    def is_empty(self) -> bool:
        return not self.articles


def _line(kind: str, data: dict) -> str:
    return json.dumps({"kind": kind, "data": data}, ensure_ascii=False) + "\n"


def _flag_dict(flag: NewsFlag) -> dict:
    # multiplier is derived, not stored: writing it would let a stale file disagree with
    # the vocabulary, and the vocabulary is the authority.
    data = asdict(flag)
    data.pop("multiplier", None)
    return data
