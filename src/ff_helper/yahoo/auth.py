"""Yahoo OAuth2 authorization-code flow with on-disk token caching.

Yahoo access tokens expire after an hour, which is shorter than some drafts. The refresh
token does not, so we cache both and refresh transparently -- a draft must never stop
because a token aged out mid-round.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlencode

import httpx

from ff_helper.config import AUTH_URL, TOKEN_URL, Settings, token_path

# Refresh this many seconds before actual expiry, so a request never races the deadline.
REFRESH_MARGIN = 120.0


class AuthError(RuntimeError):
    """Raised when authentication cannot proceed without human action."""


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - REFRESH_MARGIN

    @classmethod
    def from_response(cls, payload: dict) -> Token:
        return cls(
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            expires_at=time.time() + float(payload.get("expires_in", 3600)),
        )

    def save(self) -> None:
        path = token_path()
        path.write_text(json.dumps(asdict(self), indent=2))
        path.chmod(0o600)  # contains a long-lived credential

    @classmethod
    def load(cls) -> Token | None:
        path = token_path()
        if not path.exists():
            return None
        try:
            return cls(**json.loads(path.read_text()))
        except (json.JSONDecodeError, TypeError, KeyError):
            # A corrupt cache should send the user back through login, not crash.
            return None


def _basic_auth_header(settings: Settings) -> str:
    raw = f"{settings.client_id}:{settings.client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def authorization_url(settings: Settings, state: str) -> str:
    """The URL the user opens in a browser to approve access.

    No ``scope`` parameter is sent by default, and that is deliberate. Fantasy access is
    granted to the *app* -- via Yahoo's approval process at
    https://sports.yahoo.com/developer/access/ -- not requested per sign-in. Asking for
    ``fspt-r`` from an app that has not been approved is rejected outright with
    ``error=invalid_scope`` before the user can even approve, which is a worse failure than
    signing in successfully and discovering the problem on the first API call. Once the app
    is approved, the permission rides on the token without being asked for.

    ``FF_OAUTH_SCOPE`` forces a scope anyway, for the day Yahoo decides it wants one.
    """
    params = {
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if settings.oauth_scope:
        params["scope"] = settings.oauth_scope
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(settings: Settings, code: str) -> Token:
    """Trade an authorization code for an access/refresh token pair."""
    response = httpx.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(settings),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "redirect_uri": settings.redirect_uri,
            "code": code,
        },
        timeout=30.0,
    )
    if response.status_code != 200:
        raise AuthError(
            f"Yahoo rejected the authorization code ({response.status_code}): {response.text}\n"
            "The most common causes are a redirect URI that does not exactly match the one "
            "registered on your Yahoo app, or a code that was already used once."
        )
    return Token.from_response(response.json())


def refresh(settings: Settings, token: Token) -> Token:
    """Exchange a refresh token for a fresh access token."""
    response = httpx.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(settings),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "redirect_uri": settings.redirect_uri,
            "refresh_token": token.refresh_token,
        },
        timeout=30.0,
    )
    if response.status_code != 200:
        raise AuthError(
            f"Token refresh failed ({response.status_code}): {response.text}\n"
            "Re-run `python scripts/setup_auth.py` to sign in again."
        )
    payload = response.json()
    # Yahoo usually returns a fresh refresh_token, but tolerate it being absent.
    payload.setdefault("refresh_token", token.refresh_token)
    return Token.from_response(payload)


def get_valid_token(settings: Settings) -> Token:
    """Load the cached token, refreshing it if needed. Never triggers interactive login."""
    token = Token.load()
    if token is None:
        raise AuthError("Not signed in to Yahoo yet. Run:\n    python scripts/setup_auth.py")
    if token.expired:
        token = refresh(settings, token)
        token.save()
    return token
