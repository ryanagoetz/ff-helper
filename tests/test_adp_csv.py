"""Tests for platform ADP read from a rankings export.

The point of this source is *which room* the ADP describes. Yahoo ADP earns the heaviest
weight in the blend because it predicts your actual draft; a national average wearing that
label would be a lie the board could not detect, so the column choice is tested harder
than the parsing.
"""

from __future__ import annotations

import pytest

from ff_helper.rankings.sources.adp_csv import AdpError, load, pick_column

# The real 4for4 rankings header, trimmed to the columns that matter.
HEADER = [
    "Rank",
    "Player",
    "Team",
    "BYE",
    "Position-Rank",
    "FF Pts",
    "ADP ( Average )",
    "ADP Dif ( Average )",
    "ADP (ESPN)",
    "ADP (Y!)",
    "ADP Dif (Y!)",
]


def _row(rank, name, team, pos_rank, avg, espn, yahoo):
    return f'"{rank}","{name}","{team}","6","{pos_rank}","250","{avg}","0","{espn}","{yahoo}","0"'


def _write(tmp_path, rows, header=None):
    path = tmp_path / "adp.csv"
    lines = [",".join(f'"{h}"' for h in (header or HEADER))]
    lines.extend(rows)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _board(count=30, yahoo_from=1):
    return [
        _row(i + 1, f"Player{i}", "DET", f"RB-{i + 1:02d}", i + 1, i + 2, yahoo_from + i)
        for i in range(count)
    ]


class TestColumnChoice:
    def test_yahoo_column_wins_over_average_and_espn(self):
        index, label = pick_column(HEADER)
        assert HEADER[index] == "ADP (Y!)"
        assert label == "yahoo"

    def test_the_difference_column_is_never_chosen(self):
        """"ADP Dif (Y!)" is a delta, not an ADP; reading it yields ADPs near zero."""
        index, _ = pick_column(HEADER)
        assert "Dif" not in HEADER[index]

    def test_falls_back_to_average_without_a_platform_column(self):
        header = ["Rank", "Player", "Team", "ADP ( Average )"]
        index, label = pick_column(header)
        assert header[index] == "ADP ( Average )"
        # Crucially not "yahoo": a national average must not earn Yahoo's 0.65 weight.
        assert label != "yahoo"

    def test_no_adp_column_at_all(self):
        with pytest.raises(AdpError, match="No ADP column"):
            pick_column(["Rank", "Player", "Team"])


class TestParsing:
    def test_reads_yahoo_adp(self, tmp_path):
        rows, label = load(_write(tmp_path, _board()))
        assert label == "yahoo"
        assert len(rows) == 30
        assert rows[0].adp == 1.0
        assert rows[0].position == "RB"
        assert rows[0].team == "DET"

    def test_undrafted_markers_are_skipped_not_treated_as_pick_one(self, tmp_path):
        """4for4 writes "'--" and 0 for "no ADP on this platform"."""
        rows = _board(25) + [
            _row(26, "Undrafted Guy", "DET", "RB-26", 300, 300, "'--"),
            _row(27, "Zero Guy", "DET", "RB-27", 300, 300, "0"),
        ]
        parsed, _ = load(_write(tmp_path, rows))
        names = {row.name for row in parsed}
        assert "Undrafted Guy" not in names
        assert "Zero Guy" not in names
        assert min(row.adp for row in parsed) == 1.0

    def test_position_comes_from_the_combined_rank(self, tmp_path):
        rows, _ = load(_write(tmp_path, _board()))
        assert {row.position for row in rows} == {"RB"}

    def test_rows_carry_no_projection_data(self, tmp_path):
        """This source speaks only about the market; values come from projections."""
        rows, _ = load(_write(tmp_path, _board()))
        assert all(row.projected_points is None and not row.stats for row in rows)


class TestLoudFailures:
    def test_missing_file(self, tmp_path):
        with pytest.raises(AdpError, match="not found"):
            load(tmp_path / "nope.csv")

    def test_too_few_rows_is_rejected(self, tmp_path):
        with pytest.raises(AdpError, match="only"):
            load(_write(tmp_path, _board(5)))

    def test_unreadable_values_are_reported(self, tmp_path):
        rows = _board(25) + [_row(26, "Weird Guy", "DET", "RB-26", 300, 300, "1 2 3")]
        with pytest.raises(AdpError, match="could not be read"):
            load(_write(tmp_path, rows))

    def test_no_player_column(self, tmp_path):
        header = ["Rank", "Team", "ADP (Y!)"]
        path = tmp_path / "adp.csv"
        path.write_text('"Rank","Team","ADP (Y!)"\n"1","DET","1"\n', encoding="utf-8")
        with pytest.raises(AdpError, match="no player column"):
            load(path)
