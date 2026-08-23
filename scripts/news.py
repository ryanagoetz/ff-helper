#!/usr/bin/env python3
"""Capture article text so the weekly projections can react to it.

    uv run python scripts/news.py --serve            # paste box at 127.0.0.1:8778
    uv run python scripts/news.py --serve --bridge   # ...and accept the userscript
    uv run python scripts/news.py --file article.txt # ingest one file, no server
    uv run python scripts/news.py --list             # what is stored for this week

Only phrases in the closed vocabulary (``season/news/flags.py``) move a projection, each
by a constant bounded multiplier, and every adjustment carries the sentence that caused
it. Anything else is kept as a caveat and printed beside the recommendation rather than
folded into it. Nothing here is sent anywhere -- it is read, matched, and written to a
local log.

Needs a week snapshot to exist, because the player registry comes from it:

    uv run python scripts/weekly.py --fetch
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ff_helper.config import bridge_token, cache_dir, load_settings  # noqa: E402
from ff_helper.rankings.players import PlayerRegistry  # noqa: E402
from ff_helper.season import cache as week_cache  # noqa: E402
from ff_helper.season.news.extract import extract  # noqa: E402
from ff_helper.season.news.server import create_news_app  # noqa: E402
from ff_helper.season.news.store import Article, NewsStore, article_id_for  # noqa: E402

DEFAULT_PORT = 8778


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", help="League key. Defaults to FF_LEAGUE_KEY.")
    parser.add_argument("--week", type=int, help="Defaults to the latest stored week.")
    parser.add_argument("--serve", action="store_true", help="Run the paste-box server.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--bridge",
        action="store_true",
        help="Accept posts from the 4for4 userscript, and print the token it needs. "
        "Without this the server answers only its own page.",
    )
    parser.add_argument("--file", type=Path, help="Ingest one article from a text file.")
    parser.add_argument("--url", default="", help="Source URL for --file.")
    parser.add_argument("--title", default="", help="Headline for --file.")
    parser.add_argument("--list", action="store_true", help="Show what is stored.")
    args = parser.parse_args()

    settings = load_settings(require_credentials=False)
    league_key = args.league or settings.league_key
    if not league_key:
        print("No league key. Set FF_LEAGUE_KEY in .env or pass --league.")
        return 1

    snapshot = _snapshot(league_key, args.week)
    if snapshot is None:
        print(
            f"No stored week for {league_key}. The player registry comes from it, so "
            "there is nothing to match names against yet.\n"
            "Run: uv run python scripts/weekly.py --fetch"
        )
        return 1

    store = NewsStore.load(league_key, snapshot.season)
    registry = PlayerRegistry(snapshot.players)
    positions = {player.player_key: player.primary_position for player in snapshot.players}

    if args.list:
        return _report(store, registry, snapshot.week)
    if args.file:
        return _ingest_file(args, store, registry, snapshot.week, positions)
    if args.serve:
        return _serve(args, store, registry, snapshot.week, positions)

    parser.print_help()
    return 1


def _snapshot(league_key: str, week: int | None):
    safe = league_key.replace("/", "_")
    seasons = sorted(
        {
            path.stem.split("-")[-2]
            for path in cache_dir().glob(f"week-{safe}-*.json")
            if len(path.stem.split("-")) >= 3
        },
        reverse=True,
    )
    for season in seasons:
        found = (
            week_cache.load(league_key, season, week)
            if week is not None
            else week_cache.latest(league_key, season)
        )
        if found is not None:
            return found
    return None


def _ingest_file(args, store: NewsStore, registry, week: int, positions) -> int:
    try:
        text = args.file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Could not read {args.file}: {exc}")
        return 1

    captured_at = time.time()
    article = Article(
        article_id=article_id_for(args.url, args.title or args.file.name, captured_at),
        url=args.url,
        title=args.title or args.file.name,
        source="file",
        captured_at=captured_at,
        week=week,
        text=text,
    )
    if store.has_article(article.article_id):
        print("Already captured; nothing added.")
        return 0

    found, caveats = extract(article, registry, position_of=positions)
    store.append(article, found, caveats)

    print(f"Stored {article.title} -- {len(found)} flag(s), {len(caveats)} caveat(s)")
    for flag in found:
        marker = " (inferred)" if flag.confidence == "inferred" else ""
        print(f"  {_name(registry, flag.player_key)}: {flag.flag} "
              f"x{flag.multiplier}{marker}")
        print(f"      {flag.quote}")
    if not found:
        print("  Nothing in the vocabulary matched. Kept as caveats only.")
    return 0


def _report(store: NewsStore, registry, week: int) -> int:
    print(f"Week {week}: {len(store.articles)} article(s), {len(store.flags)} flag(s), "
          f"{len(store.caveats)} caveat(s)")
    print(f"  log: {store.path}")

    live = sorted(store.players_with_news(week))
    if not live:
        print("\nNothing live for this week.")
        return 0

    print("\nLive adjustments:")
    for player_key in live:
        availability, role, flags = store.adjustment_for(player_key, week)
        effective = availability * role
        if not flags:
            continue
        print(f"  {_name(registry, player_key)}  x{effective:.2f}")
        for flag in flags:
            age = flag.age_hours()
            stale = "  (stale)" if age > 72 else ""
            marker = " (inferred)" if flag.confidence == "inferred" else ""
            print(f"      {flag.flag} x{flag.multiplier}{marker}  {age:.0f}h ago{stale}")
            print(f"      \"{flag.quote}\"")
    return 0


def _serve(args, store: NewsStore, registry, week: int, positions) -> int:
    import uvicorn

    token = bridge_token() if args.bridge else ""
    app = create_news_app(store, registry, week, bridge_token=token, position_of=positions)

    print(f"News intake for week {week} on http://127.0.0.1:{args.port}")
    print(f"  log: {store.path}")
    print(f"  {len(store.articles)} article(s) already captured")
    if token:
        print(f"\n  bridge token: {token}")
        print("  Fill it into the userscript with:")
        print(f"    uv run python scripts/make_reader.py --news --port {args.port}")
    else:
        print("\n  Paste box only. Pass --bridge to also accept the 4for4 userscript.")

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def _name(registry, player_key: str) -> str:
    player = registry.by_key.get(player_key)
    return player.full_name if player else player_key


if __name__ == "__main__":
    raise SystemExit(main())
