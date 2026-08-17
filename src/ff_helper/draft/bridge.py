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

from ff_helper.engine.auction import MIN_BID
from ff_helper.rankings.players import PlayerRegistry, SourceRow
from ff_helper.yahoo.models import Team

# "$34", "34", "$1,234" -- and a bare "-" or "--" for a slot with no price.
_MONEY = re.compile(r"^\$?(\d{1,4})$")

# Fallback shape for a line that is not delimited: "Ja'Marr Chase $55 Team Name"
_LINE = re.compile(r"^(?P<name>.+?)\s+\$(?P<cost>\d{1,4})\s+(?P<buyer>.+?)$")

_POSITIONS = {"QB", "RB", "WR", "TE", "K", "PK", "DEF", "DST", "D/ST"}

# An NFL team abbreviation: two to four letters, all caps, and not a position.
_TEAM_ABBR = re.compile(r"^[A-Z]{2,4}$")

# Yahoo prints an injury designation on its own line, between the name and the position.
_INJURY_FLAGS = {"Q", "O", "D", "IR", "PUP", "SUSP", "NA"}


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
    # Matches settled by price because the name alone fit more than one player. Surfaced
    # so a wrong guess is visible rather than silently on the board.
    assumed: list[tuple[str, str]] = field(default_factory=list)
    # Buyers who matched no team and were given a free slot. Their money leaves the room
    # correctly; only that rival's own budget is a guess.
    assigned_buyers: list[tuple[str, str]] = field(default_factory=list)


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
        values: dict[str, float] | None = None,
    ) -> None:
        self.registry = registry
        self.teams = teams
        self.values = values or {}
        self._players: dict[tuple[str, str], tuple[str | None, str]] = {}
        # Buyer names with no matching team, given a free slot so their money still
        # leaves the room. Remembered, so the same name keeps the same slot.
        self._assigned: dict[str, str] = {}
        self._by_team_name: dict[str, str] = {}
        for team in teams:
            self._by_team_name[_fold(team.name)] = team.team_key
            self._by_team_name[team.team_key] = team.team_key
            # Yahoo labels the reader's own team "Your Team" rather than by name, so
            # without this every one of your own purchases fails to resolve -- and your
            # budget is the number the whole app exists to produce.
            if team.is_mine:
                for label in YOUR_TEAM_LABELS:
                    self._by_team_name[label] = team.team_key
        for alias, target in (team_aliases or {}).items():
            resolved = self._by_team_name.get(_fold(target))
            if resolved:
                self._by_team_name[_fold(alias)] = resolved

    def resolve_player(self, sale: RawSale) -> tuple[str | None, str]:
        """Returns ``(player_key, how)`` where *how* is exact, fuzzy, priced, or "".

        Yahoo's draft room abbreviates names to a first initial -- "B. Robinson" -- and an
        initial is not an identity. ``find`` refuses when one fits two players, which is
        right when nothing can separate them, but the sale price often can: Bijan Robinson
        is worth about $90 to this league and Brian Robinson about $1, so a $71 sale is
        not in genuine doubt. Where price settles it, that is reported as an assumption
        rather than passed off as a match.
        """
        # Team and position are the identity here; the price is evidence, not identity.
        # Keying on cost too meant a live nomination -- whose bid ticks upward in the page
        # text the readers scrape -- minted a fresh entry per increment, so the negative
        # caching this class exists for never fired on the one row that keeps changing.
        cache_key = (_fold(sale.name), sale.team_abbr.upper())
        if cache_key in self._players:
            # Replay the original provenance. Returning "cached" made resolve_all stop
            # recording fuzzy and priced matches after the first reading, which silently
            # retired the warning that a guess had been made.
            return self._players[cache_key]

        row = SourceRow(
            name=sale.name, position=sale.position, team=sale.team_abbr, source="bridge"
        )
        player = self.registry.find(row)
        how = "exact" if player else ""

        if player is None:
            options = self.registry.candidates(row)
            if len(options) == 1:
                player, how = options[0], "exact"
            elif len(options) > 1 and sale.cost is not None and self.values:
                # Default to MIN_BID, matching DollarValues.value_of. Defaulting to 0.0
                # made an unprojected candidate the nearest match for every cheap sale,
                # charging the money to him and leaving the real player on the board.
                player = min(
                    options,
                    key=lambda candidate: abs(
                        self.values.get(candidate.player_key, float(MIN_BID))
                        - float(sale.cost)
                    ),
                )
                how = "priced"

        if player is None:
            player, _score = self.registry.find_fuzzy(row)
            how = "fuzzy" if player else ""

        key = player.player_key if player else None
        if how != "priced":
            # A priced match is an answer about this sale, not about this name: the same
            # abbreviation at a different price is a different player. Caching it would
            # replay one guess over every later sale of that name.
            self._players[cache_key] = (key, how)
        return key, how

    def resolve_team(self, buyer: str) -> tuple[str | None, str]:
        """Returns ``(team_key, how)`` where *how* is exact, assigned, or "".

        A name we have never seen gets given a free team slot rather than blocking the
        sale, because being sure *which* rival bought a player matters far less than it
        looks. ``league_money_remaining`` sums what is left across the twelve slots, so as
        long as a sale lands on some real slot the money leaves the room and inflation
        stays honest -- and inflation is what prices everything. Getting the wrong rival
        only misstates that rival's own budget, which is a display detail.

        Refusing instead would mean a team that renamed itself mid-draft, or joined late,
        silently stopped counting against the pool -- which overstates the money chasing
        the remaining players and inflates every price the board quotes. Adapting is the
        safer failure.

        Your own team is never assigned this way. It is pinned by name, by alias, and by
        Yahoo's own "Your Team" label, because your budget and max bid are the numbers the
        app exists to produce and a wrong slot there is not a display detail.
        """
        folded = _fold(buyer)
        known = self._by_team_name.get(folded)
        if known:
            return known, "exact"
        if not folded:
            return None, ""

        assigned = self._assigned.get(folded)
        if assigned:
            return assigned, "assigned"

        taken = set(self._assigned.values())
        for team in self.teams:
            if team.is_mine or team.team_key in taken:
                continue
            self._assigned[folded] = team.team_key
            return team.team_key, "assigned"

        # Every slot is spoken for. Almost always this means the league is configured with
        # fewer teams than the room actually has, so say that rather than leaving the
        # caller to report a mysterious unresolvable buyer.
        return None, "no free slot"

    def resolve_all(self, sales: list[RawSale], *, is_auction: bool) -> ResolutionReport:
        report = ResolutionReport()
        for sale in sales:
            player_key, how = self.resolve_player(sale)
            if player_key is None:
                report.unknown_players.append(sale)
                continue
            if how == "fuzzy":
                report.fuzzy.append((sale.name, player_key))
            elif how == "priced":
                report.assumed.append((sale.name, player_key))

            team_key = ""
            if is_auction:
                if sale.cost is None:
                    report.missing_price.append(sale)
                    continue
                team_key, placed = self.resolve_team(sale.buyer)
                if not team_key:
                    report.unknown_buyers.append(sale)
                    continue
                if placed == "assigned":
                    report.assigned_buyers.append((sale.buyer, team_key))

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
    # A trailing run of dots is truncation, not part of the name.
    return cleaned.strip().rstrip(".").strip()


