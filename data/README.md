# data/

Projection exports and keeper files live here. Everything except this README and
`.gitkeep` is gitignored — a subscriber export is not ours to redistribute, and the repo
is public.

## Naming

Name each projections file for the league it belongs to:

```
data/projections-461.l.111111.csv     # snake league
data/projections-461.l.222222.csv     # auction league
```

`fetch_rankings.py` picks these up automatically, so neither league needs a flag:

```bash
uv run python scripts/fetch_rankings.py --league 461.l.111111
uv run python scripts/fetch_rankings.py --league 461.l.222222
```

`data/projections.csv` is the fallback when no league-specific file exists. Use it only
if you play in one league, or if your exports carry per-stat columns.

## Why the naming matters

**An export carrying only a points total has already been scored**, under whichever
league's settings were active when you exported it. The app re-scores per-stat lines
under your league's modifiers, but there is nothing to re-score in a points-only file —
it arrives finished.

That makes a mixed-up file the worst kind of wrong. Snake-scored points loaded into an
auction league produce a full board of plausible numbers, every one of them computed
under the wrong rules, and no coverage check or crosswalk can detect it. Naming the file
for its league is what makes that mistake impossible rather than merely unlikely.

`fetch_rankings.py` warns when it falls back to a shared file that carries pre-scored
points, and records which file it used in the snapshot notes.

Per-stat exports do not have this problem — they get scored under whatever league you
point them at, so one file serves both leagues correctly.
