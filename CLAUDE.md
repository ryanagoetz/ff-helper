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

- `doctor.py --all` — preflight against real Yahoo data; run before anything else. `--season` also probes the in-season endpoints
- `setup_auth.py` — one-time OAuth paste flow, then prints your league keys
- `fetch_rankings.py --projections file.csv` — build the on-disk snapshot; also `--offline <config.yaml>` and `--league <key>`
- `weekly.py [--fetch] [--week N] [--json]` — build/read one week's in-season snapshot
- `news.py [--serve] [--file x.txt] [--list]` — capture article text; `--serve --bridge` accepts the userscript
- `make_reader.py --news` — emit `news_bridge.ready.js` with the token filled in
- `replay.py [--league|--from-file] [--dump path]` — replay a completed draft through the engine
- `backtest.py --file record.json [--time] [--predictor mc] [--follow-from N] [--stop-after N]` — hits, calibration (Brier), counterfactual roster. Auctions replay at recorded prices; `--follow-from` hands over to the policy mid-draft, `--stop-after` cuts the comparison at pick N and fills every policy's remaining slots the same way (including `actual`'s), for records where the human stopped deciding partway — autopick, walked away — and whose later buys are not a decision any policy can be graded against
- `make_reader.py` — emit `yahoo_bridge.ready.js` with the bridge token filled in
- `evaluate_keepers.py`, `mock_config.py` — keeper value report; league YAML from a mock room

Runtime state (OAuth token, ranking cache, bridge token) lives in `~/.ff-helper/`, never the repo. Tests isolate it via `FF_HELPER_HOME` (see [tests/conftest.py](tests/conftest.py)).

## Architecture

Two phases, deliberately separated so draft day never depends on the network — plus a
third, in-season phase built on the same split (see "In-season" below):

**Fetch (once, the day before)** — `scripts/fetch_rankings.py` pulls the Yahoo player pool, FFC ADP, FantasyPros rankings, and a projections CSV, then writes raw `SourceRow`s to a versioned snapshot in `rankings/cache.py`. Raw inputs are cached, not the finished blend, which is what lets `replay.py` recompute valuations offline.

