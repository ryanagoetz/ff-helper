# Mock draft: what to do

## First, how the pieces fit

Three ways a sale can reach the board, in increasing order of doing the work for you:

1. **Typing** one sale at a time. Always works, needs nothing, and is the fallback for
   everything else. Be fluent in it regardless.
2. **Pasting** Yahoo's whole sold list into ff-helper. Proven against text copied out of a
   real draft room, so this is now the expected way to work on Saturday: every few
   minutes, copy and paste, and it catches up everything — including sales you missed
   while bidding, which typing cannot recover.
3. **The reader** — a Tampermonkey script that does the pasting for you every few seconds.
   Written, but never yet run against a live room. That is what the mock is for.

Everything from here is about moving up that list with evidence, and never losing the step
below it.

## The two jobs

| | What | How long | Why |
|---|---|---|---|
| **Job A** | Copy the sold list out of Yahoo and send it to me | 5 min | Done — the parser now reads Yahoo's real format. Repeat only if the room looks different. |
| **Job B** | Practise typing sales in ff-helper | 20 min | The path that works regardless. |
| **Job C** | Run the bridge against the mock | 20 min | Proves the whole automated path before it matters. |

**The mock is not your league.** ff-helper is configured for Bust A Move — 12 teams, your
scoring, your team names. Do not try to make it track the mock draft; it will not match
and that is fine. Job A needs no app at all, and Job B uses your real config with made-up
sales.

---

## Job A — capture what Yahoo's copy produces (already done once)

The sample you sent has been turned into a parser and a test fixture, so this only needs
repeating if the live room looks different from the results panel you copied. If it does,
follow this again and send the new text.

1. Go to **https://football.fantasysports.yahoo.com** and find the mock draft lobby.
   **Join an auction mock if one is offered.** If only snake mocks are available, join one
   anyway — the results panel is still worth seeing, it just will not have prices.
2. Wait for a handful of players to be drafted. Ten or so is plenty.
3. Find the panel listing completed picks — usually "Draft Results", "Sold", or the
   right-hand pick log.
4. **Select that list and copy it** (click-drag over it, or click inside and Ctrl+A, then
   Ctrl+C).
5. Paste it into a message to me, exactly as it came out. Do not tidy it up — the messy
   version is the useful one.

If select-and-copy does not work on that panel (some JavaScript apps block it), say so
and take a screenshot of the panel instead. There are other ways in; that just tells me
which one we need.

**What I need from the text:** the player name, the price, and the buying team, and how
they are separated. Everything else follows from that.

While you are in the room, also note whether team names appear **in full** or truncated.
This matters most for `Rx...` — if the room shows something different from your config,
buyer matching fails, and it fails on *your* team, the one that sets your max bid. There
is a `team_aliases:` block in the config for exactly that.

The draft page URL is already known: the client is on
`https://football.fantasysports.yahoo.com/draftclient/...`, the same origin as the rest of
Yahoo Fantasy. Its `?auth=` parameter is a session credential — do not paste it anywhere.

---

## Job B — practise the entry flow

Use your real config and invent the sales. The board resets every time you restart, so
nothing you do here can spoil draft day.

### Start it

```bash
cd ~/ff-helper && uv run ff-helper --offline data/league-bustamove.yaml
```

It opens at **http://127.0.0.1:8777**. You should see `Bust A Move`, `$200 BUDGET LEFT`,
`$186 MAX BID`, and Jahmyr Gibbs at the top of the board.

If you get `address already in use`, it is **already running** from earlier — just open
http://127.0.0.1:8777 in a browser. Note the page is served from disk, so a plain refresh
picks up any changes without restarting anything.

### Practise typing sales — the keyboard flow

Drill this whatever happens with Job A. If pasting works you will still type corrections;
if it does not, this is the whole draft. Never touch the mouse:

```
type a name → Enter → type the price → Enter → choose the buyer → Enter
```

- **Arrow keys** move through the matching players.
- **Esc** abandons a half-entered sale.
- The buyer box starts blank on purpose and must be chosen every time. Defaulting to the
  last buyer would be wrong more often than right, and a sale charged to the wrong team
  corrupts that team's budget and every price the app quotes afterwards.

