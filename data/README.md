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
