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

## Kickers and defenses

Kicker rows come through with zeroes across every scoreable column, because `FG` and `XP`
have no Yahoo stat IDs the engine can score. Those rows are treated as unprojected and
ranked by consensus instead — which is the intended behaviour, not a gap. Defenses are
usually absent from projection exports entirely and get the same treatment.