Do fifteen or twenty. Watch **Money in room** and **Price level** move. The thing to build
is the reflex, not the understanding — you want this taking ten seconds while you are
listening to the next nomination.

### See how the paste path behaves

Not a substitute for the typing drill — this is so the behaviour is familiar if Job A
comes back clean and pasting becomes your main path on Saturday.

Paste this into the **Paste the draft room** box and click *Apply pasted board*:

```
Jahmyr Gibbs	$62	B-U-T(t)-S Butts Butts Butts
Ja'Marr Chase	$55	3 QB's
Bijan Robinson	$48	Rx...
```

Expect `read 3, 3 new.` and the money in the room to drop by $165.

Then click *Apply pasted board* **again without changing anything**. Expect
`read 3, 0 new, 3 unchanged.` and no change to the money. That is the important
behaviour: sales are matched by player, so **re-pasting the whole board is always safe**.
Send everything every time; never try to paste only the new rows.

Now break it on purpose, so you recognise it on Saturday:

- Change a buyer to a name that is not in your league. The whole paste is refused, the
  message names the team, and the board does not move.
- Delete two of the three lines. It refuses that too — a board that lost sales almost
  always means the copy failed, and applying it would hand money back to teams that
  already spent it.

### Reset

Stop the app with **Ctrl+C** and start it again. The board is empty. Nothing is written to
disk.

---

## Job C — run the bridge against a mock

Six steps. **Run one app at a time, always on port 8777**, so the userscript never needs
editing.

### 0. Once, before the mock: put the token in the script

```bash
uv run python -c "import sys;sys.path.insert(0,'src');from ff_helper.config import bridge_token;print(bridge_token())"
```

Open the Tampermonkey script and set `TOKEN` to that value. It does not change, so this is
a one-time step. Leave `PORT` at 8777.

### 1. Join a mock auction and let a few players sell

Five or six is enough.

### 2. Copy the draft-results panel

Click in it, Ctrl+A, Ctrl+C.

### 3. Build a league that matches the room

```bash
uv run python scripts/mock_config.py --num-teams 12
```

It waits for you to paste. Paste, then press **Ctrl+D**. It reads the buyers out of the
text and writes `data/league-mock.yaml`, borrowing your real player pool so there is
nothing to download. Check `Your Team` is marked as you.

### 4. Stop the Bust A Move app, start the mock one

```bash
uv run ff-helper --offline data/league-mock.yaml --bridge
```

Opens on 8777 as usual. `--bridge` is what lets the draft room talk to it.

### 5. Reload the draft room tab

The userscript runs on load. A badge appears bottom-right:

```
ff-helper: 16 read, 16 new · 14:32
```

That is it working. Leave it; it re-reads every four seconds and only posts when something
changed. Click the badge to pause.

### What each badge colour means

| Badge | Meaning |
|---|---|
| green, counts rising | working |
| `no sales on screen yet` | nothing sold, or the panel is not on screen — scroll it into view |
| `cannot reach ff-helper` | the app is not running, or was started without `--bridge` |
| `REFUSED — ... buyer ...` | a team joined or renamed after step 3. Redo steps 2–4. |
| amber, `n skipped` | some sales did not match a player; the rest went in. Check the console. |

### Then check it actually agrees with the room

Open http://127.0.0.1:8777 and compare against Yahoo: the players sold should be gone from
the board, money in the room should equal $200 × teams minus everything spent, and your own
budget should match what "Your Team" has spent. **That comparison is the real test** — a
green badge only means text was accepted, not that it was read correctly.

### When you are done

Ctrl+C the mock app. Nothing was written to disk, and `data/league-mock.yaml` is throwaway.
Start the real one again with:

```bash
uv run ff-helper --offline data/league-bustamove.yaml --bridge
```

---

## What to send me afterwards

1. The copied text from Job A, raw.
2. The draft room URL.
3. Anything in Job B that felt slow, wrong, or surprising.

With those I can finish the parser against the real format and, if the copy path looks
solid, build the automated reader that does this without you pasting.

---

## If you only have five minutes

Do **Job A**. The parser cannot be finished without it, and the entry flow can be
practised any time.
