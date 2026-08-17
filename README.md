# ff-helper

A live draft assistant for Yahoo Fantasy Football. It watches your draft as it happens and,
when you're on the clock, tells you who to take — and why.

Runs entirely on your own machine. Read-only: it never writes to your league.

---

Supports **both snake and auction** leagues. It reads the draft type from your league
settings and switches models automatically — you don't configure anything.

## What it actually does

The hard part of a draft isn't knowing who's good. It's knowing what it costs you to wait —
and what "waiting" means depends entirely on the draft format.

**In a snake draft the scarce resource is picks.** From slot 5 in a 12-team league you pick
at 5 and 20; fourteen players disappear in between. If four comparable receivers will
survive that gap but the last good running back won't, you take the running back — even if
the receiver grades higher.

**In an auction the scarce resource is dollars.** Nobody is ever unavailable, only
unaffordable, so pick-scarcity vanishes entirely. The questions become: what is he worth in
dollars, what will the room actually pay, and what can I still afford?

These are genuinely different problems, so they get different engines — sharing the
valuation layer underneath but nothing above it.

### Three signals, kept separate

Most cheat sheets collapse everything into one ranked list, which is exactly why they can't
answer the question above. This keeps them apart:

**Value — how good is he?**
Projected fantasy points, re-scored under *your* league's stat modifiers pulled from the
Yahoo API. Not "projected points" from some site that assumed half-PPR when you play full
PPR. That becomes **VOR** (Value Over Replacement): points above the last startable player
at his position, which is what makes a quarterback's 320 and a running back's 210
comparable at all.

Replacement level is derived by greedily filling your league's real starting lineups, so
flex slots land on whichever position actually wins them instead of being split by a rule
of thumb.

**Timing — when will he be gone?**
ADP, with a standard deviation. This says nothing about quality; it's what the room
believes. Yahoo's own ADP is weighted heaviest (0.65) because you're drafting in a Yahoo
room against people looking at Yahoo's rankings.

**Scarcity — what does waiting cost?**
This is the part that differs by format.

*Snake:* **VONA** (Value Over Next Available):

```
VONA(player) = VOR(player) − E[VOR of best player at his position at my next pick]
```

Draft position is modelled as a **logistic** distribution rather than normal. Real ADP has
fat tails — players slide on injury news, get reached for on hype — and a Gaussian makes
someone sitting 20 picks past his ADP mathematically impossible. Survival is also
conditioned on the player being available *right now*, which is what makes the model
usable on exactly the fallers you most want advice about.

*Auction:* **surplus** — the same idea in dollars.

```
surplus(player) = what he's worth to you − what the room will pay
```

VOR converts to dollars once you know how much money exists and how much value it's
chasing. Yahoo's `average_cost` is the market price — the auction analog of ADP, and just
as separate from value.

The live part is **inflation**. Par values are computed once, but money leaves the room
unevenly. If the league blows its budget on early studs, the dollars left chasing everyone
else shrink and the survivors get cheap; if the room is thrifty early, they inflate. In
testing, after twelve teams each spent $150 on a stud, the price level dropped to **28% of
par** — a $50 player became a $15 player. Recomputing that after every sale is the single
biggest edge available, because most of the room is still reading a sheet printed before
the draft started.

Your **max bid** is a hard constraint, not advice: budget remaining minus $1 for every
roster spot you still have to fill. Players going for more than that are ranked below
everyone you can actually win, because you can't have them.

Every recommendation comes with a one-line reason, because you're the one making the pick
and you'll want to overrule it sometimes.

---

## Setup

Five steps, once. Budget twenty minutes of your own time — but start step 1 well before
draft day, because how long Yahoo takes to approve you is not up to either of us.

### 1. Apply for Fantasy Sports API access

**Do this first, and do it early — it is an approval process, not a checkbox, and nothing
else in this README works until it clears.**

Apply at **https://sports.yahoo.com/developer/access/**. The form asks what you are
building, what data you need, and how many users you expect; Yahoo warns that thin
submissions are closed without reply, so describe the app properly. Access is read-only,
which is all this app wants anyway.

Yahoo publishes no approval timeline, so treat it as the long pole in your setup.

### 2. Register a Yahoo app

1. Go to **https://developer.yahoo.com/apps/create/**
2. **Application Name:** `ff-helper`
3. **Application Type:** *Confidential Client*
4. **Redirect URI:** `https://localhost:8000/callback`
5. **API Permissions:** leave everything unticked. Fantasy Sports is not offered here any
   more — the list is down to OpenID Connect and TW Auction, neither of which you want.
   Fantasy access arrives through the approval above, attached to the app itself.