**Draft (live)** — `Assistant.build` ([src/ff_helper/assistant.py](src/ff_helper/assistant.py)) is the single wiring point: snapshot → `PlayerRegistry.crosswalk` (name matching) → `rankings/blend.py` (one valuation per player) → `engine/replacement.py` (baselines from the league's real starting slots) → auction par values if applicable. `Assistant` then holds the fixed valuation model plus the live `DraftState`, and answers one question: given what's gone, who should I take?

The dataflow that matters:

```
yahoo/      OAuth, HTTP, and parse.py -- Yahoo's XML-derived JSON is quarantined here
rankings/   sources -> crosswalk -> blend -> snapshot
engine/     scoring -> replacement -> {vona.py (snake) | auction.py (auction) |
                                       weekly.py (in-season)}
            lineup.py sits *beside* replacement, not above it: all three engines
            import it, and none of them import each other
            winprob.py is its own layer under weekly.py -- score distributions
            and matchup odds, with no idea what a lineup slot is
draft/      state.py (the authoritative board), sync.py (poller), bridge.py, keepers.py
season/     cache.py (WeekSnapshot), valuation.py (WeeklyValuation) -- the in-season
            parallel to rankings/, kept separate on purpose
            news/ flags.py (the closed vocabulary), store.py (append-only JSONL),
                  extract.py (deterministic), server.py (paste box + userscript)
web/        FastAPI + one static page, no build step
backtest/   capture, calibration, counterfactual -- how engine changes get justified
offline.py  a parallel data layer that substitutes for yahoo/ entirely
```

### Load-bearing design decisions

**Value and timing never collapse into one number.** Projected points (re-scored under *your* league's modifiers, `engine/scoring.py`) answer "how good"; ADP answers "when will he be gone". `blend.py` deliberately discards a source's own points total, because it carries the exporter's scoring — so a projections CSV without per-stat columns is rejected rather than silently producing a 0.0 board.

**Snake and auction are different problems and share only the valuation layer.** `vona.py` prices pick scarcity (conditional survival, logistic ADP tails, a needs-to-picks plan DP); `auction.py` prices dollar scarcity (par values, live inflation, max bid as a hard constraint). Nothing above `replacement.py` is shared.

**What a player is worth and what he costs are separate, and so is "we don't know what he costs".** `auction.PriceBasis` holds three tiers of price — a source's published cost, `_estimate_markets`' interpolation between published costs, and his par scaled by what this room has actually been paying per dollar of par at his position — plus my own inflation-adjusted worth as the floor. Two accessors read them at two different evidence bars, because substituting my worth for an unknown price is circular in a way that only bites some consumers: `market_price` (published/interpolated only) backs claims about an *individual* — affordability, surplus, the plan-marginal score — while `estimate` takes all three and backs *budgeting*, where a number is required and refusing to guess just means guessing $1. The room tier fires whenever no *still-available* player carries a published cost — usual offline (Yahoo's `average_cost` is live-only, and an auction column in the projections CSV is the only other way one arrives), but a live league reaches it too once the last priced player is sold. It stays out of `market_price` because within a position it ranks players exactly as my own sheet does, and letting it declare a player unaffordable measurably lost points.

**A tie in the budget plan is the common case, not a coincidence.** `auction.py`'s break-even test compares two DP totals that share most of their terms, and whenever the plan's own choice for the slot a candidate would fill *is* that candidate, buying him and planning to buy him are the same basket — so the marginal is algebraically zero and only float noise picks its sign. Both comparisons in `plan_bid_for` therefore carry `_PLAN_EPSILON`. A strict `< 0` there returned `plan_bid = 0` for the best remaining player at any position whose dedicated slots were full, and since `rank_key` leads on `bid_to <= 0` he did not just look cheap, he fell off the short list: measured on the 2026 record, a back worth $51 with $34 still to spend ranked 214th.

**The board is the source of truth; the poller is just one of its writers.** `DraftState` accepts picks from the Yahoo poller, manual entry, and the draft-room bridge. Conflicts resolve toward Yahoo, but a superseded manual entry is reported, never silently overwritten.

**Failures are asymmetric, and the code takes sides.** An unresolvable *buyer* in an auction is refused outright (money charged to nobody inflates every remaining price); an unresolvable *player* only degrades toward stale. An unmatched keeper name is a hard error, not a skipped row. A name-match miss drops a player from every recommendation with no error, so `rankings/players.py` matches in explicit layers and everything unmatched is reported.

**Concurrency.** `sync.py` runs on its own thread and writes `DraftState` under `Assistant.lock`. Read the board under the lock, copy what you need, then run the pure engine functions outside it — `snake_recommendations` is the pattern to follow.

### In-season

A third phase, following the same fetch/act split: `scripts/weekly.py --fetch` writes a
`WeekSnapshot` and everything else runs from disk, which is what makes a digest
reproducible rather than dependent on when it happened to run.

**The week lives in the container, not in `SourceRow`.** A weekly projection *is* a
`SourceRow` whose `stats` are one game's worth, so the crosswalk and `engine/scoring.py`
are reused verbatim — but weekly rows are stored and loaded separately from season ones,
because `blend._combine` averages point totals and a Tuesday projection of 11 would drag a
season projection of 190 toward it. Different files, different loaders, mistake impossible.

**Every week's file is kept** (`week-<league>-<season>-wNN.json`). Week N's projections
sitting next to week N's realized stats is the only thing that makes weekly models
falsifiable, and it is the input to every backtest in this half of the app.

**`parse_roster` and `parse_roster_entries` are siblings, not one function.** The first is
premised on "before a draft, the only way a player sits on a team is if they were kept" —
which is what makes the keeper path safe — and that premise is false in week 6. The second
keeps `selected_position`, which the first discards.

**Per-season and per-game availability are different models.** `blend.availability_of`
correctly says a questionable tag costs nothing across seventeen games;
`season.valuation.play_probability` says it costs plenty across one. They share no
constant, deliberately.

**A source's own point total is still discarded.** `--trust-source-points K,DEF` is the
one opt-in exception, for the two positions with no scoreable stat IDs at all, and it
warns by name every time it fires.

**The lineup optimizer is exact, and that fixed a real bug.** `engine/lineup.py`'s
`optimal_lineup` solves the slot assignment with Hungarian on a provably bounded matrix
(only the top-K players per position can ever start). It replaced a greedy fill whose
docstring claimed to be "exact for every layout Yahoo actually offers" — it is not, and
`FLEX_ELIGIBILITY`'s `W/R` + `W/T` pair is the counterexample, in that module's docstring.
`backtest/counterfactual._lineup_points` now delegates to it, so recorded numbers can only
rise. Both leagues in this repo have a single flex, so they tie exactly; `tests/test_lineup.py`
asserts that tie over randomized rosters, which is what makes the swap behaviour-preserving.

**`start_sit` and `inactive_check` answer different questions.** One improves a total, the
other catches a zero, and they resolve on different clocks. The digest points the gameday
check at the *recommended* lineup rather than the current one — otherwise the two sections
can offer the same bench player twice and following both starts him in two slots.

### Win probability

**Two kinds of spread, never mixed.** `WeeklyValuation.points_stdev` is *source
disagreement* — model uncertainty, legitimately zero with one projection source. How much
a player's real score swings week to week is a different quantity that exists even when
every source agrees. `winprob.player_sigma` adds them in quadrature; treating either alone
as "the" variance is the mistake the split exists to prevent.

**`_WEEKLY_CV` is the weakest thing in the codebase.** Unfitted judgement, and meant to be
replaced: `fitted_cv()` reads `~/.ff-helper/cache/weekly-cv.json` when it exists and falls
back to the table, so re-fitting is a data file rather than a code change.

**Both sides of a matchup are simulated in one pass**, sharing game factors. Your receiver
and their quarterback in the same NFL game are correlated, and that is exactly the case
where independent sampling misleads most — a shootout lifts you both and moves the margin
far less than the totals. Two levels: a factor per game, plus a passing-offense factor per
team (QB/WR/TE only — a back in the same offense is closer to uncorrelated with his
quarterback, often negatively).

**Lognormal, not the gamma the plan called for.** A Gaussian copula onto a gamma needs an
inverse CDF with no dependency here, and adding scipy for it is a poor trade. Lognormal is
right-skewed, strictly positive, exactly moment-matchable, and takes the correlated normal
directly. Cost: a slightly heavy right tail, and a defense that scores negative cannot be
represented.

**Availability is zero-inflation, not a shrunken mean.** A doubtful player is a coin flip
on zero, not a small score, and the two have different shapes. `_conditional_mean` undoes
the availability multiplier already baked into `projected_points` so the absence is not
charged for twice.

**`WINPROB_EDGE` is the design decision, not the search.** An exact win-prob optimizer
would chase an optimization gap far smaller than the estimation error beneath it. The
threshold makes the tool say "start your projected-best lineup" almost every week and
speak up only when the underdog maths is large. Both lineups are always reported — showing
only the recommendation hides what it costs, and the trade is the entire content.

Only slot-for-bench swaps are searched: the team total is a sum over the *set* of starters,
so reordering them between slots produces an identical distribution.

### News

**Free text never moves a number directly.** `season/news/flags.py` is a closed vocabulary;
every multiplier is a literal in that file; anything the vocabulary has no word for becomes
a `Caveat` printed beside the recommendation rather than folded into it. Four invariants,
all tested: no multiplier is computed, same-family flags don't stack (most pessimistic
wins), the product is clamped to `[0.0, 1.5]`, and `RULED_OUT` is absorbing.

**Availability and role compose differently.** An article calling a player questionable says
the same thing Yahoo's injury designation says, so the two are reconciled by taking the more
pessimistic — multiplying would charge for one injury twice. Role and context flags say
something Yahoo doesn't, so they multiply on top. Hence `WeeklyValuation.pre_news_points`
and the split between `news_multiplier` and `effective_multiplier`: reporting the whole span
as "what the news did" would credit an article with a cut Yahoo had already made.

**Extraction is deliberately dumb.** Regex sentence split, capitalized n-grams through
`PlayerRegistry.find`, trigger match, done. The crosswalk's existing refusal to resolve
ambiguous initials (the Bijan/Brian Robinson case) is the load-bearing part — a wrong
attribution zeroes the wrong starter, confidently and invisibly. Negation and past-tense
windows demote a matched trigger to a caveat, which trades a little recall for correctness
in the direction that matters.

**`WEATHER_SEVERE` is defined but never fires from prose**, and that is a decision rather
than a gap: weather is a property of a *game*, the forecast sentence names no player, and
bridging that would need an "apply to anyone mentioned nearby" rule. It waits for a forecast
source keyed to the game.

**The news bridge does not touch the draft app.** `season/news/server.py` is its own small
FastAPI app on its own port. Draft day has to start the first time, every time, and
threading an in-season mode through `web/app.py` would put new failure modes in front of
the one hour of the year with no time to debug. Same origin-gate middleware pattern, and
the asymmetry from `draft/bridge.py` is deliberately inverted — nothing blocks here,
because a misattributed news line costs one bounded multiplier where a misattributed sale
corrupts every price in the room.

### Working in this codebase

- **Module docstrings carry the reasoning**, not just the summary. Read the top of a module before changing it; if you change a modeling decision, update the docstring's justification too.
- **Engine changes are gated on backtests.** Changing a model constant means re-running `scripts/backtest.py` on a real draft record and keeping the change only if Brier or the counterfactual roster improves. Every engine change in the history was justified that way.
- **New behavior gets tested against fixtures, not the network.** `tests/fixtures/` holds real Yahoo JSON/HTML variants; `tests/helpers.py` builds a full synthetic league for engine and web tests.
- **Snapshot format changes require bumping the version in `rankings/cache.py`** — old snapshots are refused so a missing field can't read as `None`.
- **`data/` is gitignored by default** (league configs name a real league; projection exports aren't ours to redistribute). Only `data/README.md`, `league-example.yaml`, and anonymized `data/drafts/*.json` are tracked. `MOCK-DRAFT.md` and `checklist.md` are local-only personal runbooks.

Known modeling gaps are listed under "Limitations" in [README.md](README.md) — uneven keeper counts, untracked auction nominations, unmodeled bonus stats, no third-round reversal.
