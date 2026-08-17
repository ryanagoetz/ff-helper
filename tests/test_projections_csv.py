"""Tests for CSV projection imports.

The failure mode being guarded against is a *short* file, not a malformed one. A CSV that
fails to parse gets noticed immediately; a CSV that parses to 15 players looks fine, caches
fine, and moves replacement level up above every startable player in the league.
"""

from __future__ import annotations

import httpx
import pytest

from ff_helper.rankings.sources import fantasypros
from ff_helper.rankings.sources.projections_csv import (
    MIN_ROWS,
    ProjectionsError,
    load,
    resolve_path,
)

HEADER = "player,pos,team,pass_yds,pass_td,int,rush_yds,rush_td,rec,rec_yds,rec_td,fum_lost"


def _write(tmp_path, text: str, name: str = "proj.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _full_file(count: int = MIN_ROWS + 5, header: str = HEADER) -> str:
    lines = [header]
    for index in range(count):
        lines.append(f"Player{index},RB,CIN,0,0,0,{900 - index},8,40,300,2,1")
    return "\n".join(lines)


class TestParsing:
    def test_reads_per_stat_columns(self, tmp_path):
        rows = load(_write(tmp_path, _full_file()))
        assert len(rows) == MIN_ROWS + 5
        first = rows[0]
        assert first.name == "Player0"
        assert first.position == "RB"
        assert first.team == "CIN"
        assert first.stats["rush_yds"] == 900
        assert first.stats["rec"] == 40

    def test_stat_keys_match_what_the_scoring_engine_reads(self, tmp_path):
        """A stat spelled any other way is silently never scored."""
        from ff_helper.yahoo.models import PROJECTION_STAT_IDS

        rows = load(_write(tmp_path, _full_file()))
        assert set(rows[0].stats) <= set(PROJECTION_STAT_IDS)

    def test_accepts_alternate_column_spellings(self, tmp_path):
        header = "Name,Position,Tm,Rushing Yards,Rushing TDs,Receptions,Fumbles Lost"
        body = "\n".join(f"Player{i},RB,CIN,{900 - i},8,40,1" for i in range(MIN_ROWS))
        rows = load(_write(tmp_path, f"{header}\n{body}"))
        assert len(rows) == MIN_ROWS
        assert rows[0].stats["rush_yds"] == 900
        assert rows[0].stats["rec"] == 40
        assert rows[0].stats["fum_lost"] == 1

    def test_reads_the_real_4for4_projections_export(self, tmp_path):
        """The exact header of 4for4's per-stat projections export.

        Spellings that were misses on the first pass: "FF Pts" for the total, "Fum" for
        fumbles lost, and stat names separated by spaces rather than underscores.
        """
        header = (
            "PID,Player,Pos,Team,FF Pts,ADP,Pass Comp,Pass Att,Pass Yds,Pass TD,INT,"
            "Rush Att,Rush Yds,Rush TD,Rec,Rec Yds,Rec TD,BYE,Pa1D,Ru1D,Rec1D,Fum,XP,FG"
        )
        body = "\n".join(
            f"play{i},Player{i},QB,BUF,345.1,28,311.3,458.5,{3670 - i},24.5,8.7,"
            f"96.6,505.4,10.1,0,0,0,7,176.5,32.9,0,2.6,0,0"
            for i in range(MIN_ROWS)
        )
        rows = load(_write(tmp_path, f"{header}\n{body}"))
        assert len(rows) == MIN_ROWS
        assert rows[0].position == "QB"
        assert rows[0].team == "BUF"
        assert rows[0].stats["pass_yds"] == 3670
        assert rows[0].stats["fum_lost"] == 2.6
        assert rows[0].projected_points == 345.1

    def test_position_rank_only_supplies_the_position(self, tmp_path):
        header = "player,position-rank,rec_yds"
        body = "\n".join(f"Player{i},WR-{i + 1:02d},{1200 - i}" for i in range(MIN_ROWS))
        rows = load(_write(tmp_path, f"{header}\n{body}"))
        assert {row.position for row in rows} == {"WR"}

    def test_a_plain_position_column_wins_over_the_rank(self, tmp_path):
        header = "player,pos,position-rank,rec_yds"
        body = "\n".join(f"Player{i},TE,WR-{i + 1:02d},{800 - i}" for i in range(MIN_ROWS))
        rows = load(_write(tmp_path, f"{header}\n{body}"))
        assert {row.position for row in rows} == {"TE"}

    def test_all_zero_stat_line_falls_through_to_interpolation(self, tmp_path):
        """Kickers carry FG and XP, which the engine cannot score, and zeros elsewhere.

        Keeping the zeros would score them as a genuine 0.0 and suppress the
        interpolation that is supposed to rank them by consensus.
        """
        header = HEADER + ",fpts"
        body = "\n".join(
            f"Player{i},RB,CIN,0,0,0,{900 - i},8,40,300,2,1,250" for i in range(MIN_ROWS)
        )
        # Shaped like a real kicker row: zeros across every scoreable column, but a
        # points total, because the export scores FG and XP that the engine cannot.
        kicker = "\nKicker Guy,K,DAL,0,0,0,0,0,0,0,0,0,158.1"
        rows = load(_write(tmp_path, f"{header}\n{body}{kicker}"))
        by_name = {row.name: row for row in rows}
        assert by_name["Kicker Guy"].stats == {}
        assert by_name["Kicker Guy"].projected_points == 158.1
        assert by_name["Player0"].stats["rush_yds"] == 900


class TestPointsOnlyIsRejected:
    """blend.py discards a source's own point total, so such a file is worth nothing.

    This was briefly documented as a working fallback. It is not: it yields a board where
    every player is 0.0 and ranking silently reverts to ADP.
    """

    def test_points_only_file_is_rejected(self, tmp_path):
        header = "player,pos,fpts"
        body = "\n".join(f"Player{i},RB,{300 - i}" for i in range(MIN_ROWS))
        with pytest.raises(ProjectionsError, match="no per-stat columns"):
            load(_write(tmp_path, f"{header}\n{body}"))

    def test_the_error_says_what_to_export_instead(self, tmp_path):
        header = "player,pos,ff_pts"
        body = "\n".join(f"Player{i},RB,{300 - i}" for i in range(MIN_ROWS))
        with pytest.raises(ProjectionsError, match="stat columns"):
            load(_write(tmp_path, f"{header}\n{body}"))

    def test_the_4for4_rankings_export_shape_is_rejected(self, tmp_path):
        """The wrong 4for4 report: a rankings table, points-only, no stats to score."""
        header = '"Rank","Player","Team","BYE","Position-Rank","FF Pts","VOR","ADP ( Average )"'
        body = "\n".join(
            f'"{i + 1}","Player{i}","DET","6","RB-{i + 1:02d}","{276.7 - i}","160","{i + 1}"'
            for i in range(MIN_ROWS)
        )
        with pytest.raises(ProjectionsError, match="no per-stat columns"):
            load(_write(tmp_path, f"{header}\n{body}"))

    def test_a_file_with_any_stat_lines_is_still_accepted(self, tmp_path):
        """Mixed files are fine -- only a total absence of stats is fatal."""
        header = "player,pos,fpts,rush_yds"
        body = "\n".join(f"Player{i},RB,{300 - i},{900 - i}" for i in range(MIN_ROWS))
        rows = load(_write(tmp_path, f"{header}\n{body}"))
        assert rows[0].stats["rush_yds"] == 900
        assert rows[0].projected_points == 300

    def test_blank_lines_are_skipped(self, tmp_path):
        rows = load(_write(tmp_path, _full_file() + "\n\n\n"))
        assert len(rows) == MIN_ROWS + 5


class TestLoudFailures:
    def test_short_file_is_rejected(self, tmp_path):
        """The whole point of the module -- see the FantasyPros teaser."""
        with pytest.raises(ProjectionsError, match="too few"):
            load(_write(tmp_path, _full_file(count=MIN_ROWS - 1)))

    def test_missing_player_column_names_what_it_wanted(self, tmp_path):
        body = "\n".join(f"RB,CIN,{900 - i}" for i in range(MIN_ROWS))
        with pytest.raises(ProjectionsError, match="player column"):
            load(_write(tmp_path, f"pos,team,rush_yds\n{body}"))

    def test_unreadable_number_is_an_error_not_a_dropped_stat(self, tmp_path):
        """A stat that quietly becomes None makes the player worth less for no reason."""
        text = _full_file().replace("Player3,RB,CIN,0,0,0,897", "Player3,RB,CIN,0,0,0,8 97")
        with pytest.raises(ProjectionsError, match="not a number"):
            load(_write(tmp_path, text))

    def test_row_with_neither_stats_nor_points_is_reported(self, tmp_path):
        text = _full_file() + "\nGhost Player,RB,CIN,,,,,,,,,"
        with pytest.raises(ProjectionsError, match="no stats and no points"):
            load(_write(tmp_path, text))

    def test_missing_file(self, tmp_path):
        with pytest.raises(ProjectionsError, match="not found"):
            load(tmp_path / "nope.csv")

    def test_empty_file(self, tmp_path):
        with pytest.raises(ProjectionsError, match="no projections"):
            load(_write(tmp_path, HEADER))


class TestPathResolution:
    """Which file a league gets, and whether it was chosen for that league specifically."""

    SNAKE = "461.l.111111"
    AUCTION = "461.l.222222"

    def test_league_specific_file_wins(self, tmp_path):
        (tmp_path / f"projections-{self.SNAKE}.csv").write_text("x")
        (tmp_path / "projections.csv").write_text("x")
        path, specific = resolve_path(tmp_path, self.SNAKE)
        assert path.name == f"projections-{self.SNAKE}.csv"
        assert specific is True

    def test_each_league_gets_its_own_file(self, tmp_path):
        """The mix-up this naming exists to prevent."""
        (tmp_path / f"projections-{self.SNAKE}.csv").write_text("x")
        (tmp_path / f"projections-{self.AUCTION}.csv").write_text("x")
        snake, _ = resolve_path(tmp_path, self.SNAKE)
        auction, _ = resolve_path(tmp_path, self.AUCTION)
        assert snake != auction

    def test_shared_fallback_is_flagged_as_not_league_specific(self, tmp_path):
        (tmp_path / "projections.csv").write_text("x")
        path, specific = resolve_path(tmp_path, self.SNAKE)
        assert path.name == "projections.csv"
        assert specific is False

    def test_explicit_path_overrides_everything(self, tmp_path):
        (tmp_path / f"projections-{self.SNAKE}.csv").write_text("x")
        chosen = tmp_path / "somewhere-else.csv"
        path, specific = resolve_path(tmp_path, self.SNAKE, chosen)
        assert path == chosen
        assert specific is True

    def test_nothing_found(self, tmp_path):
        path, specific = resolve_path(tmp_path, self.SNAKE)
        assert path is None
        assert specific is False

    def test_a_league_without_its_own_file_does_not_borrow_another_leagues(self, tmp_path):
        (tmp_path / f"projections-{self.SNAKE}.csv").write_text("x")
        path, _ = resolve_path(tmp_path, self.AUCTION)
        assert path is None


class TestFantasyProsTeaserGuard:
    """The guard lives in fetch_projections, not parse_projections.

    parse_projections is a pure parser and is legitimately handed short fixtures all over
    the test suite. "This page is a teaser" is a claim about a live fetch, so it belongs
    at the fetch layer.
    """

    def _table(self, rows: int) -> str:
        body = "".join(
            f"<tr><td><a>Player{i}</a><small>CIN</small></td>"
            f"<td>200</td><td>900</td><td>8</td><td>40</td><td>300</td>"
            f"<td>2</td><td>1</td><td>250.5</td></tr>"
            for i in range(rows)
        )
        return f"<table id='data'><tbody>{body}</tbody></table>"

    def _client(self, rows: int) -> httpx.Client:
        html = self._table(rows)
        return httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))
        )

    def test_ten_row_teaser_is_rejected(self):
        """FantasyPros serves exactly this to signed-out callers, and it parses fine."""
        with (
            self._client(fantasypros.TEASER_ROWS) as client,
            pytest.raises(fantasypros.ScrapeError, match="teaser"),
        ):
            fantasypros.fetch_projections(client=client)

    def test_teaser_error_points_at_the_csv_route(self):
        with (
            self._client(fantasypros.TEASER_ROWS) as client,
            pytest.raises(fantasypros.ScrapeError, match="--projections"),
        ):
            fantasypros.fetch_projections(client=client)

    def test_full_table_is_accepted(self):
        with self._client(45) as client:
            rows = fantasypros.fetch_projections(client=client)
        # Four positions requested, all served the same 45-row fixture.
        assert len(rows) == 45 * len(fantasypros.PROJECTION_POSITIONS)

    def test_parser_itself_still_accepts_short_fixtures(self):
        """Regression guard: the check must not creep back into the parser."""
        assert len(fantasypros.parse_projections(self._table(2), "rb")) == 2
