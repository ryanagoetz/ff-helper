"""FastAPI backend and entry point.

Deliberately small. The interesting code is in ``engine`` and ``draft``; this layer just
exposes it over HTTP and serves one static page. There is no build step and no frontend
framework, because on draft day the app has to start the first time, every time.
"""

from __future__ import annotations

import argparse
import contextlib
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ff_helper import offline
from ff_helper.assistant import Assistant
from ff_helper.config import Settings, load_settings
from ff_helper.draft import bridge, keepers
from ff_helper.draft.state import BridgeSale, DraftState
from ff_helper.draft.sync import DraftSync
from ff_helper.rankings import cache
from ff_helper.rankings.players import PlayerRegistry
from ff_helper.yahoo.client import YahooClient

STATIC_DIR = Path(__file__).parent / "static"


def _serialize(pick, is_auction: bool) -> dict:
    """Common player fields, plus whichever model's numbers apply."""
    valuation = pick.valuation
    common = {
        "player_key": valuation.player_key,
        "name": pick.name,
        "position": pick.position,
        "team": valuation.team,
        "bye": valuation.bye_week,
        "status": valuation.status,
        "tier": valuation.tier,
        "points": round(valuation.projected_points, 1),
        "estimated": valuation.points_estimated,
        "score": round(pick.score, 1),
        "reason": pick.reason,
    }
    if is_auction:
        common.update(
            {
                "value": round(pick.value, 1),
                "par": round(pick.par, 1),
                "market": round(pick.market, 1) if pick.market is not None else None,
                "surplus": round(pick.surplus, 1) if pick.surplus is not None else None,
                "bid_to": pick.bid_to,
                "affordable": pick.affordable,
            }
        )
    else:
        common.update(
            {
                "adp": round(valuation.adp, 1),
                "vor": round(pick.vor, 1),
                "vona": round(pick.vona, 1),
                "survival": round(pick.survival_to_next, 3),
            }
        )
    return common


class PastedBoard(BaseModel):
    text: str


def _resolution_failure(report: bridge.ResolutionReport) -> str:
    """Explain exactly which rows blocked the paste, and why it refused rather than part-applied."""
    problems: list[str] = []
    if report.unknown_buyers:
        # Listed first because it is the expensive one: a sale charged to no team leaves
        # the money in the room, so inflation reads the league as richer than it is.
        names = ", ".join(sorted({sale.buyer or "(blank)" for sale in report.unknown_buyers}))
        problems.append(
            f"{len(report.unknown_buyers)} sale(s) name a buyer that is not a team in this "
            f"league: {names}. Fix the name, or add an alias under 'team_aliases' in the "
            "league config."
        )
    if report.unknown_players:
        names = ", ".join(sale.name for sale in report.unknown_players[:8])
        problems.append(f"{len(report.unknown_players)} player(s) did not match: {names}")
    if report.missing_price:
        names = ", ".join(sale.name for sale in report.missing_price[:8])
        problems.append(f"{len(report.missing_price)} auction sale(s) have no price: {names}")
    problems.append("Nothing was applied; the board is unchanged.")
    return " ".join(problems)


class ManualPick(BaseModel):
    player_key: str
    pick: int | None = None
    # Auction only. Without the price a recorded sale would corrupt every budget and
    # therefore the whole inflation model, so the UI always sends it.
    cost: int | None = None
    team_key: str | None = None


