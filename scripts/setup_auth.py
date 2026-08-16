#!/usr/bin/env python3
"""One-time Yahoo sign-in.

Run this once on your own machine. It caches a refresh token to ~/.ff-helper/token.json,
after which the app renews access tokens on its own and you never sign in again.

    python scripts/setup_auth.py

The flow deliberately avoids running a local HTTPS callback server. Yahoo requires an
HTTPS redirect URI, which would mean generating and trusting a self-signed certificate --
a step with far more failure modes than it is worth for something you do once. Instead you
paste the URL your browser lands on. That works identically whether your app is registered
with an "oob" redirect or an "https://localhost/..." one, so you are covered either way.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ff_helper.config import load_settings  # noqa: E402
from ff_helper.yahoo.auth import (  # noqa: E402
    AuthError,
    authorization_url,
    exchange_code,
)
from ff_helper.yahoo.client import YahooClient  # noqa: E402


def extract_code(raw: str) -> str:
    """Accept either a bare code or the full URL the browser was redirected to."""
    raw = raw.strip()
    if not raw:
        raise ValueError("Nothing pasted.")
    if raw.startswith(("http://", "https://")):
        params = parse_qs(urlparse(raw).query)
        if "error" in params:
            raise ValueError(f"Yahoo returned an error: {params['error'][0]}")
        codes = params.get("code")
        if not codes:
            raise ValueError("That URL has no ?code= parameter in it.")
        return codes[0]
    return raw


def main() -> int:
    settings = load_settings()
    state = secrets.token_urlsafe(16)

    print("\n1. Open this URL in your browser and approve access:\n")
    print(f"   {authorization_url(settings, state)}\n")
    if settings.uses_oob:
        print("2. Yahoo will show you a short code. Copy it.\n")
    else:
        print(
            "2. Your browser will be redirected to a page that probably fails to load --\n"
            "   that is expected and fine. Copy the ENTIRE URL from the address bar;\n"
            "   the authorization code is in it.\n"
        )

    try:
        pasted = input("Paste it here: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1

    try:
        code = extract_code(pasted)
    except ValueError as exc:
        print(f"\nCould not read an authorization code: {exc}")
        return 1

    try:
        token = exchange_code(settings, code)
    except AuthError as exc:
        print(f"\n{exc}")
        return 1

    token.save()
    print("\nSigned in. Token cached to ~/.ff-helper/token.json\n")

    # Immediately prove the token works and show the user their league keys, since
    # FF_LEAGUE_KEY is the next thing they need and it is otherwise annoying to find.
    try:
        with YahooClient(settings) as client:
            leagues = client.my_leagues()
    except Exception as exc:  # noqa: BLE001 - surfacing any failure is the point here
        print(f"Signed in, but listing your leagues failed: {exc}")
        print("The token is saved; you can set FF_LEAGUE_KEY manually.")
        return 0

    if not leagues:
        print("No NFL fantasy leagues found on this account for the current season.")
        return 0

    print("Your leagues -- copy the key of the one you are drafting into .env:\n")
    for league in leagues:
        print(f"   FF_LEAGUE_KEY={league.league_key}    ({league.name}, {league.num_teams} teams)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
