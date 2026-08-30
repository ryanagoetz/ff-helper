# data/

Projection exports, keeper files, and league configs live here. **The directory is
gitignored by default** — a subscriber export is not ours to redistribute, and a league
config carries a real league ID and your leaguemates' team names. Only three things are
allowed through, because they are generic or anonymized: this README,
`league-example.yaml`, and the draft dumps under `drafts/`.

Anything you add here stays local unless you add an explicit `!` rule to `.gitignore`.

## The normal case: one file

`data/projections.csv` serves every league. Scoring happens in the app, under each
league's own modifiers, so the same stat lines are correct for a snake league and an
auction league at once:

```bash
uv run python scripts/fetch_rankings.py --league 461.l.111111
uv run python scripts/fetch_rankings.py --league 461.l.222222
```

## Export projections, not rankings

**The file must carry per-stat columns** — passing yards, receptions, rushing TDs, and so
on. A rankings table with a points total and no stats is rejected on load.

That is not fussiness. `rankings/blend.py` discards a source's own points total on
purpose, because the total was computed under the exporter's scoring rather than yours.
A points-only file therefore contributes nothing: every player falls through to
interpolation, every position reports "no stat projections available", and the board
comes back 0.0 with ranking silently reverting to ADP.

Providers usually offer both reports. From 4for4, the projections export is the one with
`Pass Yds` / `Rec` / `Rush TD` columns, not the one with `FF Pts` and `VOR`.

## Giving a league its own file

Only needed if a league should use different projections:

```
data/projections-461.l.111111.csv
```

A league-specific file wins over `projections.csv`, and a league without one does not
borrow another league's.

## Weekly projections (in-season)

The season export above is for the draft. In-season, the same subscription's **weekly**
export goes under `data/weekly/`, one file per week:

```
data/weekly/weekly-461.l.111111-w07.csv    # this league
data/weekly/weekly-w07.csv                 # any league
```

```bash
uv run python scripts/weekly.py --fetch --week 7
uv run python scripts/weekly.py                    # read it back, no network
```

Same rule as above, and it matters more here: **per-stat columns are required**, because a
start-or-sit call turns on smaller margins than a draft pick does. An `Opp` column is used
when present (`@KC` and `vs. KC` both parse, and the home/away flag is left unset rather
than guessed when the file does not say). A `Week` column is filtered on when present; when
it is absent, the filename is the only record of which week the file is, which is why the
week is part of the name.

Unlike the season file, a row with no stats and no points is **counted and skipped** rather
than failing the load. In a weekly export that is what a bye looks like, and failing over
thirty of them in week 9 would make the tool unusable. The count is reported in the notes.

## Kickers and defenses

Kicker rows come through with zeroes across every scoreable column, because `FG` and `XP`
have no Yahoo stat IDs the engine can score. Those rows are treated as unprojected and
ranked by consensus instead — which is the intended behaviour, not a gap. Defenses are
usually absent from projection exports entirely and get the same treatment.

In-season there is no consensus rank to fall back on, so a kicker or defense simply has no
weekly value and is reported as unvalued. `scripts/weekly.py --trust-source-points K,DEF`
accepts the export's own point total for those two positions only. It is off by default
because accepting a point total means accepting the exporter's scoring, and it prints a
warning naming every player it applied to. Whether that trade is worth making is a
question to settle by running a week both ways, not by argument.

## drafts/

Completed drafts, anonymized and committable — the input to `scripts/backtest.py`.

`2025-shiva-snake.json` is worth describing, because it is the one record here that is not
a plain snake and it was wrong on disk until 2026-08-30.

**Yahoo reports kept players as picks.** `draftresults` returned 150 selections for a
draft in which only 124 selections were made; the other 26 were keepers slotted into the
board at the round they cost. Replayed as recorded, the board invented 26 picks nobody
made and handed every kept player back to the pool. The record now carries the 26 as
`keepers` with their rounds and the 124 as `picks`, which is what `DraftState` means by a
pick — a selection, not a board position.

**Two picks were traded.** Two teams swapped their round-3 and round-15 picks, so no snake
formula reproduces this board: `pick_schedule` places 122 of 124. That is the ceiling, not
a bug, and it is why the record stores each pick's real owner rather than deriving it.

**It carries `player_names`.** The 2025 ranking snapshot is gone, and a player key means
nothing without the snapshot that minted it, so the record was unreplayable — 150 opaque
ids. Names survive a season, so `capture.rekeyed` crosswalks them onto whatever snapshot
is cached and `scripts/backtest.py` does this automatically, printing what it matched:

```bash
uv run python scripts/backtest.py --file data/drafts/2025-shiva-snake.json \
  --snapshot ~/.ff-helper/cache/snapshot-offline.l.667109.json
```

It also prints a warning, and the warning is the point: **a 2025 draft scored with 2026
valuations measures how much players moved between seasons at least as much as it measures
the engine.** 2025's last-round keepers — Nacua, Achane, Smith-Njigba — are 2026
first-rounders. Treat the output as a check that the replay runs, not as calibration.
