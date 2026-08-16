"""Thin, synchronous Yahoo Fantasy API client.

Synchronous on purpose: the draft poller runs in its own thread, and sync code with a
plain retry loop is far easier to reason about at 11pm on draft night than an async task
graph. The whole surface is read-only -- this app never writes to your league.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ff_helper.config import API_BASE, Settings
from ff_helper.yahoo.auth import AuthError, Token, get_valid_token, refresh
from ff_helper.yahoo.models import DraftPick, League, Team, YahooPlayer
from ff_helper.yahoo.parse import content as strip_envelope
from ff_helper.yahoo.parse import (
    parse_draft_results,
    parse_league,
    parse_leagues,
    parse_players,
    parse_teams,
    unwrap,
)

# Yahoo caps the players collection at 25 per request regardless of what you ask for.
PLAYERS_PAGE_SIZE = 25

MAX_RETRIES = 4
BACKOFF_BASE = 0.5


class YahooAPIError(RuntimeError):
    pass


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
                # Token rejected despite looking fresh -- force one refresh, then retry.
                try:
                    self._token = refresh(self.settings, self._token)
                    self._token.save()
                except AuthError as exc:
                    raise YahooAPIError(str(exc)) from exc
                last_error = "401 unauthorized"
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
