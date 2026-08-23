"""Thin, synchronous Yahoo Fantasy API client.

Synchronous on purpose: the draft poller runs in its own thread, and sync code with a
plain retry loop is far easier to reason about at 11pm on draft night than an async task
graph. The whole surface is read-only -- this app never writes to your league.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from ff_helper.config import API_BASE, Settings
from ff_helper.yahoo.auth import AuthError, Token, get_valid_token, refresh
from ff_helper.yahoo.models import (
    DraftPick,
    KeptPlayer,
    League,
    Matchup,
    RosterEntry,
    Team,
    Transaction,
    YahooPlayer,
)
from ff_helper.yahoo.parse import content as strip_envelope
from ff_helper.yahoo.parse import (
    parse_draft_results,
    parse_league,
    parse_leagues,
    parse_player_stats,
    parse_players,
    parse_roster,
    parse_roster_entries,
    parse_scoreboard,
    parse_teams,
    parse_transactions,
    unwrap,
)

# Yahoo caps the players collection at 25 per request regardless of what you ask for.
PLAYERS_PAGE_SIZE = 25

MAX_RETRIES = 4
BACKOFF_BASE = 0.5


class YahooAPIError(RuntimeError):
    pass


# Yahoo answers "your app has no Fantasy Sports access" with a plain 401, the same status
# as an expired token, and the two need completely different responses from you. Kept as a
# constant so callers can recognize it without re-parsing prose.
NOT_APPROVED_PROBLEM = "additional_authorization_required"

NOT_APPROVED = (
    'Yahoo rejected the request: oauth_problem="additional_authorization_required".\n'
    "  This is not your token. The Yahoo *app* has no Fantasy Sports API access, so\n"
    "  signing in again will succeed and every call will still fail. Approval is\n"
    "  attached to the app, not the session.\n"
    "    - Apply or re-apply: https://sports.yahoo.com/developer/access/\n"
    "    - Check the app still exists and its Client ID matches YAHOO_CLIENT_ID:\n"
    "      https://developer.yahoo.com/apps/\n"
    "  Until it clears, offline mode is the way in -- see README.md."
)


def _oauth_problem(response: httpx.Response) -> str:
    """Yahoo's real complaint, out of the WWW-Authenticate header.

    Worth digging out rather than reporting a bare status code. Yahoo answers an
    unapproved app and an expired token with the same 401, and the one word that tells
    them apart lives only in this header -- discarding it turns a five-second diagnosis
    into an afternoon.
    """
    header = response.headers.get("www-authenticate", "")
    match = re.search(r'oauth_problem="([^"]+)"', header)
    return match.group(1) if match else ""


class YahooClient:
    def __init__(self, settings: Settings, token: Token | None = None) -> None:
        self.settings = settings
        self._token = token or get_valid_token(settings)
        self._http = httpx.Client(timeout=20.0)

    def __enter__(self) -> YahooClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- transport ---------------------------------------------------------------------

    def get(self, path: str) -> dict[str, Any]:
        """GET a Fantasy API path, refreshing the token and retrying as needed."""
        url = f"{API_BASE}/{path.lstrip('/')}"
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}format=json"

        last_error: str = ""
        for attempt in range(MAX_RETRIES):
            if self._token.expired:
                self._token = refresh(self.settings, self._token)
                self._token.save()

            try:
                response = self._http.get(
                    url, headers={"Authorization": f"Bearer {self._token.access_token}"}
                )
            except httpx.RequestError as exc:
                # Transient network trouble mid-draft is expected; back off and retry.
                last_error = f"network error: {exc}"
                time.sleep(BACKOFF_BASE * (2**attempt))
                continue

            if response.status_code == 200:
                return response.json()

            if response.status_code == 401:
                problem = _oauth_problem(response)
                if problem == "additional_authorization_required":
                    # Not a token problem, and no number of retries will change it: Yahoo
                    # is saying this *app* has no Fantasy Sports API access. Signing in
                    # again succeeds and then every call fails exactly like this, which is
                    # why it has to be named rather than reported as "401 unauthorized"
                    # four times over.
                    raise YahooAPIError(NOT_APPROVED)

                # Token rejected despite looking fresh -- force one refresh, then retry.
                try:
                    self._token = refresh(self.settings, self._token)
                    self._token.save()
                except AuthError as exc:
                    raise YahooAPIError(str(exc)) from exc
                last_error = f"401 unauthorized{f' ({problem})' if problem else ''}"
                continue

            if response.status_code in (429, 500, 502, 503, 504):
                last_error = f"{response.status_code} from Yahoo"
                time.sleep(BACKOFF_BASE * (2**attempt))
                continue

            raise YahooAPIError(
                f"Yahoo returned {response.status_code} for {path}: {response.text[:400]}"
            )

        raise YahooAPIError(f"Giving up on {path} after {MAX_RETRIES} attempts ({last_error})")

    # -- resources ---------------------------------------------------------------------

    def my_leagues(self, game_key: str = "nfl") -> list[League]:
        """Every NFL league the signed-in user belongs to this season."""
        payload = self.get(f"users;use_login=1/games;game_keys={game_key}/leagues")
        return parse_leagues(payload)

    def league(self, league_key: str) -> League:
        """League metadata plus settings (scoring and roster slots) in one call."""
        payload = self.get(f"league/{league_key};out=settings")
        node = unwrap(strip_envelope(payload), "league")
        return parse_league(node)

    def teams(self, league_key: str) -> list[Team]:
        return parse_teams(self.get(f"league/{league_key}/teams"))

    def roster(self, team_key: str) -> list[KeptPlayer]:
        """Players on a team's roster right now."""
        return parse_roster(self.get(f"team/{team_key}/roster"), team_key)

    def keepers(self, teams: list[Team]) -> tuple[list[KeptPlayer], list[str]]:
        """Every player rostered before the draft -- that is, every keeper.

        One request per team, run once at startup rather than during the draft. A team
        whose roster fails to load is skipped rather than aborting the whole load, but the
        failure is *returned* alongside the keepers rather than swallowed: a team silently
        missing from the list leaves its keepers in the pool all draft, which is the exact
        quiet failure ``draft.keepers`` exists to prevent. The caller surfaces the names.

        The catch is deliberately broad. ``get`` can also raise ``AuthError`` (a sibling of
        ``YahooAPIError``, not a subclass) and ``JSONDecodeError``, and either escaping
        here would abort startup over one unreachable team.
        """
        kept: list[KeptPlayer] = []
        failed: list[str] = []
        for team in teams:
            try:
                kept.extend(self.roster(team.team_key))
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                failed.append(f"{team.name or team.team_key} ({exc})")
        return kept, failed

    def draft_results(self, league_key: str) -> list[DraftPick]:
        """Every pick made so far. This is what the live poller hits."""
        return parse_draft_results(self.get(f"league/{league_key}/draftresults"))

    def draft_status(self, league_key: str) -> str:
        """Cheap check for predraft / drafting / postdraft."""
        payload = self.get(f"league/{league_key}")
        node = unwrap(strip_envelope(payload), "league")
        return parse_league(node).draft_status

    def players(
        self,
        league_key: str,
        *,
        limit: int = 600,
        sort: str = "AR",
        with_draft_analysis: bool = True,
    ) -> list[YahooPlayer]:
        """Walk the league's player pool, newest ADP included.

        ``limit`` is generous by default: a 12-team league drafts ~180 players, but ADP
        for the next hundred matters for survival estimates late in the draft.
        """
        subresource = ";out=draft_analysis" if with_draft_analysis else ""
        collected: list[YahooPlayer] = []
        seen: set[str] = set()

        for start in range(0, limit, PLAYERS_PAGE_SIZE):
            path = (
                f"league/{league_key}/players;"
                f"sort={sort};start={start};count={PLAYERS_PAGE_SIZE}{subresource}"
            )
            page = parse_players(self.get(path))
            if not page:
                break  # Yahoo returns an empty collection past the end of the pool.
            for player in page:
                if player.player_key not in seen:
                    seen.add(player.player_key)
                    collected.append(player)

        return collected

    # -- in-season resources -----------------------------------------------------------
    #
    # Still GET only. There is no `post()` or `put()` on this client and adding one is a
    # deliberate decision, not an implementation detail: everything below reads the league
    # so the app can advise, and setting a lineup or filing a claim stays something you do
    # yourself in Yahoo. See the module docstring.

    def scoreboard(self, league_key: str, week: int) -> list[Matchup]:
        """One week's head-to-head matchups, one entry per team."""
        return parse_scoreboard(self.get(f"league/{league_key}/scoreboard;week={week}"))

    def team_roster(self, team_key: str, week: int) -> list[RosterEntry]:
        """A team's roster for a given week, with the slot each player is in.

        The weekly sibling of ``roster()``. Ask for the week explicitly -- an undated
        roster request returns whatever Yahoo considers current, which is not the same
        question once the week rolls over on Tuesday morning.
        """
        payload = self.get(f"team/{team_key}/roster;week={week}")
        return parse_roster_entries(payload, team_key, week)

    def rosters(self, teams: list[Team], week: int) -> tuple[list[RosterEntry], list[str]]:
        """Every team's weekly roster, with per-team failures reported rather than raised.

        Same contract as ``keepers()`` and for the same reason: one unreachable team must
        not abort the whole fetch, but a team silently missing would leave its players
        looking like free agents -- so failures come back alongside the entries.
        """
        entries: list[RosterEntry] = []
        failed: list[str] = []
        for team in teams:
            try:
                entries.extend(self.team_roster(team.team_key, week))
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                failed.append(f"{team.name or team.team_key} ({exc})")
        return entries, failed

    def free_agents(
        self,
        league_key: str,
        *,
        limit: int = 300,
        status: str = "A",
        sort: str = "AR",
    ) -> list[YahooPlayer]:
        """Players available right now. ``status='A'`` is free agents plus waivers.

        ``out=percent_owned`` rides along because ownership trend is the earliest waiver
        signal there is, and asking for it separately would double the request count on
        the one fetch that already pages the hardest.
        """
        collected: list[YahooPlayer] = []
        seen: set[str] = set()

        for start in range(0, limit, PLAYERS_PAGE_SIZE):
            path = (
                f"league/{league_key}/players;status={status};"
                f"sort={sort};start={start};count={PLAYERS_PAGE_SIZE};out=percent_owned"
            )
            page = parse_players(self.get(path))
            if not page:
                break
            for player in page:
                if player.player_key not in seen:
                    seen.add(player.player_key)
                    collected.append(player)

        return collected

    def transactions(
        self, league_key: str, *, types: tuple[str, ...] = ("add", "drop")
    ) -> list[Transaction]:
        """Completed league transactions, newest first as Yahoo returns them."""
        filter_ = f";type={','.join(types)}" if types else ""
        return parse_transactions(self.get(f"league/{league_key}/transactions{filter_}"))

    def weekly_stats(
        self, league_key: str, player_keys: list[str], week: int
    ) -> dict[str, dict[str, float]]:
        """Realized stat lines for a week, in the projection column vocabulary.

        This is what turns a stored ``WeekSnapshot`` into a backtest: week N's projections
        sit next to week N's actuals, and every weekly model becomes falsifiable. Fetch it
        *after* the week completes.
        """
        results: dict[str, dict[str, float]] = {}
        for start in range(0, len(player_keys), PLAYERS_PAGE_SIZE):
            chunk = player_keys[start : start + PLAYERS_PAGE_SIZE]
            if not chunk:
                break
            path = (
                f"league/{league_key}/players;player_keys={','.join(chunk)}"
                f"/stats;type=week;week={week}"
            )
            results.update(parse_player_stats(self.get(path)))
        return results
