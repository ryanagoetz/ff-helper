"""Read completed sales out of a Yahoo draft room and resolve them to keys.

Yahoo's Fantasy API is behind an approval process that has not cleared, so the app cannot
poll the draft. But the browser sitting in front of the draft room is already an
authenticated Yahoo client, and what it displays is the same information the API would
return. This module turns what that page shows -- names and dollar amounts -- into
something the board can accept.

**Resolution happens here, before anything reaches the board, and it can refuse.** Two
failure modes are not symmetrical:

* An **unresolvable buyer** must never be written. Money charged to no team never leaves
  the room, so ``league_money_remaining`` stays high, the inflation model reads the league
  as cash-rich, and every max bid in the app is overstated. A sale we cannot attribute is
  worse than a sale we never saw.
* An **unresolvable player** is merely a sale the board does not know about. Bad, but it
  degrades toward "stale" rather than toward "confidently wrong".

Both are reported rather than swallowed. The paste path is used by a human who is sitting
there and can act on an error immediately, so it refuses loudly instead of guessing.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field

from ff_helper.rankings.players import PlayerRegistry, SourceRow
from ff_helper.yahoo.models import Team

# "$34", "34", "$1,234" -- and a bare "-" or "--" for a slot with no price.
_MONEY = re.compile(r"^\$?(\d{1,4})$")

# Fallback shape for a line that is not delimited: "Ja'Marr Chase $55 Team Name"
_LINE = re.compile(r"^(?P<name>.+?)\s+\$(?P<cost>\d{1,4})\s+(?P<buyer>.+?)$")

_POSITIONS = {"QB", "RB", "WR", "TE", "K", "PK", "DEF", "DST", "D/ST"}

# An NFL team abbreviation: two to four letters, all caps, and not a position.
_TEAM_ABBR = re.compile(r"^[A-Z]{2,4}$")


@dataclass(frozen=True)
class RawSale:
    """One sale as the page showed it, before any resolution."""

    name: str
    cost: int | None = None
    buyer: str = ""
    team_abbr: str = ""
    position: str = ""
    line: int = 0


@dataclass
class ResolutionReport:
    """What resolution managed, and precisely what it could not."""

    resolved: list[tuple[RawSale, str, str]] = field(default_factory=list)
    unknown_players: list[RawSale] = field(default_factory=list)
    unknown_buyers: list[RawSale] = field(default_factory=list)
    missing_price: list[RawSale] = field(default_factory=list)
    fuzzy: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.unknown_players or self.unknown_buyers or self.missing_price)


class BridgeResolver:
    """Names to keys, memoized.

    Memoization is not premature: ``PlayerRegistry.find_fuzzy`` runs a SequenceMatcher
    over the whole pool, and the reader re-sends every sale it can see on every reading.
    Without a cache an unmatchable name costs a full scan of several hundred players every
    few seconds for the length of a draft. Negative results are cached too, for exactly
    that reason.
    """

    def __init__(
        self,
        registry: PlayerRegistry,
        teams: list[Team],
        *,
        team_aliases: dict[str, str] | None = None,
    ) -> None:
        self.registry = registry
        self.teams = teams
        self._players: dict[tuple[str, str], str | None] = {}
        self._by_team_name: dict[str, str] = {}
        for team in teams:
            self._by_team_name[_fold(team.name)] = team.team_key
            self._by_team_name[team.team_key] = team.team_key
        for alias, target in (team_aliases or {}).items():
            resolved = self._by_team_name.get(_fold(target))
            if resolved:
                self._by_team_name[_fold(alias)] = resolved

    def resolve_player(self, sale: RawSale) -> tuple[str | None, bool]:
        """Returns ``(player_key, was_fuzzy)``."""
        cache_key = (_fold(sale.name), sale.team_abbr.upper())
        if cache_key in self._players:
            key = self._players[cache_key]
            return key, False

        row = SourceRow(
            name=sale.name, position=sale.position, team=sale.team_abbr, source="bridge"
        )
        player = self.registry.find(row)
        fuzzy = False
        if player is None:
            player, _score = self.registry.find_fuzzy(row)
            fuzzy = player is not None

        key = player.player_key if player else None
        self._players[cache_key] = key
        return key, fuzzy

    def resolve_team(self, buyer: str) -> str | None:
        return self._by_team_name.get(_fold(buyer))

    def resolve_all(self, sales: list[RawSale], *, is_auction: bool) -> ResolutionReport:
        report = ResolutionReport()
        for sale in sales:
            player_key, fuzzy = self.resolve_player(sale)
            if player_key is None:
                report.unknown_players.append(sale)
                continue
            if fuzzy:
                report.fuzzy.append((sale.name, player_key))

            team_key = ""
            if is_auction:
                if sale.cost is None:
                    report.missing_price.append(sale)
                    continue
                team_key = self.resolve_team(sale.buyer) or ""
                if not team_key:
                    report.unknown_buyers.append(sale)
                    continue

            report.resolved.append((sale, player_key, team_key))
        return report


def _fold(value: str) -> str:
    """Normalize a team label for comparison: case, whitespace, and stray punctuation.

    Draft-room labels pick up decoration the league settings page does not show -- a
    trailing ellipsis where the name was truncated, a champion's trophy glyph, doubled
    spaces. Folding those away turns a class of silent mismatches into matches, and a
    mismatched *buyer* is the expensive kind.
    """
    cleaned = (value or "").strip().lower()
    cleaned = cleaned.replace("…", "...").replace(" ", " ")
    # Drop symbols and format characters -- trophies, medals, variation selectors --
    # while keeping punctuation real team names use: "B-U-T(t)-S", "3 QB's".
    cleaned = "".join(
        character
        for character in cleaned
        if unicodedata.category(character) not in {"So", "Sk", "Cf", "Co"}
    )
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def parse_paste(text: str) -> list[RawSale]:
    """Read sales out of text copied from a draft room.

    Deliberately tolerant about shape, because what a select-all-copy produces is not
    known until we have seen a live draft room, and a parser that only accepts one layout
    would be useless the moment Yahoo's differs. Two shapes are understood:

    * delimited (tab or comma), with the money column found by looking rather than by
      position, since column order varies;
    * a plain line such as ``Ja'Marr Chase $55 Team Name``.

    Rows that parse to nothing are skipped rather than guessed at; the caller reports the
    count so a paste that mostly failed cannot look like a paste that mostly worked.
    """
    sales: list[RawSale] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue

        fields = _split(line)
        if len(fields) >= 2:
            sale = _from_fields(fields, number)
            if sale is not None:
                sales.append(sale)
                continue

        match = _LINE.match(line)
        if match:
            sales.append(
                RawSale(
                    name=match.group("name").strip(),
                    cost=int(match.group("cost")),
                    buyer=match.group("buyer").strip(),
                    line=number,
                )
            )
    return sales


def _split(line: str) -> list[str]:
    if "\t" in line:
        return [part.strip() for part in line.split("\t")]
    if "," in line:
        row = next(csv.reader(io.StringIO(line)), [])
        return [part.strip() for part in row]
    return []


def _from_fields(fields: list[str], number: int) -> RawSale | None:
    """Interpret delimited fields, locating the price by shape rather than position."""
    money_at = None
    for index, value in enumerate(fields):
        if index == 0:
            continue  # a player named "50" is not a thing; the first column is the name.
        if _MONEY.match(value.replace(",", "")):
            money_at = index
            break

    name = fields[0].strip()
    if not name:
        return None

    rest = [f for index, f in enumerate(fields[1:], start=1) if index != money_at and f]

    # Pull out position and NFL team where the row carries them. Not decoration: a team
    # defense is identified by its team rather than its name, since every source spells it
    # differently ("Seattle Defense" / "Seattle Seahawks"), so without the abbreviation a
    # defense may not resolve at all.
    position = ""
    team_abbr = ""
    remaining: list[str] = []
    for value in rest:
        token = value.strip().upper()
        if not position and token in _POSITIONS:
            position = token
        elif not team_abbr and _TEAM_ABBR.match(token):
            team_abbr = token
        else:
            remaining.append(value)

    cost = int(fields[money_at].replace("$", "").replace(",", "")) if money_at else None
    return RawSale(
        name=name,
        cost=cost,
        buyer=remaining[-1] if remaining else "",
        position=position,
        team_abbr=team_abbr,
        line=number,
    )
