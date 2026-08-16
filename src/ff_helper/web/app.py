"""FastAPI backend and entry point.

Deliberately small. The interesting code is in ``engine`` and ``draft``; this layer just
exposes it over HTTP and serves one static page. There is no build step and no frontend
framework, because on draft day the app has to start the first time, every time.
"""

from __future__ import annotations

import argparse
import contextlib
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ff_helper.assistant import Assistant
from ff_helper.config import Settings, load_settings
from ff_helper.draft.state import DraftState
from ff_helper.draft.sync import DraftSync
from ff_helper.rankings import cache
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


class ManualPick(BaseModel):
    player_key: str
    pick: int | None = None
    # Auction only. Without the price a recorded sale would corrupt every budget and
    # therefore the whole inflation model, so the UI always sends it.
    cost: int | None = None
    team_key: str | None = None


def create_app(assistant: Assistant, sync: DraftSync | None) -> FastAPI:
    app = FastAPI(title="ff-helper", docs_url=None, redoc_url=None)

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

    @app.post("/api/undo")
    def undo() -> dict:
        with assistant.lock:
            entry = assistant.state.undo_last_manual()
        if entry is None:
            raise HTTPException(status_code=400, detail="No manual picks to undo")
        return {"pick": entry.pick, "player_key": entry.player_key}

    return app


def bootstrap(settings: Settings, league_key: str | None = None) -> tuple[Assistant, DraftSync]:
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
    assistant = Assistant.build(league, state, snapshot, lock=lock)

    sync = DraftSync(client, state, interval=settings.poll_interval, lock=lock)
    return assistant, sync


def main() -> None:
    parser = argparse.ArgumentParser(prog="ff-helper", description="Live draft assistant.")
    parser.add_argument(
        "--league",
        help="League key to run against. Defaults to FF_LEAGUE_KEY. Use this to switch "
        "between leagues without editing .env.",
    )
    parser.add_argument("--port", type=int, default=8777, help="Port to serve on.")
    args = parser.parse_args()

    settings = load_settings()
    assistant, sync = bootstrap(settings, args.league)

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
        sync.stop()


if __name__ == "__main__":
    main()