6. Copy the **Client ID** and **Client Secret**. The access application has an optional
   Client ID field, so register the app first and give Yahoo the ID.

**If you have not been approved, sign-in still succeeds and then every API call fails**
with 401 `oauth_problem="additional_authorization_required"`. That error says nothing
about approval, so if you see it, this step is why.

### 3. Install and configure

```bash
git clone https://github.com/ryanagoetz/ff-helper
cd ff-helper
uv sync                    # or: pip install -e .
```

Then create a `.env` in the project root with your Client ID and Secret. It is gitignored:

```
YAHOO_CLIENT_ID=
YAHOO_CLIENT_SECRET=

# Must exactly match the Redirect URI registered on your Yahoo app. Use "oob" to fall
# back to manual code paste if Yahoo rejects the localhost callback.
YAHOO_REDIRECT_URI=https://localhost:8000/callback

# Your league, e.g. 461.l.123456 -- setup_auth.py prints your leagues. Optional; it is
# only the default for when you do not pass --league.
FF_LEAGUE_KEY=

# Seconds between draft-result polls during a live draft.
FF_POLL_INTERVAL=2.0

# Auction leagues only. Leave blank to use whatever Yahoo reports, or 200 if it reports
# nothing. Set this if your league's budget is not $200 -- every dollar value depends on it.
FF_AUCTION_BUDGET=

# OAuth scope requested at sign-in. Leave blank: Yahoo grants fantasy access to an
# approved app, and asking for a scope the app lacks fails with invalid_scope.
FF_OAUTH_SCOPE=
```

### 4. Sign in

```bash
uv run python scripts/setup_auth.py
```

Open the URL it prints, approve access, and your browser will land on a page that **fails
to load** — that's expected. Copy the whole URL from the address bar and paste it back.
The authorization code is in it.

> This paste flow is deliberate. Yahoo requires an HTTPS redirect URI, and running a local
> HTTPS callback server means generating and trusting a self-signed certificate — far more
> failure modes than is worth it for something you do once. Pasting works whether your app
> is registered with `https://localhost/...` or `oob`, so either way you're covered.

The script then prints your leagues. Paste the right `FF_LEAGUE_KEY` into `.env`.

### 5. Build the ranking snapshot

```bash
uv run python scripts/fetch_rankings.py --projections 4for4-projections.csv
```

This pulls the Yahoo player pool, FantasyFootballCalculator ADP, and FantasyPros consensus
rankings, reads projections from the CSV, then caches everything to `~/.ff-helper/cache/`.
**Re-run it the day before your draft** so draft morning doesn't depend on the network.

**Projections come from a CSV export, not a scrape.** FantasyPros put full projections
behind a registration fence and now serves ten rows per position to signed-out callers —
enough to parse, nowhere near enough to derive replacement level from, which is exactly
the kind of quiet wrongness that ends up in a recommendation. Export projections from
whatever you subscribe to (4for4, or anything that emits a CSV) and pass the file.

Only a `player` column is required; positions, teams, and extra columns are optional and
extras are ignored. Column names are flexible — `Name`/`Rushing Yards`/`Receptions` work
as well as `player`/`rush_yds`/`rec`.

```csv
player,pos,team,pass_yds,pass_td,int,rush_yds,rush_td,rec,rec_yds,rec_td,fum_lost
Ja'Marr Chase,WR,CIN,0,0,0,4,0,112,1408,12,1
```

**Per-stat columns are required.** The app scores stat lines under your league's
modifiers, and deliberately discards a source's own points total — that total carries the
exporter's scoring, which is the thing being replaced. So export *projections*, not a
rankings table: a file with a points column and no stats is rejected, because it would
otherwise produce a board where every player is worth 0.0 and ranking quietly falls back
to ADP.

Put exports in `data/`, named for the league they belong to, and no flag is needed:

```
data/projections-461.l.111111.csv     # snake league
data/projections-461.l.222222.csv     # auction league
```

`data/projections.csv` is the fallback when no league-specific file exists.

**One per-stat file serves every league**, because scoring happens here rather than at
export time — so `data/projections.csv` is the normal case and the per-league names are
only needed if you have a reason to give a league its own file. `fetch_rankings.py`
records which file it used in the snapshot notes.

Everything in `data/` is gitignored except its README — a subscriber export is not yours
to redistribute.

Without a projections file the FantasyPros scrape is still attempted, and fails loudly
when it gets the teaser rather than caching ten players as though they were a draft board.

Read the coverage report it prints. A player who fails to match across sources is silently
absent from every recommendation and you'd never notice — so the script names everyone who
didn't match and exits non-zero if top-200 coverage drops below 90%.

---

## No API access? Run it offline

