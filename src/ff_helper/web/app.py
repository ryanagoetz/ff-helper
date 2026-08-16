"""FastAPI backend and entry point.

Deliberately small. The interesting code is in ``engine`` and ``draft``; this layer just
exposes it over HTTP and serves one static page. There is no build step and no frontend
framework, because on draft day the app has to start the first time, every time.
"""

from __future__ import annotations

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


class ManualPick(BaseModel):
    player_key: str
    pick: int | None = None


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
        return {
            "current_pick": assistant.state.current_pick,
            "is_my_turn": assistant.state.is_my_turn,
            "recommendations": [
                {
                    "player_key": pick.valuation.player_key,
                    "name": pick.name,
                    "position": pick.position,
                    "team": pick.valuation.team,
                    "bye": pick.valuation.bye_week,
                    "status": pick.valuation.status,
                    "tier": pick.valuation.tier,
                    "adp": round(pick.valuation.adp, 1),
                    "points": round(pick.valuation.projected_points, 1),
                    "vor": round(pick.vor, 1),
                    "vona": round(pick.vona, 1),
                    "score": round(pick.score, 1),
                    "survival": round(pick.survival_to_next, 3),
                    "estimated": pick.valuation.points_estimated,
                    "reason": pick.reason,
                }
                for pick in picks
            ],
        }

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
        with assistant.lock:
            entry = assistant.state.record_manual(body.player_key, pick=body.pick)
        return {"pick": entry.pick, "player_key": entry.player_key}

    @app.post("/api/undo")
    def undo() -> dict:
        with assistant.lock:
            entry = assistant.state.undo_last_manual()
        if entry is None:
            raise HTTPException(status_code=400, detail="No manual picks to undo")
        return {"pick": entry.pick, "player_key": entry.player_key}

    return app


def bootstrap(settings: Settings) -> tuple[Assistant, DraftSync]:
    """Load everything needed to serve, failing with advice rather than a traceback."""
    if not settings.league_key:
        raise SystemExit(
            "FF_LEAGUE_KEY is not set in .env.\nRun `python scripts/setup_auth.py` to list "
            "your leagues, then paste the key into .env."
        )

    snapshot = cache.load(settings.league_key)
    if snapshot is None:
        raise SystemExit(
            "No ranking snapshot found.\nRun `python scripts/fetch_rankings.py` first "
            "(ideally the day before your draft)."
        )
    if snapshot.age_hours > 48:
        print(
            f"Warning: ranking snapshot is {snapshot.age_hours:.0f} hours old. "
            "Consider re-running scripts/fetch_rankings.py."
        )

    client = YahooClient(settings)
    league = client.league(settings.league_key)
    teams = client.teams(settings.league_key)

    lock = threading.Lock()
    state = DraftState(league=league, teams=teams)
    assistant = Assistant.build(league, state, snapshot, lock=lock)

    sync = DraftSync(client, state, interval=settings.poll_interval, lock=lock)
    return assistant, sync


def main() -> None:
    settings = load_settings()
    assistant, sync = bootstrap(settings)

    # Prime the board before serving, so the first page load is already accurate.
    try:
        sync.poll_once()
    except Exception as exc:  # noqa: BLE001
        print(f"Initial draft sync failed ({exc}); starting anyway.")

    sync.start()
    app = create_app(assistant, sync)

    url = "http://127.0.0.1:8777"
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
        uvicorn.run(app, host="127.0.0.1", port=8777, log_level="warning")
    finally:
        sync.stop()


if __name__ == "__main__":
    main()
