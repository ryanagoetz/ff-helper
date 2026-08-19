# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                    # install (dev deps included)
uv run pytest                              # full suite; no network required
uv run pytest tests/test_engine.py::test_x # a single test
uv run ruff check .                        # lint (line-length 100; E,F,I,UP,B,SIM)
```

Running it:

```bash
uv run ff-helper                                        # live, against FF_LEAGUE_KEY
uv run ff-helper --league 461.l.111111 --port 8778      # a specific league
uv run ff-helper --offline data/league-mine.yaml        # no Yahoo API at all
uv run ff-helper --bridge                               # accept draft-room readings
```

Supporting scripts (all `uv run python scripts/...`):

- `doctor.py --all` — preflight against real Yahoo data; run before anything else
- `setup_auth.py` — one-time OAuth paste flow, then prints your league keys
- `fetch_rankings.py --projections file.csv` — build the on-disk snapshot; also `--offline <config.yaml>` and `--league <key>`
- `replay.py [--league|--from-file] [--dump path]` — replay a completed draft through the engine
- `backtest.py --file record.json [--time] [--predictor mc]` — hits, calibration (Brier), counterfactual roster
- `make_reader.py` — emit `yahoo_bridge.ready.js` with the bridge token filled in
- `evaluate_keepers.py`, `mock_config.py` — keeper value report; league YAML from a mock room

Runtime state (OAuth token, ranking cache, bridge token) lives in `~/.ff-helper/`, never the repo. Tests isolate it via `FF_HELPER_HOME` (see [tests/conftest.py](tests/conftest.py)).

## Architecture

Two phases, deliberately separated so draft day never depends on the network:

**Fetch (once, the day before)** — `scripts/fetch_rankings.py` pulls the Yahoo player pool, FFC ADP, FantasyPros rankings, and a projections CSV, then writes raw `SourceRow`s to a versioned snapshot in `rankings/cache.py`. Raw inputs are cached, not the finished blend, which is what lets `replay.py` recompute valuations offline.

**Draft (live)** — `Assistant.build` ([src/ff_helper/assistant.py](src/ff_helper/assistant.py)) is the single wiring point: snapshot → `PlayerRegistry.crosswalk` (name matching) → `rankings/blend.py` (one valuation per player) → `engine/replacement.py` (baselines from the league's real starting slots) → auction par values if applicable. `Assistant` then holds the fixed valuation model plus the live `DraftState`, and answers one question: given what's gone, who should I take?

The dataflow that matters:

```
yahoo/      OAuth, HTTP, and parse.py -- Yahoo's XML-derived JSON is quarantined here
rankings/   sources -> crosswalk -> blend -> snapshot
engine/     scoring -> replacement -> {vona.py (snake) | auction.py (auction)}
draft/      state.py (the authoritative board), sync.py (poller), bridge.py, keepers.py
web/        FastAPI + one static page, no build step
backtest/   capture, calibration, counterfactual -- how engine changes get justified
offline.py  a parallel data layer that substitutes for yahoo/ entirely
```

### Load-bearing design decisions

**Value and timing never collapse into one number.** Projected points (re-scored under *your* league's modifiers, `engine/scoring.py`) answer "how good"; ADP answers "when will he be gone". `blend.py` deliberately discards a source's own points total, because it carries the exporter's scoring — so a projections CSV without per-stat columns is rejected rather than silently producing a 0.0 board.

**Snake and auction are different problems and share only the valuation layer.** `vona.py` prices pick scarcity (conditional survival, logistic ADP tails, a needs-to-picks plan DP); `auction.py` prices dollar scarcity (par values, live inflation, max bid as a hard constraint). Nothing above `replacement.py` is shared.

**The board is the source of truth; the poller is just one of its writers.** `DraftState` accepts picks from the Yahoo poller, manual entry, and the draft-room bridge. Conflicts resolve toward Yahoo, but a superseded manual entry is reported, never silently overwritten.

**Failures are asymmetric, and the code takes sides.** An unresolvable *buyer* in an auction is refused outright (money charged to nobody inflates every remaining price); an unresolvable *player* only degrades toward stale. An unmatched keeper name is a hard error, not a skipped row. A name-match miss drops a player from every recommendation with no error, so `rankings/players.py` matches in explicit layers and everything unmatched is reported.

**Concurrency.** `sync.py` runs on its own thread and writes `DraftState` under `Assistant.lock`. Read the board under the lock, copy what you need, then run the pure engine functions outside it — `snake_recommendations` is the pattern to follow.

### Working in this codebase

- **Module docstrings carry the reasoning**, not just the summary. Read the top of a module before changing it; if you change a modeling decision, update the docstring's justification too.
- **Engine changes are gated on backtests.** Changing a model constant means re-running `scripts/backtest.py` on a real draft record and keeping the change only if Brier or the counterfactual roster improves. Every engine change in the history was justified that way.
- **New behavior gets tested against fixtures, not the network.** `tests/fixtures/` holds real Yahoo JSON/HTML variants; `tests/helpers.py` builds a full synthetic league for engine and web tests.
- **Snapshot format changes require bumping the version in `rankings/cache.py`** — old snapshots are refused so a missing field can't read as `None`.
- **`data/` is gitignored by default** (league configs name a real league; projection exports aren't ours to redistribute). Only `data/README.md`, `league-example.yaml`, and anonymized `data/drafts/*.json` are tracked. `MOCK-DRAFT.md` and `checklist.md` are local-only personal runbooks.

Known modeling gaps are listed under "Limitations" in [README.md](README.md) — uneven keeper counts, untracked auction nominations, unmodeled bonus stats, no third-round reversal.