Yahoo's approval does not arrive on a schedule that cares about your draft date. Offline
mode runs the whole app without the API: you describe the league in a YAML file, the
player pool comes from your projections export, and picks are typed in.

```bash
cp data/league-bustamove.yaml data/league-mine.yaml   # then edit it
uv run python scripts/fetch_rankings.py --offline data/league-mine.yaml
uv run ff-helper --offline data/league-mine.yaml
```

The config carries what the API would otherwise supply — roster slots, scoring, team
names, which team is yours, and the auction budget. Scoring keys are the same names the
projections CSV uses, and a category the engine cannot score is reported rather than
dropped in silence.

Everything above the data layer is unchanged: scoring, replacement level, VOR, VONA,
auction par values, and inflation are the same code that runs online. What you lose:

- **Yahoo's own ADP**, normally weighted 0.65 because it describes drafts on the platform
  you actually draft on. You are left with your export's ADP and FFC's.
- **Live draft sync.** Every pick is entered by hand — the manual path that exists for
  feed stalls becomes the only path. The sync indicator reads as absent rather than green.
- **Automatic keepers**, since pre-draft rosters come from the API. Use `--keepers`.
- **Market prices in auctions**, unless your export has them. You still get what a player
  is worth; you lose whether the room will overpay.

Team defenses are not in projection exports, so they are seeded from FFC and FantasyPros
and ranked by consensus — otherwise a league with a DEF slot would have a roster spot the
app could never fill.

---

## Draft day

```bash
uv run ff-helper
```

Opens at `http://127.0.0.1:8777`. Put it beside the Yahoo draft client. The page switches
itself to snake or auction mode based on your league.

**Snake mode**
- **Recommended pick** — the call, with VOR, VONA, ADP, survival odds, and the reasoning.
- **Alternatives** — the next seven, same numbers, so you can overrule with your eyes open.
- **Mark a player drafted** — type a name to record a pick by hand.

**Auction mode**
- **Budget bar** — your money left, max bid, spots to fill, money still in the room, and
  the live price level versus par.
- **Best value on the board** — worth to you, what the room pays, your edge, and the most
  you should bid.
- **Mark a player sold** — name, **price**, and **buyer**. All three are required: without
  the price the budget is wrong, and without the buyer the money leaves nobody's budget and
  every remaining player gets over-valued.

**Both**
- **Sync indicator** — green means the feed is live, amber means it's lagging, red means
  stop trusting it.

### Keepers

**Pulled from Yahoo automatically — nothing to configure.** Before a draft starts, the only
way a player is sitting on a roster is if they were kept, so pre-draft rosters *are* the
keeper list. The app reads them at startup and keepers then:

- drop out of the player pool, so they're never recommended to you;
- count toward your roster needs, so it won't push a position you're already full at;
- **spend their salary** in auction leagues, which shrinks both your max bid and the money
  in the room (and therefore moves inflation) — when Yahoo publishes a salary, see the CSV
  note below when it doesn't;
- **shorten the draft** — 15 spots with 2 keepers each is a 13-round draft.

Rosters are read on every start, not just before the draft opens, so restarting mid-draft
keeps them. A player the board already shows as drafted is counted as a pick, not a keeper,
so nothing is charged twice.

Your keepers are listed at the top of the roster panel so you can confirm the app actually
knows about them. If that list is empty when it shouldn't be, stop and fix it before
drafting.

**If Yahoo's answer is wrong or missing, override it with a CSV:**

```bash
uv run ff-helper --keepers my-keepers.csv
```

```csv
player,team,cost,round
Ja'Marr Chase,Team Ryan,55,2
Kenneth Walker III,Rival Squad,,4
```

`team` matches a Yahoo team name or key. `cost` matters for auctions; both it and `round`
are optional. Column names are flexible (`name`/`owner`/`salary` all work).

`round` is recorded but does not yet feed the pick maths — forfeited picks are not
modelled, so a keeper-snake league that charges a round still gets uniform pick numbers.

**In an auction, supply `cost`.** Yahoo often publishes no keeper salary, and a salary the
app does not know is spent as $0 — which leaves that money apparently in the room and
inflates every price it recommends. The app warns loudly when this happens, but the CSV is
the fix.

A name that can't be matched is a **hard error**, not a skipped row — a half-loaded keeper
file leaves players in the pool who aren't really available, which is worse than no file at
all.

### Running more than one league

Snapshots are cached per league, so nothing collides. Build one snapshot per league, then
point the app at whichever you're drafting:

```bash
uv run python scripts/fetch_rankings.py --league 461.l.111111   # snake league
uv run python scripts/fetch_rankings.py --league 461.l.222222   # auction league

uv run ff-helper --league 461.l.111111
uv run ff-helper --league 461.l.222222 --port 8778       # both at once, if drafts overlap
```

