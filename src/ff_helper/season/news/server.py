"""A small local endpoint that accepts article text.

Deliberately its own app rather than a route bolted onto ``web/app.py``. That one is the
draft server: it takes an ``Assistant`` built from a draft snapshot, it holds the live
board, and on draft day it has to start the first time, every time. Threading an
in-season mode through it would put new failure modes in front of the one hour of the
year where there is no time to debug anything.

Same origin discipline as the draft bridge, and for the same reason: the gate is
middleware rather than a per-route check, so any route added later is covered by default
instead of by remembering.

The asymmetry from ``draft/bridge.py`` is deliberately **inverted** here. There, an
unresolvable buyer refuses the whole paste, because money charged to nobody inflates every
price in the room. Here nothing blocks. A misattributed news line moves one player by at
most one bounded multiplier and prints the sentence that did it; refusing the article
instead would throw away the twelve facts that did resolve. Unmatched names come back in
the response so the reader can see what was missed.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ff_helper.rankings.players import PlayerRegistry
from ff_helper.season.news.extract import extract
from ff_helper.season.news.store import Article, NewsStore, article_id_for

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", None, ""}

# Origins the userscript may post from. Not "*": this endpoint feeds numbers into lineup
# advice, and a wildcard would let any page you have open write to it.
NEWS_ORIGINS = (
    "https://www.4for4.com",
    "https://4for4.com",
)

# Enough text to be an article. Below this it is a headline, a nav blob, or a misfire, and
# storing it would clutter the log with records that can never produce a fact.
MIN_ARTICLE_CHARS = 200


class PastedArticle(BaseModel):
    text: str
    url: str = ""
    title: str = ""
    captured_at: float | None = None


def create_news_app(
    store: NewsStore,
    registry: PlayerRegistry,
    week: int,
    *,
    bridge_token: str = "",
    position_of: dict[str, str] | None = None,
) -> FastAPI:
    app = FastAPI(title="ff-helper news", docs_url=None, redoc_url=None)
    positions = position_of or {}

    if bridge_token:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(NEWS_ORIGINS),
            allow_methods=["POST"],
            allow_headers=["Content-Type", "X-Bridge-Token"],
        )

    @app.middleware("http")
    async def gate_external_origins(request: Request, call_next):
        origin = request.headers.get("origin", "")
        external = bool(origin) and urlparse(origin).hostname not in LOCAL_HOSTS
        if external and request.headers.get("x-bridge-token") != (bridge_token or "\0"):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Bad or missing bridge token. Start the news server with "
                    "--bridge and copy the token it prints into the reader script."
                },
            )
        return await call_next(request)

    @app.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse(_PASTE_PAGE.replace("__WEEK__", str(week)))

    @app.get("/api/news/state")
    def state() -> dict:
        return {
            "week": week,
            "articles": len(store.articles),
            "flags": len(store.flags),
            "caveats": len(store.caveats),
            "players_with_news": sorted(store.players_with_news(week)),
        }

    @app.post("/api/news/paste")
    def paste_news(body: PastedArticle) -> dict:
        """Ingest one article. Advisory only -- this never writes to Yahoo."""
        text = (body.text or "").strip()
        if len(text) < MIN_ARTICLE_CHARS:
            return {
                "stored": False,
                "reason": f"only {len(text)} characters; too short to be an article",
                "flags": [],
                "caveats": 0,
            }

        captured_at = body.captured_at or time.time()
        article = Article(
            article_id=article_id_for(body.url, body.title, captured_at),
            url=body.url.strip(),
            title=body.title.strip(),
            source=_source_of(body.url),
            captured_at=captured_at,
            week=week,
            text=text,
        )
        if store.has_article(article.article_id):
            return {
                "stored": False,
                "reason": "already captured",
                "article_id": article.article_id,
                "flags": [],
                "caveats": 0,
            }

        found, caveats = extract(article, registry, position_of=positions)
        store.append(article, found, caveats)

        return {
            "stored": True,
            "article_id": article.article_id,
            "title": article.title,
            "flags": [
                {
                    "player": _name(registry, flag.player_key),
                    "flag": flag.flag,
                    "multiplier": flag.multiplier,
                    "confidence": flag.confidence,
                    "quote": flag.quote,
                }
                for flag in found
            ],
            "caveats": len(caveats),
        }

    return app


def _source_of(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.removeprefix("www.") or "paste"


def _name(registry: PlayerRegistry, player_key: str) -> str:
    player = registry.by_key.get(player_key)
    return player.full_name if player else player_key


# Same-origin, no CORS, no private-network check, nothing to install. This is the path
# that cannot break, so it is the one that always exists -- the userscript is the
# convenience on top of it, not the other way round.
_PASTE_PAGE = """
<!doctype html>
<meta charset="utf-8">
<title>ff-helper news</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 46rem; margin: 3rem auto;
         padding: 0 1rem; background: #12141a; color: #e6e9ef; }
  h1 { font-size: 1.2rem; font-weight: 600; }
  p { color: #99a1b3; }
  textarea { width: 100%; min-height: 16rem; background: #171a21; color: #e6e9ef;
             border: 1px solid #2a2f3a; border-radius: 8px; padding: .8rem;
             font: 13px/1.5 ui-monospace, monospace; }
  input { width: 100%; background: #171a21; color: #e6e9ef; border: 1px solid #2a2f3a;
          border-radius: 8px; padding: .5rem .7rem; margin-bottom: .6rem; }
  button { margin-top: .8rem; padding: .6rem 1.1rem; border-radius: 8px;
           border: 1px solid #2a2f3a; background: #222735; color: #e6e9ef;
           cursor: pointer; font-size: .95rem; }
  #out { margin-top: 1.2rem; white-space: pre-wrap; font: 13px/1.6 ui-monospace, monospace; }
  .ok { color: #4ade80; } .warn { color: #fbbf24; } .bad { color: #f87171; }
</style>
<h1>Paste an article &mdash; week __WEEK__</h1>
<p>Select the article text, copy, paste below. Only phrases in the flag vocabulary move a
projection; everything else is kept as a caveat. Nothing here is sent anywhere but this
machine.</p>
<input id="url" placeholder="source URL (optional, but makes re-pastes idempotent)">
<input id="title" placeholder="headline (optional)">
<textarea id="text" placeholder="Paste the article text here"></textarea>
<button id="send">Ingest</button>
<div id="out"></div>
<script>
const out = document.getElementById("out");
document.getElementById("send").addEventListener("click", async () => {
  const text = document.getElementById("text").value;
  out.textContent = "working...";
  try {
    const res = await fetch("/api/news/paste", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        url: document.getElementById("url").value,
        title: document.getElementById("title").value,
      }),
    });
    const body = await res.json();
    if (!body.stored) {
      out.className = "warn";
      out.textContent = "Not stored: " + (body.reason || "unknown");
      return;
    }
    const lines = [body.flags.length + " flag(s), " + body.caveats + " caveat(s)"];
    for (const f of body.flags) {
      lines.push("  " + f.player + "  " + f.flag + "  x" + f.multiplier +
                 (f.confidence === "inferred" ? "  (inferred)" : "") + "\\n      " + f.quote);
    }
    out.className = body.flags.length ? "ok" : "warn";
    out.textContent = lines.join("\\n");
  } catch (err) {
    out.className = "bad";
    out.textContent = "Failed: " + err;
  }
});
</script>
"""
