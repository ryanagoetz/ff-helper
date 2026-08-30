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
- `backtest.py --file record.json [--time] [--predictor mc] [--follow-from N] [--stop-after N] [--nominations]` — hits, calibration (Brier), counterfactual roster. `--nominations` grades the nomination model's predictions against the record (calibration only — see below). Auctions replay at recorded prices; `--follow-from` hands over to the policy mid-draft, `--stop-after` cuts the comparison at pick N and fills every policy's remaining slots the same way (including `actual`'s), for records where the human stopped deciding partway — autopick, walked away — and whose later buys are not a decision any policy can be graded against
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
            nomination.py is its own layer *above* auction.py -- who to put up,
            which is a question about other teams' money, not about my board
            winprob.py is its own layer under weekly.py -- score distributions
            and matchup odds, with no idea what a lineup slot is
draft/      state.py (the authoritative board), sync.py (poller), bridge.py, keepers.py
            state.board_key fingerprints the board; Assistant memoizes one
            whole-pool auction pass against it, shared by both auction panels
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

**The nomination list optimizes someone else's money, and cannot be backtested.**
`engine/nomination.py` is the only module here whose objective is not my own value, and it
sits *above* `auction.py` (the `winprob.py`-under-`weekly.py` relationship, not the
`vona.py`-beside-`auction.py` one) reading `AuctionRecommendation` rather than re-deriving
it. A nomination is a *timing* decision — everyone gets nominated eventually, you choose
against whose money — so the score is an expectation over two branches, both in dollars:
nobody bids and you own him, or someone bids and rival money leaves the room. Three
corrections were each necessary and each looked fine before measurement. `min_bid_marginal`
is *not* reliably positive (296 of 458 sub-$3 players on the real board are negative), so
"wasted slot" needs the `_slot_floor` baseline, which may itself be negative. Charging a player's *value* for nominating someone you
want is wrong by an order of magnitude — losing him costs the cascade to the next man — and
charging it collapsed the list onto $4 quarterbacks. And the separation from the buy list is
**weaker than it looks and bounded by the price source**: on an unpriced board the room tier
is your own par reordered by a positional premium, so the two lists rank one signal --
measured, top-20-by-price and top-20-by-value agree 14.9/20 and the panel overlaps the buy
list 3.9/6. Suppression is positional and continuous (`_plan_pressure`), never a per-player
cut, because competing backs are substitutes for one slot and a cut on adjacent floats gave
two identical receivers opposite advice. What the panel independently knows is *who can
bid*.
The backtest cannot grade any of this — no nominator in the record, prices frozen by design
— so `backtest/nominations.py` grades predictions against null baselines and says so in its
own output. The `auction.py` change that supports it (`budget_price`, `price_basis`,
`min_bid_marginal`) is inert: the gate it passed was byte-identity of the existing table.

**One engine pass per board state, keyed by a fingerprint of the board itself.** Both
auction panels need the *same* board priced and the page asks for both every 2 seconds, so
`Assistant._priced_board` memoizes a whole-pool `recommend_auction` against
`DraftState.board_key` — 30.9 ms per poll when a sale has landed, 2.6 ms when it has not,
on a 524-player board. `limit` only slices at the end of the engine, so one list serves
every caller.

The key is **derived, not maintained**, and the counter it replaced is worth remembering
because it failed in both directions at once. It over-fired: `apply_sync` bumped whether or
not a pick was new and called `drop_player` (another bump) per pick, while the poller
re-sends Yahoo's whole result list every 2 s — so a poll reporting *nothing* moved it by 101
on a 100-sale board and the cache never survived a poll, which is the only configuration
that mattered. And it under-fired: `roster_size`, `teams` and the `draft_status` setter feed
engine inputs from outside the discipline, and the test guarding the discipline was circular
— it built its expected set *from* the methods that already called `_touch`, so a new
mutator that forgot was absent from both sides and the assertion passed. A fingerprint has
neither hole and costs 0.044 ms against a 30 ms pass. `tests/test_draft.py::TestBoardKey`
pins both directions, including that a no-op poll does not move it.

Computing is guarded by a separate `_engine_lock`, never `Assistant.lock` — holding the
board lock across the engine is exactly what this repo forbids, since the poller would block
behind it. Callers that need more than the board (the nomination list needs rival budgets)
pass an `alongside` callable so their extra reads happen inside the *same* lock hold that
fingerprinted it; reading them separately let the two straddle a sale.

**The board is the source of truth; the poller is just one of its writers.** `DraftState` accepts picks from the Yahoo poller, manual entry, and the draft-room bridge. Conflicts resolve toward Yahoo, but a superseded manual entry is reported, never silently overwritten.

**A keeper costs either a roster spot or a pick, and the two need different maths.**
`apply_keepers`' collapsed `rounds` is right for the first: kept players shorten the draft
and the dense snake still describes it. For the second -- the kept player is slotted into
the board at the round he cost, live picks flow around him -- no round count can help, and
the error is not small. One keeper ahead of me in round 1 shifts every later pick by one
and the offsets never resynchronise. Replaying the **actual 2025 Shiva draft**
(`data/drafts/2025-shiva-snake.json`: 150 board positions, 26 keepers slotted at the rounds
they cost, 124 live picks) the dense mapping named the right team for **11 of 124 picks**
and knew it was my turn at **1 of my 14** -- that league is the bad case for it, since my
team kept one player where most kept three, so I drafted 14 times against their 12 and the
dense model put my last turn past the end of the draft. `DraftState.pick_schedule` builds
the exact `(slot, round)` sequence when every keeper carries a round, and `my_picks` /
`_slot_for_pick` / `round_for_pick` read it: **122/124** and 14/14.

The two it still misses are the honest ceiling, not a bug. Yahoo's record shows two teams
**swapped** their round-3 and round-15 picks, so the 2025 board is not a snake at all and
no formula over `(round, slot)` can express it. Traded picks would need the real pick order
as *data* rather than a derivation. Worth knowing before trusting `team_for_pick` in a
league that trades picks -- and worth knowing that the posted board, not the snake, is the
authority when the two disagree.

Three things about that property are deliberate. It returns **None for the ordinary
league** -- no keepers, or keepers with no rounds -- because there the dense snake *is* the
schedule, and a note nagging every Yahoo keeper league to add a column would be noise; only
a *partly* filled column is loud, since that file is inconsistent rather than describing a
spot-cost league. It refuses **all-or-nothing**, for `keepers.load_csv`'s reason: a
schedule built from six of nine keepers is wrong at every pick after the first missing one
and wrong quietly, where the dense fallback is at least wrong in a documented way. And it
is **keyed by a fingerprint, not invalidated by writers** -- the `board_key` lesson, since
`roster_size`, `teams` and `apply_keepers` all feed it from different places.

The gate this passed was *not* Brier, and the attempt is worth recording so nobody repeats
it. Replayed on the real 2025 board the delta was **+0.00008** -- very slightly *worse* --
over 5972 predictions, with the counterfactual lineup **identical to the tenth of a point**;
on a generated draft over the 2026 board it was -0.00001 (sd 0.00012, better on 4 of 8
seeds) with the lineup -6.3 against a per-seed spread of ±135. All of that is noise, and it
is noise *structurally*, for two compounding reasons. `survival_calibration` takes its scoring
windows from the record's own picks, so both arms are handed the correct turn boundaries
for free and the harness cannot see the defect at all. And no snapshot older than 2026
survives, so a 2025 replay prices a 2025 draft with 2026 valuations -- 2025's last-round
keepers Nacua, Achane and Smith-Njigba are 2026 first-rounders -- which makes the absolute
Brier (0.017) a measure of cross-season value drift more than of calibration.

What it passed instead is the `budget_price` gate -- byte-identity of the existing auction
backtest -- plus ground truth on pick ownership, which is what
`tests/test_keepers.py::TestKeepersThatCostAPick` pins, including a test asserting the
dense fallback really is wrong so the premise cannot rot. **A correctness fix to a mapping
is not a model constant, and Brier is the wrong instrument for it**; do not read the
neutral number above as evidence for or against.

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
