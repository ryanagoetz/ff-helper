"""Player identity across sources.

Every ranking source spells players differently: "Kenneth Walker III" / "Ken Walker III",
"D.J. Moore" / "DJ Moore", "Marvin Harrison Jr." / "Marvin Harrison". A missed match does
not raise -- it silently drops the player from every recommendation, which is the worst
possible failure mode for this app. So matching is explicit, layered, and *reported on*
(see ``scripts/fetch_rankings.py``) rather than trusted.

Yahoo is the canonical registry: it is the platform the draft happens on, so its player
keys are the identifiers the rest of the app uses.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ff_helper.yahoo.models import YahooPlayer

# Generational suffixes carry no identifying information and are inconsistently included.
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Short forms that are not simple prefixes of the full name, so the prefix rule below
# cannot catch them. Stored one way and compared symmetrically.
_NICKNAMES: dict[str, str] = {
    "mike": "michael",
    "bill": "william",
    "will": "william",
    "bob": "robert",
    "rob": "robert",
    "dick": "richard",
    "rick": "richard",
    "jim": "james",
    "joe": "joseph",
    "tony": "anthony",
    "nick": "nicholas",
    "tom": "thomas",
    "dan": "daniel",
    "matt": "matthew",
    "greg": "gregory",
    "josh": "joshua",
    "zach": "zachary",
    "cam": "cameron",
    "gabe": "gabriel",
    "isi": "isaiah",
}


def _first_names_compatible(a: str, b: str) -> bool:
    """Whether two first names plausibly refer to the same person.

    Covers the two ways sources disagree: truncation ("Ken" for "Kenneth", "Chris" for
    "Christopher") and substitution ("Mike" for "Michael"). Requiring three characters
    stops single initials from matching everything.
    """
    if a == b:
        return True
    if _NICKNAMES.get(a) == b or _NICKNAMES.get(b) == a:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 3 and longer.startswith(shorter)


# Team abbreviations differ per source. Normalize onto Yahoo's uppercase form.
TEAM_ALIASES: dict[str, str] = {
    "JAC": "JAX",
    "JAG": "JAX",
    "WSH": "WAS",
    "WFT": "WAS",
    "LAR": "LAR",
    "LA": "LAR",
    "STL": "LAR",
    "SD": "LAC",
    "OAK": "LV",
    "LVR": "LV",
    "TAM": "TB",
    "KAN": "KC",
    "SFO": "SF",
    "GNB": "GB",
    "NWE": "NE",
    "NOR": "NO",
    "ARZ": "ARI",
    "BLT": "BAL",
    "HST": "HOU",
    "CLV": "CLE",
}

# Defenses are named by city, nickname, or abbreviation depending on the source.
DST_POSITIONS = {"DST", "D/ST", "DEF"}


def normalize_team(team: str | None) -> str:
    if not team:
        return ""
    key = team.strip().upper()
    return TEAM_ALIASES.get(key, key)


def normalize_position(position: str | None) -> str:
    if not position:
        return ""
    key = position.strip().upper()
    # Strip FantasyPros' positional rank suffix, e.g. "WR1" -> "WR".
    key = re.sub(r"\d+$", "", key)
    if key in DST_POSITIONS:
        return "DEF"
    if key == "PK":
        return "K"
    return key


def normalize_name(name: str) -> str:
    """Collapse a display name to a comparable key.

    Strips accents, punctuation, and generational suffixes, then lowercases. "Ja'Marr
    Chase" and "JaMarr Chase" both become "jamarr chase".
    """
    if not name:
        return ""
    # Decompose accents and drop the combining marks.
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # Drop anything that is not a letter, digit or space.
    cleaned = re.sub(r"[^a-zA-Z0-9 ]", "", ascii_name).lower()
    parts = [part for part in cleaned.split() if part]
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return " ".join(parts)


def name_variants(name: str) -> list[str]:
    """Alternate keys a source might use for the same player, strongest first.

    Covers the "D.J. Moore" vs "DJ Moore" family (already handled by punctuation
    stripping) plus first-initial forms like "K. Walker".

    **Order is load-bearing, and this used to be a set.** The initial form collapses
    "Bijan Robinson" and "Brian Robinson" onto the same key -- and they are both running
    backs on Atlanta, so position and team cannot separate them either. With arbitrary
    iteration order, the weakest variant could be tried first and quietly resolve one
    player to the other: Bijan's projection got averaged with Brian's and his ADP of 2
    with Brian's 155, valuing the second overall pick as a mid-round back. The full name
    must always be tried before any abbreviation of it.
    """
    base = normalize_name(name)
    variants = [base]
    parts = base.split()
    if len(parts) >= 2:
        # Dropping a middle name still identifies a person; an initial does not.
        if len(parts) > 2:
            variants.append(f"{parts[0]} {parts[-1]}")
        variants.append(f"{parts[0][0]} {' '.join(parts[1:])}")
    return variants


def is_initial_form(variant: str) -> bool:
    """Whether a variant has been reduced to a first initial, e.g. "b robinson"."""
    head = variant.split(" ", 1)[0]
    return len(head) == 1


@dataclass(frozen=True)
class SourceRow:
    """One player's data from one external source, already normalized."""

    name: str
    position: str
    team: str
    source: str
    adp: float | None = None
    adp_stdev: float | None = None
    ecr: float | None = None
    tier: int | None = None
    projected_points: float | None = None
    # Average auction price. The auction analog of ADP: what the room will pay, which is
    # a different question from what the player is worth.
    auction_cost: float | None = None
    stats: dict[str, float] = field(default_factory=dict)


