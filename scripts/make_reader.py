#!/usr/bin/env python3
"""Write a ready-to-paste reader script with your token already filled in.

    uv run python scripts/make_reader.py

Produces `scripts/yahoo_bridge.ready.js` — open it, select all, paste into Tampermonkey.
Nothing to edit.

It exists because the alternative is "find line 44 and replace PASTE_TOKEN_HERE", which is
a step to get wrong at the exact moment you are trying to draft. The generated file holds
a credential, so it is gitignored.

Pass --console for the DevTools-console variant instead, and --port if ff-helper is not on
8777.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ff_helper.config import bridge_token  # noqa: E402

HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--console",
        action="store_true",
        help="Generate the console version rather than the Tampermonkey one.",
    )
    parser.add_argument(
        "--news",
        action="store_true",
        help="Generate the in-season 4for4 news reader instead of the draft one. "
        "Same token, different endpoint and default port.",
    )
    args = parser.parse_args()

    if args.news:
        source = HERE / "news_bridge.user.js"
        target = HERE / "news_bridge.ready.js"
        default_port = 8778
    elif args.console:
        source = HERE / "yahoo_bridge_console.js"
        target = HERE / "yahoo_bridge_console.ready.js"
        default_port = 8777
    else:
        source = HERE / "yahoo_bridge.user.js"
        target = HERE / "yahoo_bridge.ready.js"
        default_port = 8777

    port = args.port or default_port

    text = source.read_text(encoding="utf-8")
    if "PASTE_TOKEN_HERE" not in text:
        print(f"{source.name} has no PASTE_TOKEN_HERE placeholder; nothing to fill in.")
        return 1

    token = bridge_token()
    text = text.replace('"PASTE_TOKEN_HERE"', f'"{token}"')
    text = text.replace(f"const PORT = {default_port};", f"const PORT = {port};")
    target.write_text(text, encoding="utf-8")
    target.chmod(0o600)

    print(f"Wrote {target}")
    print(f"   port {port}, token {token[:4]}…{token[-4:]}")
    print()
    if args.console:
        print("Open it, copy the whole file, paste into the draft room's DevTools console.")
    else:
        print("Open it, copy the whole file, and paste it into Tampermonkey:")
        print("   Tampermonkey icon -> Dashboard -> your ff-helper script -> select all -> paste")
    if args.news:
        print("\nStart the news server with:")
        print(f"   uv run python scripts/news.py --serve --bridge --port {port}")
        print("or the reader will get a 401.")
    else:
        print("\nStart ff-helper with --bridge, or the reader will get a 401.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