`FF_LEAGUE_KEY` in `.env` is just the default when you don't pass `--league`.

### One setting worth checking for auctions

Yahoo is inconsistent about publishing the auction budget. The app tries several field
names and falls back to $200. **If your league isn't $200, set `FF_AUCTION_BUDGET` in
`.env`** — every dollar value scales off it.

### About that manual override

Yahoo's `draftresults` endpoint is polled, not pushed, and how promptly it reflects an
in-progress draft isn't something anyone can guarantee. So the board is the source of
truth and the poller is just one of its writers. If the feed stalls, type picks in and keep
going.

If Yahoo later reports a pick you'd entered differently, Yahoo wins — it's the system of
record — but you get told, rather than the board quietly rewriting itself underneath you.

---

## Check it before you trust it

### Start here: the preflight check

```bash
uv run python scripts/doctor.py --all
```

Run this **first**, before the snapshot or anything else. It verifies sign-in, reads every
league, and reports the handful of things that can only be confirmed against real data:
whether Yahoo publishes your auction budget, whether keeper salaries come through, whether
pre-draft rosters are populated, and whether ADP and `average_cost` are actually there.

It ends with a numbered list of what to fix, and exits non-zero if anything needs
attention. Everything else in this section assumes it came back clean.

### Then backtest the model

```bash
uv run python scripts/replay.py
```

Replays a completed draft pick by pick and shows what the engine *would* have recommended
at each of your turns. Point it at your league's prior season with `--league <key>` and it
becomes a real backtest rather than a smoke test. If it keeps wanting players who actually
went 40 picks later, the survival model is miscalibrated — better to learn that in August.

### Then run a live mock draft

**This is the one test that matters.** Join a Yahoo mock draft and run the app against it —
it exercises polling latency, the recommendation loop, and the UI under a real clock, none
of which any offline test can reach. Worth doing twice, and worth doing more than a day out.

The single thing to watch: **does the sync indicator stay green as picks land?** Yahoo's
`draftresults` endpoint is polled, not pushed, and how promptly it reflects a live draft is
the one assumption in this app that was never verifiable offline. If it lags, the manual
override is already there — but you want to discover that in a mock, not in your draft.

```bash
uv run pytest        # 212 tests, no network required
uv run ruff check .
```

---

## How it's put together

```
src/ff_helper/
  yahoo/      OAuth, HTTP client, and parse.py -- which quarantines Yahoo's
              XML-derived JSON so no other module has to know about it
  rankings/   sources (Yahoo ADP, FantasyPros, FFC), the player crosswalk,
              blending, and the on-disk snapshot
  engine/     scoring, replacement level, and the VONA model
  draft/      the authoritative board and the background poller
  web/        FastAPI endpoints and one static page
```

Python 3.11+, FastAPI, and a single HTML file with no build step — because on draft day the
app has to start the first time, every time.

### Two things worth knowing

**Yahoo's JSON is genuinely hostile.** Collections are dicts keyed by stringified integers;
single objects are lists of fragments; a multi-position player serializes differently from
a single-position one. All of it is confined to `yahoo/parse.py` and tested against
fixtures covering each variant.

**Name matching is where this quietly breaks.** "Kenneth Walker III" / "Ken Walker III" /
"K. Walker" are one player, and a miss drops him from every recommendation without any
error. Matching runs in layers — exact, first-initial, then surname plus a compatible first
name — and anything unmatched is reported rather than swallowed.

---

## Limitations

- **Uneven keeper counts** (teams keeping different numbers) make *other teams'* pick
  numbers approximate, because the number of picks per team stops being uniform. Your own
  turns are computed from your own keeper count, so they stay exact; what can drift is the
  countdown to a rival's pick. The app says so when it detects this. This stays approximate
  for the whole draft — nothing reconstructs the real pick order from the live feed.
- **Auction budgets** default to $200 when Yahoo doesn't publish one — override with
  `FF_AUCTION_BUDGET` if your league differs.
- **Auction nominations** aren't tracked. Yahoo's API reports completed sales, not who is
  currently on the block, so the app tells you what everyone is worth rather than reacting
  to the live nomination.
- **Kickers and defenses** have no stat projections, so their values are interpolated from
  consensus rank. Fine — you shouldn't be thinking hard about them anyway.
- **Yahoo bonus stats** (long-TD bonuses and similar) aren't scored.
- **Third-round reversal** isn't handled.

---

ADP data courtesy of [Fantasy Football Calculator](https://fantasyfootballcalculator.com).
Consensus rankings and projections from [FantasyPros](https://www.fantasypros.com).