@dataclass
class MatchReport:
    """What the crosswalk did, so unmatched players are visible instead of silent."""

    matched: dict[str, str] = field(default_factory=dict)  # source row name -> player_key
    fuzzy: list[tuple[str, str, float]] = field(default_factory=list)
    unmatched: list[SourceRow] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        total = len(self.matched) + len(self.unmatched)
        return len(self.matched) / total if total else 1.0


class PlayerRegistry:
    """Canonical Yahoo players, with lookup by normalized name."""

    # Below this similarity we would rather report a miss than invent a match.
    FUZZY_THRESHOLD = 0.88

    def __init__(self, players: list[YahooPlayer]) -> None:
        self.players = players
        self.by_key = {player.player_key: player for player in players}
        self._index: dict[tuple[str, str], list[YahooPlayer]] = {}
        self._by_name: dict[str, list[YahooPlayer]] = {}
        self._by_surname: dict[tuple[str, str], list[YahooPlayer]] = {}

        for player in players:
            position = normalize_position(player.primary_position)
            for variant in name_variants(player.full_name):
                self._index.setdefault((variant, position), []).append(player)
                self._by_name.setdefault(variant, []).append(player)

            parts = normalize_name(player.full_name).split()
            if len(parts) >= 2:
                self._by_surname.setdefault((parts[-1], position), []).append(player)

    def find(self, row: SourceRow) -> YahooPlayer | None:
        """Resolve a source row to a Yahoo player, or None."""
        position = normalize_position(row.position)
        team = normalize_team(row.team)

        for variant in name_variants(row.name):
            # An initial is not an identity. "b robinson" fits both Bijan and Brian
            # Robinson, who are both Atlanta running backs, so neither position nor team
            # breaks the tie -- and picking one merges two players into a single ruined
            # valuation. Require the abbreviation to point at exactly one player.
            strict = is_initial_form(variant)

            # 1. Exact on name + position -- the common case.
            candidates = self._index.get((variant, position))
            if candidates:
                match = _disambiguate(candidates, team, unique_only=strict)
                if match is not None:
                    return match

            # 2. Name only. Positions disagree legitimately (a WR listed as a RB in one
            #    source), so a confident name match still beats no match.
            candidates = self._by_name.get(variant)
            if candidates:
                match = _disambiguate(candidates, team, unique_only=strict)
                if match is not None:
                    return match

        # 3. Same surname and position, with a compatible first name. This is what
        #    catches "Ken Walker" against "Kenneth Walker": string similarity scores that
        #    pair at 0.83, below any threshold loose enough to be safe, but the surname
        #    plus a prefix relation on the first name is strong evidence on its own.
        parts = normalize_name(row.name).split()
        if len(parts) >= 2:
            candidates = self._by_surname.get((parts[-1], position), [])
            compatible = [
                candidate
                for candidate in candidates
                if _first_names_compatible(parts[0], normalize_name(candidate.full_name).split()[0])
            ]
            if compatible:
                return _disambiguate(compatible, team)

        return None

    def find_fuzzy(self, row: SourceRow) -> tuple[YahooPlayer | None, float]:
        """Last resort: closest name above the similarity threshold, same position.

        A row with no position at all (a keeper CSV, which only has a name) searches every
        position rather than none -- filtering on an empty position matches no player, so
        the fallback would silently never fire.
        """
        position = normalize_position(row.position)
        target = normalize_name(row.name)
        best: YahooPlayer | None = None
        best_score = 0.0

        for player in self.players:
            if position and normalize_position(player.primary_position) != position:
                continue
            score = SequenceMatcher(None, target, normalize_name(player.full_name)).ratio()
            if score > best_score:
                best, best_score = player, score

        if best is not None and best_score >= self.FUZZY_THRESHOLD:
            return best, best_score
        return None, best_score

    def crosswalk(self, rows: list[SourceRow]) -> tuple[dict[str, list[SourceRow]], MatchReport]:
        """Group source rows by Yahoo player key, reporting anything that did not match."""
        grouped: dict[str, list[SourceRow]] = {}
        report = MatchReport()

        for row in rows:
            player = self.find(row)
            score = 1.0
            if player is None:
                player, score = self.find_fuzzy(row)
                if player is not None:
                    report.fuzzy.append((row.name, player.full_name, score))

            if player is None:
                report.unmatched.append(row)
                continue

            grouped.setdefault(player.player_key, []).append(row)
            report.matched[f"{row.source}:{row.name}"] = player.player_key

        return grouped, report


def _disambiguate(
    candidates: list[YahooPlayer], team: str, *, unique_only: bool = False
) -> YahooPlayer | None:
    """Pick among same-named players using team, else the first (Yahoo sorts by rank).

    ``unique_only`` is for matches made on an abbreviated name, where falling back to
    "the first one" would merge two different players rather than pick between two
    spellings of one. Such a match returns None instead, leaving the caller to try a
    stronger rule.
    """
    if len(candidates) == 1:
        return candidates[0]

    if team:
        on_team = [c for c in candidates if normalize_team(c.team_abbr) == team]
        if len(on_team) == 1:
            return on_team[0]
        if on_team and not unique_only:
            return on_team[0]

    return None if unique_only else candidates[0]
