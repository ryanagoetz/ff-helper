#!/usr/bin/env python3
"""Build and read one week's in-season snapshot.

    uv run python scripts/weekly.py --fetch          # pull the week, write it to disk
    uv run python scripts/weekly.py                  # read it back, no network
    uv run python scripts/weekly.py --json           # machine-readable, for the digest task

Split in two the same way the draft is: ``--fetch`` does the network half and writes a
``WeekSnapshot``; everything else runs from disk. That is what makes a digest
reproducible -- rerunning it an hour later reads the same numbers rather than quietly
picking up a status change halfway through an explanation.

Weekly projections come from a CSV export, for the same reasons the season ones do:

    data/weekly/weekly-<league key>-w07.csv     # this league
    data/weekly/weekly-w07.csv                  # any league

Per-stat columns are required. A points total carries the exporter's scoring rather than
your league's, and a start-or-sit call turns on smaller margins than a draft pick does.

Every week's file is kept rather than overwritten. Week 7's projections sitting next to
week 7's actuals is the only thing that makes any of this checkable later.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ff_helper.config import load_settings  # noqa: E402
from ff_helper.engine.scoring import scoring_slug  # noqa: E402
from ff_helper.engine.weekly import (  # noqa: E402
    InactiveRisk,
    MatchupPlan,
    StartSit,
    inactive_check,
    opponent_lineup,
    recommend_for_win,
    start_sit,
)
from ff_helper.rankings.players import PlayerRegistry  # noqa: E402
from ff_helper.rankings.sources import weekly_csv  # noqa: E402
from ff_helper.season import cache as week_cache  # noqa: E402
from ff_helper.season.news.store import NewsStore  # noqa: E402
from ff_helper.season.valuation import WeeklyValuation, weekly_blend  # noqa: E402
from ff_helper.yahoo.client import YahooClient  # noqa: E402
from ff_helper.yahoo.models import League  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Below this, something is structurally wrong with the export rather than merely thin.
MIN_ROSTER_COVERAGE = 0.80

JSON_SCHEMA_VERSION = 1

# Sections the digest can render. Later phases add waivers, matchup, playoffs, trades;
# the renderer is told to show whatever is present rather than to expect a fixed set.
ALL_SECTIONS = ("lineup", "matchup", "inactive_check")


def _sections(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ALL_SECTIONS
    chosen = tuple(part.strip() for part in raw.split(",") if part.strip())
    unknown = [name for name in chosen if name not in ALL_SECTIONS]
    if unknown:
        raise SystemExit(
            f"Unknown section(s): {', '.join(unknown)}. Known: {', '.join(ALL_SECTIONS)}"
        )
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", help="League key. Defaults to FF_LEAGUE_KEY.")
    parser.add_argument(
        "--week",
        type=int,
        help="Week to work on. Defaults to whatever Yahoo says is current.",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Hit Yahoo and rebuild the week's snapshot. Without this the script reads "
        "the snapshot from disk and touches no network at all.",
    )
    parser.add_argument(
        "--projections",
        type=Path,
        help="Weekly projections CSV. Defaults to data/weekly/weekly-<league>-wNN.csv, "
        "then data/weekly/weekly-wNN.csv.",
    )
    parser.add_argument(
        "--trust-source-points",
        default="",
        metavar="POSITIONS",
        help="Comma-separated positions where the export's own point total may be used "
        "when there is no scoreable stat line, e.g. 'K,DEF'. Off by default: accepting a "
        "point total means accepting the exporter's scoring instead of your league's. "
        "Kickers and defenses are the honest exception -- they have no stat line here at "
        "all -- but it is opt-in and it reports itself.",
    )
    parser.add_argument(
        "--no-news-adjustments",
        action="store_true",
        help="Ignore the captured news store entirely. The one-word off switch for when "
        "extraction misbehaves -- projections revert to Yahoo status only, and the "
        "sentences are still visible via scripts/news.py --list.",
    )
    parser.add_argument(
        "--as-of",
        metavar="ISO8601",
        help="Only use news captured before this moment, e.g. 2026-10-20T18:00. Lets a "
        "backtest ask what Tuesday's digest would have said without Thursday's news "
        "leaking into the answer.",
    )
    parser.add_argument(
        "--sections",
        default="",
        help=f"Comma-separated subset of {','.join(ALL_SECTIONS)}. Defaults to all of "
        "them. The digest runs at different times for different reasons -- the "
        "pre-waivers run does not need the gameday inactive sweep.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object on stdout and nothing else. Human output goes to "
        "stderr, so `weekly.py --json | jq` works.",
    )
    args = parser.parse_args()

    out = sys.stderr if args.json else sys.stdout

    def say(message: str = "") -> None:
        print(message, file=out)

    trust = frozenset(
        part.strip().upper() for part in args.trust_source_points.split(",") if part.strip()
    )

    result = _fetch(args, trust, say) if args.fetch else _from_disk(args, say)

    if result is None:
        return 1
    snapshot = result
    league = snapshot.league()
    if league.settings is None:
        say("The stored week carries no league settings; re-run with --fetch.")
        return 1

    registry = PlayerRegistry(snapshot.players)

    news: NewsStore | None = None
    as_of = _parse_as_of(args.as_of)
    if not args.no_news_adjustments:
        news = NewsStore.load(snapshot.league_key, snapshot.season)
    blend = weekly_blend(
        registry,
        snapshot.projections,
        league.settings,
        snapshot.week,
        trust_source_points=trust,
        news=news,
        as_of=as_of,
    )

    for note in snapshot.notes + blend.notes:
        say(f"  note: {note}")
    _report_news(news, blend.valuations, snapshot.week, as_of, say)

    sections = _sections(args.sections)
    entries = snapshot.roster_for(snapshot.my_team_key)
    plan = start_sit(blend.valuations, entries, league.settings)
    # Checked against the lineup the digest is telling you to field, not the one currently
    # set. Otherwise the two sections contradict each other: the lineup section says
    # "start Odunze at WR" while the gameday check offers the same Odunze as a swap for
    # somebody else, and following both would start him twice. Anyone risky who is in the
    # current lineup but not the recommended one already appears in the changes list, with
    # the bye or the injury as the stated reason.
    risks = inactive_check(blend.valuations, entries, league.settings, lineup=plan.optimal)

    matchup_plan = None
    matchup_gap = ""
    if "matchup" in sections:
        matchup_plan, matchup_gap = _matchup_plan(
            snapshot, blend.valuations, league.settings, entries
        )

    coverage = _report_lineup(snapshot, blend.valuations, plan, sections, say)
    if matchup_plan is not None:
        _report_matchup(snapshot, blend.valuations, matchup_plan, say)
    elif "matchup" in sections:
        say(f"\nMatchup: not available -- {matchup_gap}.")
    if "inactive_check" in sections:
        _report_inactives(risks, say)

    if args.json:
        print(
            json.dumps(
                _as_json(
                    league, snapshot, blend.valuations, plan, risks, sections, news,
                    matchup_plan,
                ),
                indent=2,
            )
        )

    if coverage is not None and coverage < MIN_ROSTER_COVERAGE:
        say(
            f"\nOnly {coverage:.0%} of your roster has a projection this week. Players "
            "missing here are invisible to every recommendation -- check the export "
            "covers the whole player pool and is the right week."
        )
        return 2
    return 0


def _fetch(args, trust: frozenset[str], say) -> week_cache.WeekSnapshot | None:
    settings = load_settings()
    league_key = args.league or settings.league_key
    if not league_key:
        say("No league key. Set FF_LEAGUE_KEY in .env or pass --league.")
        return None

    notes: list[str] = []

    with YahooClient(settings) as client:
        say(f"Fetching league {league_key} ...")
        league = client.league(league_key)
        if league.settings is None:
            say("Could not read league settings; cannot score projections.")
            return None

        week = args.week or league.current_week
        if week is None:
            say(
                "Yahoo did not report a current week and none was given. Pass --week -- "
                "guessing would put every projection against the wrong schedule."
            )
            return None

        say(f"  {league.name}: week {week}, {league.num_teams} teams, "
            f"{scoring_slug(league.settings)} scoring")

        teams = client.teams(league_key)
        my_team_key = next((team.team_key for team in teams if team.is_mine), "")
        if not my_team_key:
            # Everything personal keys off this. An empty value would silently produce a
            # report about nobody's roster, which reads exactly like an empty roster.
            notes.append("WARNING: Yahoo did not mark any team as yours")
            say("  WARNING: no team is marked as yours; the roster report will be empty")

        say(f"Fetching rosters for {len(teams)} teams ...")
        entries, failed = client.rosters(teams, week)
        rosters: dict[str, list] = {}
        for entry in entries:
            rosters.setdefault(entry.team_key, []).append(entry)
        if failed:
            # Reported, never swallowed: a team missing from this dict looks exactly like
            # a team whose players are all free agents.
            notes.append(f"{len(failed)} team rosters failed to load: {'; '.join(failed)}")
            say(f"  FAILED for {len(failed)} teams: {'; '.join(failed)}")
        say(f"  {len(entries)} rostered players")

        say("Fetching the player pool ...")
        players = client.players(league_key, limit=600, with_draft_analysis=False)
        say(f"  {len(players)} players")

        say("Fetching free agents (with ownership) ...")
        try:
            free_agents = client.free_agents(league_key, limit=300)
        except Exception as exc:  # noqa: BLE001 - one dead source must not sink the run
            free_agents = []
            notes.append(f"Free agents unavailable: {exc}")
            say(f"  FAILED: {exc}")
        say(f"  {len(free_agents)} available")

        # Free-agent records carry percent_owned; pool records do not. Later wins, so the
        # richer copy of a player replaces the thinner one rather than sitting beside it.
        merged = {player.player_key: player for player in players}
        merged.update({player.player_key: player for player in free_agents})

        try:
            matchups = client.scoreboard(league_key, week)
        except Exception as exc:  # noqa: BLE001
            matchups = []
            notes.append(f"Scoreboard unavailable: {exc}")
            say(f"  FAILED: {exc}")

        try:
            transactions = client.transactions(league_key)
        except Exception as exc:  # noqa: BLE001
            transactions = []
            notes.append(f"Transactions unavailable: {exc}")
            say(f"  FAILED: {exc}")

    path, league_specific = weekly_csv.resolve_path(DATA_DIR, league_key, week, args.projections)
    if path is None:
        say(
            f"\nNo weekly projections for week {week}. Export them and put the file at\n"
            f"  {DATA_DIR / 'weekly' / f'weekly-w{week:02d}.csv'}\n"
            "or pass --projections. Per-stat columns are required."
        )
        return None

    say(f"Reading weekly projections from {path} ...")
    try:
        projections, csv_notes = weekly_csv.load(path, week=week)
    except weekly_csv.ProjectionsError as exc:
        say(f"  FAILED: {exc}")
        return None
    notes.extend(csv_notes)
    notes.append(
        f"Weekly projections from {path.name}"
        + ("" if league_specific else " (shared across leagues)")
    )
    say(f"  {len(projections)} players projected")

    registry = PlayerRegistry(list(merged.values()))
    _, report = registry.crosswalk([p.row for p in projections])
    say(f"\nCrosswalk: {report.match_rate:.1%} of weekly rows matched a Yahoo player")
    if report.unmatched:
        say(f"  {len(report.unmatched)} unmatched:")
        for row in report.unmatched[:15]:
            say(f"    {row.name} ({row.position} {row.team})")
        if len(report.unmatched) > 15:
            say(f"    ... and {len(report.unmatched) - 15} more")

    snapshot = week_cache.WeekSnapshot(
        league_key=league_key,
        season=league.season,
        week=week,
        fetched_at=time.time(),
        league_name=league.name,
        num_teams=league.num_teams,
        my_team_key=my_team_key,
        team_names={team.team_key: team.name for team in teams},
        settings=league.settings,
        players=list(merged.values()),
        projections=projections,
        rosters=rosters,
        free_agent_keys=[player.player_key for player in free_agents],
        matchups=matchups,
        transactions=transactions,
        notes=notes,
    )
    written = week_cache.save(snapshot)
    say(f"Snapshot written to {written}")
    return snapshot


def _from_disk(args, say) -> week_cache.WeekSnapshot | None:
    """Read a stored week. No network, no credentials, no Yahoo call at all."""
    settings = load_settings(require_credentials=False)
    league_key = args.league or settings.league_key
    if not league_key:
        say("No league key. Set FF_LEAGUE_KEY in .env or pass --league.")
        return None

    say("Reading the stored week (no network) ...")
    snapshot = None
    for season in _seasons_on_disk(league_key):
        snapshot = (
            week_cache.load(league_key, season, args.week)
            if args.week is not None
            else week_cache.latest(league_key, season)
        )
        if snapshot is not None:
            break

    if snapshot is None:
        say(
            f"No stored week for {league_key}"
            + (f" week {args.week}" if args.week else "")
            + ".\nRun: uv run python scripts/weekly.py --fetch"
        )
        return None

    say(f"  {snapshot.league_name}: week {snapshot.week}, fetched "
        f"{snapshot.age_hours:.1f}h ago")
    if snapshot.age_hours > 72:
        say("  WARNING: this snapshot is over three days old; statuses have moved since")
    return snapshot


def _seasons_on_disk(league_key: str) -> list[str]:
    """Seasons with at least one stored week, newest first."""
    from ff_helper.config import cache_dir

    safe = league_key.replace("/", "_")
    seasons: set[str] = set()
    for path in cache_dir().glob(f"week-{safe}-*.json"):
        parts = path.stem.split("-")
        if len(parts) >= 3:
            seasons.add(parts[-2])
    return sorted(seasons, reverse=True)


def _report_lineup(
    snapshot: week_cache.WeekSnapshot,
    valuations: dict[str, WeeklyValuation],
    plan: StartSit,
    sections: tuple[str, ...],
    say,
) -> float | None:
    my_team_key = snapshot.my_team_key
    entries = snapshot.roster_for(my_team_key)
    if not entries:
        say("\nNo roster stored for your team.")
        return None
    if "lineup" not in sections:
        return _coverage(entries, valuations)

    def name_of(player_key: str) -> str:
        valuation = valuations.get(player_key)
        return valuation.name if valuation else player_key

    say(f"\nWeek {snapshot.week} lineup -- {snapshot.team_name(my_team_key)}")
    say(f"  {'slot':<7} {'player':<26} {'pos':<5} {'opp':<6} {'proj':>7}  notes")

    started = set(plan.current.starter_keys)
    for slot, player_key in plan.current.starters:
        say(f"  {slot.position:<7} {_row(name_of(player_key), valuations.get(player_key))}")
    for slot in plan.current.empty:
        say(f"  {slot.position:<7} {'(empty)':<26}")
    for entry in entries:
        if entry.player_key in started:
            continue
        say(f"  {'BN':<7} {_row(name_of(entry.player_key), valuations.get(entry.player_key))}")

    say(f"\n  currently set: {plan.current.total:.1f} projected")
    matchup = snapshot.matchup_for(my_team_key)
    if matchup is not None:
        say(
            f"  opponent {snapshot.team_name(matchup.opponent_key)}"
            + (
                f", Yahoo projects them {matchup.opponent_projected:.1f}"
                if matchup.opponent_projected is not None
                else ""
            )
        )

    if plan.is_already_optimal:
        say("\n  Best available lineup is already set. Nothing to change.")
    else:
        say(f"  best available: {plan.optimal.total:.1f} "
            f"({plan.points_left_on_bench:+.1f} on the bench)")
        say("\n  Changes to make:")
        for change in plan.changes:
            over = f" over {name_of(change.player_out)}" if change.player_out else ""
            say(
                f"    start {name_of(change.player_in)}{over} at {change.slot or '?'}  "
                f"({change.points_delta:+.1f}) -- {change.reason}"
            )

    if plan.optimal.empty:
        say(
            "\n  No eligible player for: "
            + ", ".join(slot.position for slot in plan.optimal.empty)
            + " -- that slot scores zero until you fill it."
        )
    if plan.unvalued:
        say(
            f"\n  {len(plan.unvalued)} rostered players have no projection and were left "
            "out of the lineup maths entirely (not treated as zero)."
        )

    return _coverage(entries, valuations)


def _coverage(entries, valuations: dict[str, WeeklyValuation]) -> float:
    covered = sum(1 for entry in entries if entry.player_key in valuations)
    return covered / len(entries) if entries else 1.0


def _row(name: str, valuation: WeeklyValuation | None) -> str:
    if valuation is None:
        return f"{name[:26]:<26} {'':<5} {'':<6} {'--':>7}  no projection"
    return (
        f"{name[:26]:<26} {valuation.position:<5} {valuation.opponent or '-':<6} "
        f"{valuation.projected_points:>7.1f}  {_flags(valuation)}"
    )


def _parse_as_of(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        raise SystemExit(
            f"--as-of {raw!r} is not an ISO 8601 timestamp (e.g. 2026-10-20T18:00)"
        ) from None


def _report_news(
    news: NewsStore | None,
    valuations: dict[str, WeeklyValuation],
    week: int,
    as_of: float | None,
    say,
) -> None:
    """Print every projection the news moved, with the sentence that moved it.

    This section is the reason the news layer is allowed to touch a number at all. An
    adjustment you cannot trace back to a sentence is one you cannot overrule.
    """
    if news is None:
        say("\nNews adjustments are switched off (--no-news-adjustments).")
        return

    adjusted = [v for v in valuations.values() if v.adjustments]
    if not adjusted:
        if news.is_empty:
            say("\nNo articles captured yet -- see scripts/news.py.")
        else:
            say(f"\n{len(news.articles)} article(s) captured, none affecting this week.")
        return

    say(f"\nNews adjustments ({len(adjusted)} player(s))"
        + (f", as known at {datetime.fromtimestamp(as_of):%Y-%m-%d %H:%M}" if as_of else ""))
    for valuation in sorted(
        adjusted, key=lambda v: -abs(v.pre_news_points - v.projected_points)
    ):
        # Compared against the pre-news number, not the raw stat line: Yahoo's own injury
        # designation had already been applied, and crediting news with that cut too
        # would overstate what the articles are doing.
        say(f"  {valuation.name}: {valuation.pre_news_points:.1f} -> "
            f"{valuation.projected_points:.1f}  (news x{valuation.news_multiplier:.2f})")
        for adjustment in valuation.adjustments:
            age = (time.time() - (adjustment.observed_at or time.time())) / 3600.0
            marks = []
            if adjustment.source == "inferred":
                marks.append("inferred")
            if age > 72:
                marks.append("stale")
            suffix = f"  ({', '.join(marks)})" if marks else ""
            say(f"      {adjustment.flag} x{adjustment.multiplier}, "
                f"{age:.0f}h ago{suffix}")
            say(f"      \"{adjustment.quote}\"")


def _matchup_plan(
    snapshot: week_cache.WeekSnapshot,
    valuations: dict[str, WeeklyValuation],
    settings,
    entries,
) -> tuple[MatchupPlan | None, str]:
    """Build the matchup plan, or explain why there isn't one.

    Returns a reason rather than just None. A section that silently disappears reads as
    "nothing to report" when it usually means "something upstream is missing", and the
    difference is the whole value of the note.
    """
    matchup = snapshot.matchup_for(snapshot.my_team_key)
    if matchup is None:
        return None, "no matchup stored for this week (bye week, or the scoreboard failed)"
    if not entries:
        return None, "no roster stored for your team"

    their_entries = snapshot.roster_for(matchup.opponent_key)
    if not their_entries:
        return None, f"no roster stored for {snapshot.team_name(matchup.opponent_key)}"

    opponent = opponent_lineup(valuations, their_entries, settings)
    if not opponent:
        return None, (
            f"none of {snapshot.team_name(matchup.opponent_key)}'s "
            f"{len(their_entries)} rostered players have a projection this week"
        )
    return recommend_for_win(valuations, entries, settings, opponent), ""


def _report_matchup(
    snapshot: week_cache.WeekSnapshot,
    valuations: dict[str, WeeklyValuation],
    plan: MatchupPlan,
    say,
) -> None:
    matchup = snapshot.matchup_for(snapshot.my_team_key)
    opponent_name = snapshot.team_name(matchup.opponent_key) if matchup else "opponent"

    def name_of(player_key: str) -> str:
        valuation = valuations.get(player_key)
        return valuation.name if valuation else player_key

    outcome = plan.recommended_outcome
    say(f"\nMatchup vs {opponent_name}")
    say(f"  you   {outcome.mine.mean:6.1f} projected, sd {outcome.mine.sd:4.1f}  "
        f"(floor {outcome.mine.floor:.0f}, ceiling {outcome.mine.ceiling:.0f})")
    say(f"  them  {outcome.theirs.mean:6.1f} projected, sd {outcome.theirs.sd:4.1f}  "
        f"(floor {outcome.theirs.floor:.0f}, ceiling {outcome.theirs.ceiling:.0f})")
    say(f"  win probability: {outcome.win_probability:.1%}")

    if not plan.differs:
        say(f"  {plan.reason}")
    else:
        # Both lineups are always shown. Reporting only the recommendation would hide
        # what it costs, and the trade is the whole point of the section.
        say(f"\n  Max points:  {plan.max_ev.total:.1f} proj, "
            f"{plan.max_ev_outcome.win_probability:.1%} to win")
        say(f"  Recommended: {plan.recommended.total:.1f} proj, "
            f"{plan.recommended_outcome.win_probability:.1%} to win")
        for swap in plan.swaps:
            over = f" over {name_of(swap.player_out)}" if swap.player_out else ""
            say(f"    {name_of(swap.player_in)}{over} at {swap.slot or '?'} "
                f"({swap.points_delta:+.1f} points)")
        say(f"  {plan.reason}")

    say("  (assumes your opponent starts their best legal lineup, which is the "
        "conservative error)")


def _report_inactives(risks: tuple[InactiveRisk, ...], say) -> None:
    if not risks:
        say("\nGameday check: every starter is active and off bye.")
        return

    plural = "starter" if len(risks) == 1 else "starters"
    say(f"\nGameday check -- {len(risks)} {plural} at risk")
    for risk in risks:
        marker = "WILL NOT PLAY" if risk.is_certain else "may not play"
        say(f"  {marker}: {risk.name} ({risk.slot}, {risk.projected_points:.1f} proj)"
            f" -- {risk.status_full or risk.reason}")
        if risk.replacement_name:
            say(f"      best legal swap: {risk.replacement_name} "
                f"({risk.replacement_points:.1f}, {risk.gain_from_replacing:+.1f})")
        else:
            say("      no eligible replacement on your bench")


def _flags(valuation: WeeklyValuation) -> str:
    parts: list[str] = []
    if valuation.is_bye:
        parts.append("BYE")
    if valuation.status:
        parts.append(valuation.status_full or valuation.status)
    if valuation.points_estimated:
        parts.append("estimated from the export's own total")
    for adjustment in valuation.adjustments:
        # Named inline as well as in the news section: a number in a lineup table that
        # has been moved should say so where you are looking at it, not only further down.
        parts.append(f"news {adjustment.flag} x{adjustment.multiplier:g}")
    return ", ".join(parts)


def _as_json(
    league: League,
    snapshot: week_cache.WeekSnapshot,
    valuations: dict[str, WeeklyValuation],
    plan: StartSit,
    risks: tuple[InactiveRisk, ...],
    sections: tuple[str, ...],
    news: NewsStore | None,
    matchup_plan: MatchupPlan | None,
) -> dict:
    """The digest contract. Sections that do not exist yet are simply absent.

    Every number here was computed in Python. The renderer's job is to choose what to say
    and in what order -- if a figure is not in this object, it is not a figure.
    """
    settings = league.settings
    my_team_key = snapshot.my_team_key

    def name_of(player_key: str) -> str:
        valuation = valuations.get(player_key)
        return valuation.name if valuation else player_key

    payload: dict = {}
    if "lineup" in sections:
        payload["lineup"] = {
            "current": _lineup_json(plan.current, valuations),
            "optimal": _lineup_json(plan.optimal, valuations),
            "points_left_on_bench": round(plan.points_left_on_bench, 2),
            "is_already_optimal": plan.is_already_optimal,
            "changes": [
                {
                    "player_in": change.player_in,
                    "player_in_name": name_of(change.player_in),
                    "player_out": change.player_out,
                    "player_out_name": name_of(change.player_out) if change.player_out else "",
                    "slot": change.slot,
                    "points_delta": round(change.points_delta, 2),
                    "reason": change.reason,
                }
                for change in plan.changes
            ],
            "unvalued": list(plan.unvalued),
        }
    adjusted = [v for v in valuations.values() if v.adjustments]
    payload["news"] = {
        "enabled": news is not None,
        "articles": len(news.articles) if news else 0,
        "players_adjusted": len(adjusted),
        "adjustments": [
            {
                "player_key": valuation.player_key,
                "name": valuation.name,
                "base_points": round(valuation.base_points, 2),
                "pre_news_points": round(valuation.pre_news_points, 2),
                "projected": round(valuation.projected_points, 2),
                "news_multiplier": round(valuation.news_multiplier, 3),
                "effective_multiplier": round(valuation.effective_multiplier, 3),
                "flags": [
                    {
                        "flag": adjustment.flag,
                        "multiplier": adjustment.multiplier,
                        "quote": adjustment.quote,
                        "url": adjustment.url,
                        "confidence": adjustment.source,
                        "age_hours": round(
                            (time.time() - (adjustment.observed_at or time.time())) / 3600.0,
                            1,
                        ),
                    }
                    for adjustment in valuation.adjustments
                ],
            }
            for valuation in adjusted
        ],
    }

    if matchup_plan is not None:
        outcome = matchup_plan.recommended_outcome
        their_matchup = snapshot.matchup_for(my_team_key)
        payload["matchup"] = {
            "opponent_key": their_matchup.opponent_key if their_matchup else "",
            "opponent_name": (
                snapshot.team_name(their_matchup.opponent_key) if their_matchup else ""
            ),
            "my_mean": round(outcome.mine.mean, 2),
            "my_sd": round(outcome.mine.sd, 2),
            "my_floor": round(outcome.mine.floor, 2),
            "my_ceiling": round(outcome.mine.ceiling, 2),
            "their_mean": round(outcome.theirs.mean, 2),
            "their_sd": round(outcome.theirs.sd, 2),
            "win_probability": round(outcome.win_probability, 4),
            "margin_mean": round(outcome.margin_mean, 2),
            "differs_from_max_points": matchup_plan.differs,
            "max_points_lineup": {
                "total": round(matchup_plan.max_ev.total, 2),
                "win_probability": round(
                    matchup_plan.max_ev_outcome.win_probability, 4
                ),
            },
            "recommended_lineup": {
                "total": round(matchup_plan.recommended.total, 2),
                "win_probability": round(
                    matchup_plan.recommended_outcome.win_probability, 4
                ),
            },
            "win_probability_gain": round(matchup_plan.win_probability_gain, 4),
            "points_given_up": round(matchup_plan.points_given_up, 2),
            "swaps": [
                {
                    "player_in": swap.player_in,
                    "player_in_name": name_of(swap.player_in),
                    "player_out": swap.player_out,
                    "player_out_name": name_of(swap.player_out) if swap.player_out else "",
                    "slot": swap.slot,
                    "points_delta": round(swap.points_delta, 2),
                }
                for swap in matchup_plan.swaps
            ],
            "reason": matchup_plan.reason,
            "opponent_assumption": "opponent starts their best legal lineup",
        }

    if "inactive_check" in sections:
        payload["inactive_check"] = {
            "at_risk": [
                {
                    "player_key": risk.player_key,
                    "name": risk.name,
                    "slot": risk.slot,
                    "reason": risk.reason,
                    "is_certain": risk.is_certain,
                    "status_full": risk.status_full,
                    "projected": round(risk.projected_points, 2),
                    "replacement": risk.replacement_key,
                    "replacement_name": risk.replacement_name,
                    "replacement_projected": round(risk.replacement_points, 2),
                    "gain_from_replacing": round(risk.gain_from_replacing, 2),
                }
                for risk in risks
            ]
        }

    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "generated_at": time.time(),
        "league": {
            "key": snapshot.league_key,
            "season": snapshot.season,
            "week": snapshot.week,
            "fetched_at": snapshot.fetched_at,
            "uses_faab": bool(settings and settings.uses_faab),
            "waiver_type": settings.waiver_type if settings else "",
            "playoff_start_week": settings.playoff_start_week if settings else None,
        },
        "my_team": {
            "key": my_team_key,
            "name": snapshot.team_name(my_team_key),
            "roster": [
                {
                    "player_key": entry.player_key,
                    "slot": entry.selected_position,
                    "is_starting": entry.is_starting,
                    **_valuation_json(valuations.get(entry.player_key)),
                }
                for entry in snapshot.roster_for(my_team_key)
            ],
        },
        **payload,
        "notes": snapshot.notes,
    }


def _lineup_json(board, valuations: dict[str, WeeklyValuation]) -> dict:
    return {
        "total": round(board.total, 2),
        "starters": [
            {
                "slot": slot.position,
                "slot_index": slot.index,
                "player_key": player_key,
                **_valuation_json(valuations.get(player_key)),
            }
            for slot, player_key in board.starters
        ],
        "bench": list(board.bench),
        "empty_slots": [slot.position for slot in board.empty],
    }


def _valuation_json(valuation: WeeklyValuation | None) -> dict:
    if valuation is None:
        return {"projected": None}
    return {
        "name": valuation.name,
        "position": valuation.position,
        "eligible_positions": list(valuation.eligible_positions),
        "opponent": valuation.opponent,
        "projected": round(valuation.projected_points, 2),
        "base_points": round(valuation.base_points, 2),
        "is_bye": valuation.is_bye,
        "status": valuation.status,
        "status_full": valuation.status_full,
        "availability": valuation.availability,
        "points_estimated": valuation.points_estimated,
        "adjustments": [
            {
                "flag": adjustment.flag,
                "multiplier": adjustment.multiplier,
                "quote": adjustment.quote,
                "url": adjustment.url,
            }
            for adjustment in valuation.adjustments
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