def create_app(assistant: Assistant, sync: DraftSync | None) -> FastAPI:
    app = FastAPI(title="ff-helper", docs_url=None, redoc_url=None)

    # Built once and kept: the resolver memoizes every name it has looked up, including
    # the ones it failed to match. Rebuilding per request would throw that away and pay
    # for a full fuzzy scan of the pool again on every paste.
    cached: list[bridge.BridgeResolver] = []

    def _resolver() -> bridge.BridgeResolver:
        if not cached:
            cached.append(
                bridge.BridgeResolver(
                    assistant.registry,
                    assistant.state.teams,
                    team_aliases=assistant.team_aliases,
                )
            )
        return cached[0]

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state() -> dict:
        payload = assistant.snapshot_state()
        payload["sync_healthy"] = sync.healthy if sync else False
        payload["sync_running"] = sync is not None
        return payload

    @app.get("/api/recommend")
    def recommend(limit: int = 8) -> dict:
        picks = assistant.recommendations(limit=limit)
        payload = {
            "draft_type": "auction" if assistant.is_auction else "snake",
            "current_pick": assistant.state.current_pick,
            "is_my_turn": assistant.state.is_my_turn,
            "recommendations": [_serialize(pick, assistant.is_auction) for pick in picks],
        }
        if assistant.is_auction:
            payload["inflation"] = round(assistant.current_inflation(), 3)
            payload["max_bid"] = assistant.state.my_max_bid()
        return payload

    @app.get("/api/search")
    def search(q: str, limit: int = 10) -> dict:
        return {
            "results": [
                {
                    "player_key": valuation.player_key,
                    "name": valuation.name,
                    "position": valuation.position,
                    "team": valuation.team,
                    "adp": round(valuation.adp, 1),
                }
                for valuation in assistant.search(q, limit=limit)
            ]
        }

    @app.post("/api/pick")
    def manual_pick(body: ManualPick) -> dict:
        """Mark a player drafted by hand, for when the Yahoo feed stalls."""
        if body.player_key not in assistant.valuations.valuations:
            raise HTTPException(status_code=404, detail="Unknown player")

        # A player already on the board must not be recorded twice. `spent` sums board
        # entries without deduplicating by player, so a second entry charges his buyer a
        # second time. The UI hides drafted players from search, but that guard is stale
        # for as long as it takes to type a price and choose a buyer -- and a pasted board
        # landing in that window is exactly when this fires.
        existing = assistant.state.pick_for_player(body.player_key)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"{assistant._player_name(body.player_key)} is already on the board "
                f"at pick {existing}. Recording him again would charge his buyer twice.",
            )
        if assistant.is_auction:
            # Both halves matter. Without the price the budget is wrong; without the buyer
            # the money leaves nobody's budget, so the league looks richer than it is and
            # every remaining player gets over-valued.
            if body.cost is None:
                raise HTTPException(
                    status_code=400,
                    detail="An auction sale needs the price it went for; budgets depend on it.",
                )
            if not body.team_key:
                raise HTTPException(
                    status_code=400,
                    detail="An auction sale needs the buying team; budgets depend on it.",
                )
        with assistant.lock:
            entry = assistant.state.record_manual(
                body.player_key, pick=body.pick, cost=body.cost, team_key=body.team_key
            )
        return {"pick": entry.pick, "player_key": entry.player_key, "cost": entry.cost}

    @app.post("/api/board/paste")
    def paste_board(body: PastedBoard) -> dict:
        """Ingest a full reading of the draft room, pasted from the Yahoo tab.

        Same origin as the app, so this needs no CORS, no browser-policy exemption and no
        extension -- which is why it exists before any automated reader. It is also the
        recovery path if an automated reader breaks mid-draft.

        Deliberately all-or-nothing on resolution failures. A human pasted this and is
        looking at the result, so refusing with a specific complaint is more useful than
        applying nine of ten sales and burying the tenth in a notes list.
        """
        sales = bridge.parse_paste(body.text)
        if not sales:
            raise HTTPException(
                status_code=400,
                detail="Nothing in that paste looked like a sale. Expected lines such as "
                "'Ja'Marr Chase\t$55\tTeam Name'.",
            )

        report = _resolver().resolve_all(sales, is_auction=assistant.is_auction)

        if not report.ok:
            raise HTTPException(status_code=422, detail=_resolution_failure(report))

        with assistant.lock:
            diff = assistant.state.apply_bridge(
                [
                    BridgeSale(player_key=key, team_key=team, cost=sale.cost)
                    for sale, key, team in report.resolved
                ],
                timestamp=time.time(),
            )

        if diff.rejected:
            raise HTTPException(status_code=409, detail=diff.rejected)

        return {
            "read": len(sales),
            "applied": len(diff.applied),
            "corrected": len(diff.corrected),
            "removed": len(diff.removed),
            "unchanged": diff.unchanged,
            "fuzzy": [{"name": name, "player_key": key} for name, key in report.fuzzy],
        }

    @app.post("/api/undo")
    def undo() -> dict:
        with assistant.lock:
            entry = assistant.state.undo_last_manual()
        if entry is None:
            raise HTTPException(status_code=400, detail="No manual picks to undo")
        return {"pick": entry.pick, "player_key": entry.player_key}

    return app


def bootstrap(
    settings: Settings,
    league_key: str | None = None,
    keeper_csv: Path | None = None,
) -> tuple[Assistant, DraftSync]:
    """Load everything needed to serve, failing with advice rather than a traceback."""
    league_key = league_key or settings.league_key
    if not league_key:
        raise SystemExit(
            "No league key.\nRun `python scripts/setup_auth.py` to list your leagues, then "
            "either paste the key into .env as FF_LEAGUE_KEY or pass --league."
        )

    snapshot = cache.load(league_key)
    if snapshot is None:
        raise SystemExit(
            f"No ranking snapshot found for {league_key}.\n"
            f"Run `python scripts/fetch_rankings.py --league {league_key}` first "
            "(ideally the day before your draft)."
        )
    if snapshot.age_hours > 48:
        print(
            f"Warning: ranking snapshot is {snapshot.age_hours:.0f} hours old. "
            "Consider re-running scripts/fetch_rankings.py."
        )

    client = YahooClient(settings)
    league = client.league(league_key)
    teams = client.teams(league_key)

    lock = threading.Lock()
    state = DraftState(league=league, teams=teams)

    # Keepers must be resolved before the assistant is built: they change the player pool,
    # the roster counts, the budgets and the number of rounds.
    registry = PlayerRegistry(snapshot.players)
    startup_notes: list[str] = []

    if league.draft_status != "predraft":
        # Restarting after the draft has opened is the normal recovery path here (crash,
        # reboot, a stalled feed), and it must not cost us the keepers -- they would go
        # straight back into the pool and be recommended for the rest of the draft. Seed
        # the board *first* so that players already drafted are recognised as picks rather
        # than mistaken for keepers; DraftState then resolves the overlap in favour of the
        # board.
        try:
            state.apply_sync(client.draft_results(league_key), timestamp=time.time())
        except Exception as exc:  # noqa: BLE001 - a missing board is not fatal here
            startup_notes.append(f"Could not read the draft board before keepers ({exc})")

    rostered, roster_failures = client.keepers(teams)
    if roster_failures:
        startup_notes.append(
            f"Could not read {len(roster_failures)} of {len(teams)} rosters, so any keepers "
            f"they hold are still in the pool: {'; '.join(roster_failures)}"
        )
    try:
        keeper_set = keepers.resolve(rostered, teams, registry, keeper_csv)
    except keepers.KeeperError as exc:
        raise SystemExit(str(exc)) from exc

    state.apply_keepers(keeper_set.kept)
    assistant = Assistant.build(league, state, snapshot, lock=lock)
    assistant.notes.extend(keeper_set.notes)
    assistant.notes.extend(startup_notes)

    sync = DraftSync(client, state, interval=settings.poll_interval, lock=lock)
    return assistant, sync