# Yahoo's draft-results panel copies out one record per sale, across several lines:
#
#     3⇥                <- pick number, trailing tab
#     B. Robinson       <- name
#     B. Robinson       <- the same name again
#     Q                 <- injury designation, only sometimes
#     RB                <- position
#     Atl               <- NFL team
#     Bye 11
#     Your Team         <- the buyer
#     $71               <- price
#
# Records run newest-first with "Round N" headings between them. Everything is read from
# the end of the record backwards, because the only genuinely optional parts -- the
# repeated name and the injury flag -- are at the front.
_PICK_LINE = re.compile(r"^(\d{1,3})\s*$")

# The results table announces itself with a header row. Everything above it is live draft
# furniture -- the nomination counter, the current bid, the budget table -- and a bare
# number up there ("8 nominations until your turn") otherwise starts a record that then
# swallows the current bid as its price and invents a sale.
_RESULTS_HEADER = re.compile(r"^pick\b.*\bplayer\b.*\bcost\b", re.IGNORECASE)
_BYE_LINE = re.compile(r"^bye\b", re.IGNORECASE)
_ROUND_LINE = re.compile(r"^round\s+\d+\s*$", re.IGNORECASE)
_PRICE_LINE = re.compile(r"^\$\s*(\d{1,4})$")

