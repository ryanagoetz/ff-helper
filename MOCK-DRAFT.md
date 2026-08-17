# Mock draft: what to do

You have two separate jobs at the mock, and they are easy to confuse because only one of
them involves the app.

| | What | How long | Why it matters |
|---|---|---|---|
| **Job A** | Copy the sold list out of Yahoo and send it to me | 5 min | Nobody has seen Yahoo's 2026 draft room. Until I see the real text, the paste parser is a guess. |
| **Job B** | Practise entering sales in ff-helper | 20 min | On Saturday every sale is typed by you. Learn the flow before it is the only flow. |

**The mock is not your league.** ff-helper is configured for Bust A Move — 12 teams, your
scoring, your team names. Do not try to make it track the mock draft; it will not match
and that is fine. Job A needs no app at all, and Job B uses your real config with made-up
sales.

---

## Job A — capture what Yahoo's copy actually produces

This is the one that unblocks everything else. Five minutes.

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

While you are in the room, also note:

- the **URL of the draft page** (the whole thing) — the automated reader needs the exact
  origin, and the draft client may live on a different subdomain than the league;
- whether team names appear **in full**, or truncated with an ellipsis. This matters most
  for `Rx...` — if the room shows something different from your config, buyer matching
  fails and it fails on *your* team, which is the one that sets your max bid.

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

This is the path that has to be automatic on Saturday. Never touch the mouse:

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

### Practise the paste path

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