def bootstrap_offline(config_path: Path, keeper_csv: Path | None = None) -> Assistant:
    """Serve with no Yahoo access: league from YAML, picks typed in by hand.

    There is no sync object at all, rather than a stubbed one. ``create_app`` already
    treats ``sync=None`` as "not running", so the UI reports the feed as absent instead
    of showing a sync indicator that is green and lying.
    """
    try:
        config = offline.load_config(config_path)
    except offline.OfflineConfigError as exc:
        raise SystemExit(str(exc)) from exc

    league = config.league
    snapshot = cache.load(league.league_key)
    if snapshot is None:
        raise SystemExit(
            f"No ranking snapshot found for {league.name}.\n"
            f"Run `python scripts/fetch_rankings.py --offline {config_path}` first."
        )

    lock = threading.Lock()
    state = DraftState(league=league, teams=config.teams)
    registry = PlayerRegistry(snapshot.players)

    # Offline there are no pre-draft rosters to read, so a keeper league needs the CSV.
    # Passing no rostered players makes resolve() fall back to an empty Yahoo set, which
    # is correct: we genuinely do not know of any keepers unless told.
    try:
        keeper_set = keepers.resolve([], config.teams, registry, keeper_csv)
    except keepers.KeeperError as exc:
        raise SystemExit(str(exc)) from exc

    state.apply_keepers(keeper_set.kept)
    assistant = Assistant.build(league, state, snapshot, lock=lock)
    assistant.team_aliases = config.team_aliases
    assistant.notes.extend(config.notes)
    assistant.notes.extend(keeper_set.notes)
    assistant.notes.append(
        "Offline mode: no Yahoo feed, so every pick must be entered by hand."
    )
    if not keeper_set.kept:
        assistant.notes.append(
            "No keepers loaded. Offline there is no roster to read, so pass --keepers "
            "if this is a keeper league."
        )
    return assistant


def main() -> None:
    parser = argparse.ArgumentParser(prog="ff-helper", description="Live draft assistant.")
    parser.add_argument(
        "--league",
        help="League key to run against. Defaults to FF_LEAGUE_KEY. Use this to switch "
        "between leagues without editing .env.",
    )
    parser.add_argument(
        "--keepers",
        type=Path,
        help="CSV of kept players, overriding Yahoo. Columns: player,team,cost,round. "
        "Use when your league settles keepers outside Yahoo.",
    )
    parser.add_argument(
        "--offline",
        type=Path,
        metavar="CONFIG",
        help="Run with no Yahoo API access, from a league config YAML. Picks are entered "
        "by hand and there is no live feed. Use when API approval has not arrived.",
    )
    parser.add_argument("--port", type=int, default=8777, help="Port to serve on.")
    args = parser.parse_args()

    sync: DraftSync | None
    if args.offline:
        assistant = bootstrap_offline(args.offline, args.keepers)
        sync = None
    else:
        settings = load_settings()
        assistant, sync = bootstrap(settings, args.league, args.keepers)

        # Prime the board before serving, so the first page load is already accurate.
        try:
            sync.poll_once()
        except Exception as exc:  # noqa: BLE001
            print(f"Initial draft sync failed ({exc}); starting anyway.")

        sync.start()

    app = create_app(assistant, sync)

    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  ff-helper ready at {url}")
    print(f"  {assistant.league.name} -- {len(assistant.valuations.valuations)} players valued")
    if assistant.state.my_slot:
        upcoming = ", ".join(str(pick) for pick in assistant.state.my_picks[:6])
        print(f"  You draft from slot {assistant.state.my_slot}: picks {upcoming} ...")
    for note in assistant.notes:
        print(f"  note: {note}")
    print()

    # A headless machine has no browser to open; that is not a failure worth reporting.
    with contextlib.suppress(Exception):
        webbrowser.open(url)

    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        if sync is not None:
            sync.stop()


if __name__ == "__main__":
    main()
