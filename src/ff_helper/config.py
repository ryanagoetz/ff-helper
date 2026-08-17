"""Configuration and on-disk paths.

Everything mutable at runtime (OAuth token, ranking cache) lives under ``~/.ff-helper``
so that a git clone stays clean and secrets never land in the repo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Yahoo's OAuth2 endpoints and the Fantasy Sports API root.
AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

# Yahoo's out-of-band redirect value, used when a localhost callback is rejected.
OOB_REDIRECT = "oob"

# Yahoo grants fantasy access to an approved app, not per sign-in request, so no scope is
# sent by default -- asking for one an unapproved app lacks fails with invalid_scope.
DEFAULT_OAUTH_SCOPE = ""


def state_dir() -> Path:
    """Directory for tokens and caches. Override with FF_HELPER_HOME (used by tests)."""
    root = os.environ.get("FF_HELPER_HOME")
    path = Path(root) if root else Path.home() / ".ff-helper"
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_path() -> Path:
    return state_dir() / "token.json"


def cache_dir() -> Path:
    path = state_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str
    league_key: str | None
    poll_interval: float
    # Overrides the auction budget when Yahoo does not publish one for your league.
    auction_budget: int | None = None
    # Empty means send no scope at all, which is what an approved app wants. See
    # authorization_url for why asking is worse than not asking.
    oauth_scope: str = DEFAULT_OAUTH_SCOPE

    @property
    def uses_oob(self) -> bool:
        """True when we must fall back to manual code paste instead of a local callback."""
        return self.redirect_uri.strip().lower() in {OOB_REDIRECT, "urn:ietf:wg:oauth:2.0:oob"}


def load_settings(*, require_credentials: bool = True) -> Settings:
    """Read settings from .env / environment.

    Raises a message aimed at a human running this for the first time, not a stack trace.
    """
    load_dotenv()
    client_id = os.environ.get("YAHOO_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YAHOO_CLIENT_SECRET", "").strip()

    if require_credentials and not (client_id and client_secret):
        raise SystemExit(
            "Missing Yahoo credentials.\n"
            "  1. Apply for Fantasy Sports API access at\n"
            "     https://sports.yahoo.com/developer/access/ -- it is an approval\n"
            "     process, and nothing works until it clears\n"
            "  2. Register an app at https://developer.yahoo.com/apps/create/\n"
            "     (Confidential Client; leave API Permissions unticked -- Fantasy\n"
            "      Sports is not offered there any more)\n"
            "  3. Put YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET in a .env file\n"
            "See README.md for the full walkthrough."
        )

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=os.environ.get(
            "YAHOO_REDIRECT_URI", "https://localhost:8000/callback"
        ).strip(),
        league_key=(os.environ.get("FF_LEAGUE_KEY") or "").strip() or None,
        poll_interval=float(os.environ.get("FF_POLL_INTERVAL", "2.0")),
        auction_budget=_optional_int(os.environ.get("FF_AUCTION_BUDGET")),
        oauth_scope=(os.environ.get("FF_OAUTH_SCOPE") or "").strip() or DEFAULT_OAUTH_SCOPE,
    )


def _optional_int(raw: str | None) -> int | None:
    if not raw or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value > 0 else None