# A sale record is name, name, [flag], position, team, bye, buyer, price. A generous
# ceiling on that, so a stray number on the page cannot swallow half the document.
_MAX_RECORD_LINES = 12

# How Yahoo labels the reader's own team rather than naming it. The draft-results panel
# and the budget panel do not agree with each other, so both spellings are needed.
YOUR_TEAM = "your team"
YOUR_TEAM_LABELS = {"your team", "you", "my team"}


def parse_yahoo_results(text: str) -> list[RawSale]:
    """Read Yahoo's draft-results panel as copied from the browser."""
    lines = [line.strip() for line in text.splitlines()]

    # Start at the results header when there is one, so page chrome above it cannot be
    # read as sales. Without a header -- a hand-tidied paste, say -- read the lot.
    first = next(
        (index + 1 for index, line in enumerate(lines) if _RESULTS_HEADER.match(line)), 0
    )
    lines = lines[first:]

    starts = [
        index
        for index, line in enumerate(lines)
        if _PICK_LINE.match(line) and not _ROUND_LINE.match(line)
    ]
    if not starts:
        return []

    sales: list[RawSale] = []
    for position, start in enumerate(starts):
        limit = starts[position + 1] if position + 1 < len(starts) else len(lines)
        # A record ends at its price, not at the next pick number. Reading to the next
        # pick number lets whatever follows the last sale -- chat, nav, a watch list --
        # be absorbed into it, which cost the final sale its price. It also means a bare
        # number loose on the page starts a record that never closes: requiring a price
        # inside a short window discards those instead of inventing a sale from chatter.
        block: list[str] = []
        for line in lines[start + 1 : min(limit, start + 1 + _MAX_RECORD_LINES)]:
            if not line or _ROUND_LINE.match(line):
                continue
            block.append(line)
            if _PRICE_LINE.match(line):
                break
        else:
            continue  # no price found: not a sale.

        if not _PRICE_LINE.match(block[-1]):
            continue

        sale = _from_yahoo_block(int(lines[start]), block)
        if sale is not None:
            sales.append(sale)
    return sales


def _from_yahoo_block(pick: int, block: list[str]) -> RawSale | None:
    if len(block) < 4:
        return None

    price = _PRICE_LINE.match(block[-1])
    cost = int(price.group(1)) if price else None
    rest = block[:-1] if price else list(block)
    if not rest:
        return None

    buyer = rest.pop()
    if rest and _BYE_LINE.match(rest[-1]):
        rest.pop()

    team_abbr = ""
    if rest and _TEAM_ABBR.match(rest[-1].upper()) and rest[-1].upper() not in _POSITIONS:
        team_abbr = rest.pop().upper()

    position = ""
    if rest and rest[-1].upper() in _POSITIONS:
        position = rest.pop().upper()

    # Whatever is left is the name, repeated and possibly preceded by an injury flag.
    names = [value for value in rest if value.upper() not in _INJURY_FLAGS]
    if not names:
        return None

    return RawSale(
        name=names[0], cost=cost, buyer=buyer, team_abbr=team_abbr, position=position, line=pick
    )


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
    yahoo = parse_yahoo_results(text)
    if yahoo:
        return yahoo

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
    # The buyer is the last field, and is never a position or an NFL team however it is
    # spelled. Without holding it back, a short team name -- "TNT", "Bums", "Kev" -- was
    # eaten as an abbreviation and the sale arrived with a blank buyer, which then failed
    # to resolve and refused the whole paste.
    buyer = rest[-1] if rest else ""
    for value in rest[:-1]:
        token = value.strip().upper()
        if not position and token in _POSITIONS:
            position = token
        elif not team_abbr and _TEAM_ABBR.match(token):
            team_abbr = token
        else:
            remaining.append(value)

    cost = int(fields[money_at].replace("$", "").replace(",", "")) if money_at is not None else None
    return RawSale(
        name=name,
        cost=cost,
        buyer=buyer,
        position=position,
        team_abbr=team_abbr,
        line=number,
    )
