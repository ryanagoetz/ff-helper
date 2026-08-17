"""Tests for running without the Yahoo API.

Offline mode replaces the two things Yahoo supplies that nothing else does: the league
description and the canonical player registry. Both are places where a quiet mistake
turns into a whole draft of subtly wrong advice, so the failures here are loud.
"""

from __future__ import annotations

import pytest
import yaml

from ff_helper import offline
from ff_helper.rankings.players import SourceRow

BASE_CONFIG = {
    "name": "Bust A Move",
    "league_id": "107878",
    "num_teams": 12,
    "draft_type": "auction",
    "auction_budget": 200,
    "my_team": "Team Ryan",
    "teams": ["Team Ryan"] + [f"Team {i}" for i in range(2, 13)],
    "roster": {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 6, "IR": 1},
    "scoring": {
        "pass_yds": 0.04,
        "pass_td": 4,
        "int": -1,
        "rush_yds": 0.1,
        "rush_td": 6,
        "rec": 0.5,
        "rec_yds": 0.1,
        "rec_td": 6,
        "fum_lost": -2,
    },
}


def _write(tmp_path, config: dict, name: str = "league.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _config(**overrides) -> dict:
    merged = {**BASE_CONFIG, **overrides}
    return merged


class TestLoadConfig:
    def test_reads_a_full_league(self, tmp_path):
        result = offline.load_config(_write(tmp_path, _config()))
        league = result.league
        assert league.name == "Bust A Move"
        assert league.num_teams == 12
        assert league.settings.is_auction is True
        assert league.settings.auction_budget == 200
        assert league.draft_status == "predraft"

    def test_roster_size_excludes_ir(self, tmp_path):
        """15 draftable spots, not 16 -- auction max bid is computed off this."""
        result = offline.load_config(_write(tmp_path, _config()))
        assert result.league.settings.roster_size == 15

    def test_flex_counts_toward_every_eligible_position(self, tmp_path):
        settings = offline.load_config(_write(tmp_path, _config())).league.settings
        assert settings.starters_at("RB") == 3  # RB, RB, W/R/T
        assert settings.starters_at("TE") == 2  # TE, W/R/T

    def test_scoring_maps_onto_engine_stat_ids(self, tmp_path):
        from ff_helper.yahoo.models import STAT_PASS_YDS, STAT_REC

        settings = offline.load_config(_write(tmp_path, _config())).league.settings
        assert settings.stat_modifiers[STAT_REC] == 0.5
        assert settings.stat_modifiers[STAT_PASS_YDS] == 0.04

    def test_unscoreable_categories_are_reported_not_dropped_silently(self, tmp_path):
        """A league scoring return yards still works, but must be told it loses them."""
        scoring = {**BASE_CONFIG["scoring"], "return_yds": 0.02, "first_downs": 0.5}
        result = offline.load_config(_write(tmp_path, _config(scoring=scoring)))
        assert any("cannot be scored" in note for note in result.notes)
        assert any("return_yds" in note for note in result.notes)

    def test_snake_league(self, tmp_path):
        result = offline.load_config(_write(tmp_path, _config(draft_type="snake")))
        assert result.league.settings.is_auction is False

    def test_teams_are_generated_when_absent(self, tmp_path):
        config = _config()
        del config["teams"]
        config["my_team"] = "Team 1"
        result = offline.load_config(_write(tmp_path, config))
        assert len(result.teams) == 12

    def test_exactly_one_team_is_mine(self, tmp_path):
        result = offline.load_config(_write(tmp_path, _config()))
        mine = [team for team in result.teams if team.is_mine]
        assert len(mine) == 1
        assert mine[0].name == "Team Ryan"


class TestLoudFailures:
    def test_missing_file(self, tmp_path):
        with pytest.raises(offline.OfflineConfigError, match="not found"):
            offline.load_config(tmp_path / "nope.yaml")

    def test_my_team_is_required(self, tmp_path):
        """Roster needs, budget, and max bid are all computed against your team."""
        config = _config()
        del config["my_team"]
        with pytest.raises(offline.OfflineConfigError, match="my_team"):
            offline.load_config(_write(tmp_path, config))

    def test_my_team_must_be_in_the_teams_list(self, tmp_path):
        with pytest.raises(offline.OfflineConfigError, match="not in the teams list"):
            offline.load_config(_write(tmp_path, _config(my_team="Somebody Else")))

    def test_team_count_must_match_num_teams(self, tmp_path):
        with pytest.raises(offline.OfflineConfigError, match="num_teams"):
            offline.load_config(_write(tmp_path, _config(teams=["Team Ryan", "Team 2"])))

    def test_bad_draft_type(self, tmp_path):
        with pytest.raises(offline.OfflineConfigError, match="draft_type"):
            offline.load_config(_write(tmp_path, _config(draft_type="lottery")))

    def test_no_scoreable_scoring_keys(self, tmp_path):
        with pytest.raises(offline.OfflineConfigError, match="scoreable"):
            offline.load_config(_write(tmp_path, _config(scoring={"tackles": 1})))

    def test_missing_roster(self, tmp_path):
        config = _config()
        del config["roster"]
        with pytest.raises(offline.OfflineConfigError, match="roster"):
            offline.load_config(_write(tmp_path, config))

    def test_not_yaml_mapping(self, tmp_path):
        path = tmp_path / "league.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        with pytest.raises(offline.OfflineConfigError, match="mapping"):
            offline.load_config(path)


class TestPlayerPool:
    def _rows(self) -> list[SourceRow]:
        return [
            SourceRow(name="Josh Allen", position="QB", team="BUF", source="csv",
                      adp=28.0, projected_points=345.0, stats={"pass_yds": 3670}),
            SourceRow(name="Jahmyr Gibbs", position="RB", team="DET", source="csv",
                      adp=1.0, projected_points=276.0, stats={"rush_yds": 1200}),
            SourceRow(name="No ADP Guy", position="WR", team="CIN", source="csv",
                      projected_points=200.0, stats={"rec": 80}),
        ]

    def test_builds_a_pool(self):
        players = offline.players_from_rows(self._rows(), "offline.l.1")
        assert len(players) == 3
        assert all(p.player_key.startswith("offline.l.1.p.") for p in players)

    def test_ordered_by_adp_then_points(self):
        """fetch_rankings measures coverage against the first 200, so order matters."""
        players = offline.players_from_rows(self._rows(), "offline.l.1")
        assert [p.full_name for p in players] == ["Jahmyr Gibbs", "Josh Allen", "No ADP Guy"]

    def test_adp_is_carried_as_draft_analysis(self):
        players = offline.players_from_rows(self._rows(), "offline.l.1")
        assert players[0].draft_analysis.average_pick == 1.0

    def test_positions_survive(self):
        players = offline.players_from_rows(self._rows(), "offline.l.1")
        assert {p.primary_position for p in players} == {"QB", "RB", "WR"}


class TestDefenceSupplement:
    """Projection exports carry no DST, so a DEF slot would be unfillable."""

    def _pool(self):
        return offline.players_from_rows(
            [SourceRow(name="Josh Allen", position="QB", team="BUF", source="csv", adp=28.0)],
            "offline.l.1",
        )

    def test_defences_are_added_from_other_sources(self):
        rows = [
            SourceRow(name="Seattle Defense", position="DEF", team="SEA", source="ffc", adp=140.0),
            SourceRow(name="Dallas Defense", position="DEF", team="DAL", source="ffc", adp=150.0),
        ]
        players, notes = offline.supplement_positions(self._pool(), rows, "offline.l.1")
        assert sum(1 for p in players if p.primary_position == "DEF") == 2
        assert notes and "DEF" in notes[0]

    def test_the_same_defence_from_two_sources_is_one_player(self):
        """FFC says "Seattle Defense", FantasyPros says "Seattle Seahawks"."""
        rows = [
            SourceRow(name="Seattle Defense", position="DEF", team="SEA", source="ffc"),
            SourceRow(name="Seattle Seahawks", position="DEF", team="SEA", source="fantasypros"),
        ]
        players, _ = offline.supplement_positions(self._pool(), rows, "offline.l.1")
        assert sum(1 for p in players if p.primary_position == "DEF") == 1

    def test_no_op_when_the_position_is_already_covered(self):
        pool = offline.players_from_rows(
            [SourceRow(name="Seattle Defense", position="DEF", team="SEA", source="csv")],
            "offline.l.1",
        )
        rows = [SourceRow(name="Dallas Defense", position="DEF", team="DAL", source="ffc")]
        players, notes = offline.supplement_positions(pool, rows, "offline.l.1")
        assert len(players) == 1
        assert notes == []
