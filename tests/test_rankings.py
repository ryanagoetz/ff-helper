"""Tests for source parsing, the player crosswalk, and blending."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ff_helper.rankings.blend import blend
from ff_helper.rankings.players import (
    PlayerRegistry,
    SourceRow,
    normalize_name,
    normalize_position,
    normalize_team,
)
from ff_helper.rankings.sources import fantasypros, ffc, yahoo_adp
from ff_helper.yahoo.models import (
    STAT_REC,
    STAT_REC_TD,
    STAT_REC_YDS,
    STAT_RUSH_TD,
    STAT_RUSH_YDS,
    DraftAnalysis,
    LeagueSettings,
    RosterSlot,
    YahooPlayer,
)

FIXTURES = Path(__file__).parent / "fixtures"


def yahoo_player(key, name, position, team, adp=None) -> YahooPlayer:
    return YahooPlayer(
        player_key=key,
        player_id=key.split(".")[-1],
        full_name=name,
        team_abbr=team,
        display_position=position,
        eligible_positions=(position,),
        draft_analysis=DraftAnalysis(average_pick=adp),
    )


@pytest.fixture
def registry() -> PlayerRegistry:
    return PlayerRegistry(
        [
            yahoo_player("461.p.1", "Ja'Marr Chase", "WR", "Cin", adp=1.6),
            yahoo_player("461.p.2", "Kenneth Walker III", "RB", "Sea", adp=28.0),
            yahoo_player("461.p.3", "Travis Kelce", "TE", "KC", adp=58.0),
        ]
    )


@pytest.fixture
def league_settings() -> LeagueSettings:
    return LeagueSettings(
        roster_slots=(
            RosterSlot("QB", 1),
            RosterSlot("RB", 2),
            RosterSlot("WR", 2),
            RosterSlot("TE", 1),
            RosterSlot("W/R/T", 1),
            RosterSlot("BN", 6),
        ),
        stat_modifiers={
            STAT_RUSH_YDS: 0.1,
            STAT_RUSH_TD: 6.0,
            STAT_REC: 1.0,
            STAT_REC_YDS: 0.1,
            STAT_REC_TD: 6.0,
        },
        is_auction=False,
    )


class TestNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Ja'Marr Chase", "jamarr chase"),
            ("JaMarr Chase", "jamarr chase"),
            ("Kenneth Walker III", "kenneth walker"),
            ("Marvin Harrison Jr.", "marvin harrison"),
            ("D.J. Moore", "dj moore"),
            ("DJ Moore", "dj moore"),
            ("Amon-Ra St. Brown", "amonra st brown"),
        ],
    )
    def test_name_normalization(self, raw, expected):
        assert normalize_name(raw) == expected

    def test_suffix_only_stripped_when_it_is_a_suffix(self):
        # "V" is a suffix, but a lone-word name must not be emptied out.
        assert normalize_name("V") == "v"

    def test_team_aliases(self):
        assert normalize_team("JAC") == "JAX"
        assert normalize_team("Cin") == "CIN"
        assert normalize_team("WSH") == "WAS"

    def test_position_normalization(self):
        assert normalize_position("D/ST") == "DEF"
        assert normalize_position("DST") == "DEF"
        assert normalize_position("PK") == "K"
        # FantasyPros appends a positional rank in some tables.
        assert normalize_position("WR1") == "WR"


class TestCrosswalk:
    def test_exact_match(self, registry):
        row = SourceRow(name="Ja'Marr Chase", position="WR", team="CIN", source="ffc")
        assert registry.find(row).player_key == "461.p.1"

    def test_matches_truncated_first_name(self, registry):
        # "Ken Walker III" (FFC) vs "Kenneth Walker III" (Yahoo). These score only 0.83 on
        # raw string similarity -- below any threshold loose enough to stay safe -- so the
        # surname + compatible-first-name rule is what has to catch it.
        row = SourceRow(name="Ken Walker III", position="RB", team="SEA", source="ffc")
        assert registry.find(row).player_key == "461.p.2"

    def test_matches_substituted_nickname(self):
        registry = PlayerRegistry([yahoo_player("461.p.20", "Michael Pittman", "WR", "Ind")])
        row = SourceRow(name="Mike Pittman", position="WR", team="IND", source="ffc")
        assert registry.find(row).player_key == "461.p.20"

    def test_does_not_match_different_people_sharing_a_surname(self, registry):
        # The compatible-first-name rule must not collapse distinct players.
        registry = PlayerRegistry(
            [
                yahoo_player("461.p.30", "Josh Allen", "QB", "Buf"),
                yahoo_player("461.p.31", "Kyler Murray", "QB", "Ari"),
            ]
        )
        row = SourceRow(name="Brandon Allen", position="QB", team="SF", source="ffc")
        assert registry.find(row) is None

    def test_matches_first_initial_form(self, registry):
        row = SourceRow(name="K. Walker", position="RB", team="SEA", source="other")
        assert registry.find(row).player_key == "461.p.2"

    def test_unmatched_players_are_reported_not_dropped_silently(self, registry):
        rows = [
            SourceRow(name="Ja'Marr Chase", position="WR", team="CIN", source="ffc"),
            SourceRow(name="Totally Unknown", position="WR", team="FA", source="ffc"),
        ]
        grouped, report = registry.crosswalk(rows)
        assert len(grouped) == 1
        assert len(report.unmatched) == 1
        assert report.unmatched[0].name == "Totally Unknown"
        assert report.match_rate == 0.5

    def test_disambiguates_same_name_by_team(self):
        registry = PlayerRegistry(
            [
                yahoo_player("461.p.10", "Mike Williams", "WR", "NYJ"),
                yahoo_player("461.p.11", "Mike Williams", "WR", "LAC"),
            ]
        )
        row = SourceRow(name="Mike Williams", position="WR", team="LAC", source="ffc")
        assert registry.find(row).player_key == "461.p.11"


class TestFFC:
    def test_parses_adp_and_stdev(self):
        payload = json.loads((FIXTURES / "ffc_adp.json").read_text())
        rows = ffc.parse(payload)
        assert len(rows) == 4
        chase = rows[0]
        assert chase.name == "Ja'Marr Chase"
        assert chase.adp == 1.4
        assert chase.adp_stdev == 0.6
        assert chase.team == "CIN"

    def test_zero_stdev_is_treated_as_unknown(self):
        # A player drafted 12 times with stdev 0 is not a certainty; it is no data.
        # Passing 0 through would make the survival model infinitely confident.
        payload = json.loads((FIXTURES / "ffc_adp.json").read_text())
        rows = ffc.parse(payload)
        rare = next(row for row in rows if row.name == "Somebody Notinyahoo")
        assert rare.adp_stdev is None


class TestFantasyPros:
    def test_parses_embedded_ecr_json(self):
        html = (FIXTURES / "fantasypros_rankings.html").read_text()
        rows = fantasypros.parse_rankings(html)
        assert len(rows) == 3
        chase = rows[0]
        assert chase.ecr == 1
        assert chase.tier == 1
        assert chase.position == "WR"
        # rank_std rides along as ecr_std -- expert disagreement, an input to
        # projection variance -- and is never mistaken for an ADP stdev.
        assert chase.ecr_std == pytest.approx(0.5)
        assert chase.adp_stdev is None

    def test_raises_loudly_when_page_shape_changes(self):
        # Silently returning [] here would look like "no players are ranked", which the
        # blend would happily accept.
        with pytest.raises(fantasypros.ScrapeError):
            fantasypros.parse_rankings("<html><body>redesigned</body></html>")

    def test_parses_projection_stat_lines(self):
        html = (FIXTURES / "fantasypros_projections_rb.html").read_text()
        rows = fantasypros.parse_projections(html, "rb")
        assert len(rows) == 2
        walker = rows[0]
        assert walker.name == "Kenneth Walker III"
        assert walker.team == "SEA"
        # Thousands separators must survive parsing.
        assert walker.stats["rush_yds"] == 1080.5
        assert walker.stats["rec"] == 44.2
        assert walker.projected_points == 231.4

    def test_projection_rows_carry_stats_not_just_totals(self):
        # The whole point is re-scoring under the user's league, which needs stat lines.
        html = (FIXTURES / "fantasypros_projections_rb.html").read_text()
        rows = fantasypros.parse_projections(html, "rb")
        assert set(rows[0].stats) >= {"rush_att", "rush_yds", "rush_td", "rec", "rec_yds"}


class TestBlend:
    def _rows(self):
        payload = json.loads((FIXTURES / "ffc_adp.json").read_text())
        rows = ffc.parse(payload)
        rows += fantasypros.parse_rankings((FIXTURES / "fantasypros_rankings.html").read_text())
        rows += fantasypros.parse_projections(
            (FIXTURES / "fantasypros_projections_rb.html").read_text(), "rb"
        )
        return rows

    def test_projections_are_rescored_under_league_settings(self, registry, league_settings):
        grouped, _ = registry.crosswalk(self._rows())
        result = blend(registry, grouped, league_settings)
        walker = result.valuations["461.p.2"]

        # Full PPR: 108.05 rush yds + 50.4 rush TD + 44.2 rec + 32.01 rec yds + 9.6 rec TD.
        # Fumbles lost are projected but carry no modifier in these settings, so score 0.
        assert walker.projected_points == pytest.approx(244.26, abs=0.1)
        # Deliberately NOT FantasyPros' own 231.4 total, which assumed their scoring.
        assert walker.projected_points != pytest.approx(231.4, abs=0.1)

    def test_adp_blend_weights_yahoo_more_heavily(self, registry, league_settings):
        rows = self._rows() + yahoo_adp.from_players(registry.players)
        grouped, _ = registry.crosswalk(rows)
        result = blend(registry, grouped, league_settings)

        walker = result.valuations["461.p.2"]
        # Yahoo says 28.0, FFC says 26.2. A 0.65/0.35 blend lands nearer Yahoo.
        assert walker.adp == pytest.approx(0.65 * 28.0 + 0.35 * 26.2, abs=0.01)
        assert abs(walker.adp - 28.0) < abs(walker.adp - 26.2)

    def test_real_stdev_preferred_over_estimate(self, registry, league_settings):
        grouped, _ = registry.crosswalk(self._rows())
        result = blend(registry, grouped, league_settings)
        assert result.valuations["461.p.2"].adp_stdev == pytest.approx(5.1)

    def test_players_without_projections_are_interpolated_not_dropped(
        self, registry, league_settings
    ):
        # Chase and Kelce have rankings/ADP but no stat projections in these fixtures.
        grouped, _ = registry.crosswalk(self._rows())
        result = blend(registry, grouped, league_settings)

        assert "461.p.1" in result.valuations
        assert result.valuations["461.p.1"].points_estimated is True
        assert result.notes

    def test_tier_is_carried_through(self, registry, league_settings):
        grouped, _ = registry.crosswalk(self._rows())
        result = blend(registry, grouped, league_settings)
        assert result.valuations["461.p.2"].tier == 4

    def test_yahoo_auction_costs_scale_with_the_league_budget(self, registry, league_settings):
        rows = [
            SourceRow(
                name="Kenneth Walker III",
                position="RB",
                team="Sea",
                source="yahoo",
                auction_cost=40.0,
            ),
            SourceRow(
                name="Ja'Marr Chase", position="WR", team="Cin", source="csv", auction_cost=40.0
            ),
        ]
        grouped, _ = registry.crosswalk(rows)
        rich = LeagueSettings(
            roster_slots=league_settings.roster_slots,
            stat_modifiers=league_settings.stat_modifiers,
            is_auction=True,
            auction_budget=300,
        )
        result = blend(registry, grouped, rich)

        # Yahoo's average_cost is measured across default $200 rooms, so in a $300
        # league the same player's going rate is half again higher.
        assert result.valuations["461.p.2"].market_cost == pytest.approx(60.0)
        # A CSV cost is the user's own export for this league and is left alone.
        assert result.valuations["461.p.1"].market_cost == pytest.approx(40.0)


class TestValuationRefinements:
    """Injury availability, projection variance, and the snapshot bump that carries it."""

    def _one_player_registry(self, status: str = "") -> PlayerRegistry:
        hurt = YahooPlayer(
            player_key="461.p.9",
            player_id="9",
            full_name="Fragile Back",
            team_abbr="Chi",
            display_position="RB",
            eligible_positions=("RB",),
            status=status,
            draft_analysis=DraftAnalysis(average_pick=30.0),
        )
        return PlayerRegistry([hurt])

    def _stat_row(self, *, source="ffc", yards=1000.0, ecr_std=None) -> SourceRow:
        return SourceRow(
            name="Fragile Back",
            position="RB",
            team="CHI",
            source=source,
            adp=30.0,
            ecr_std=ecr_std,
            stats={"rush_yds": yards},
        )

    def _blend_one(self, registry, rows, league_settings):
        grouped, _ = registry.crosswalk(rows)
        return blend(registry, grouped, league_settings).valuations["461.p.9"]

    def test_injury_status_cuts_the_projection_by_expected_games(self, league_settings):
        registry = self._one_player_registry(status="IR")
        valuation = self._blend_one(registry, [self._stat_row()], league_settings)
        # 1000 rush yards = 100 points, scaled to the 7 of 17 games IR leaves him.
        assert valuation.availability == pytest.approx(7 / 17)
        assert valuation.projected_points == pytest.approx(100.0 * 7 / 17)

    def test_questionable_costs_nothing_at_blend_time(self, league_settings):
        registry = self._one_player_registry(status="Q")
        valuation = self._blend_one(registry, [self._stat_row()], league_settings)
        assert valuation.availability == 1.0
        assert valuation.projected_points == pytest.approx(100.0)

    def test_disagreeing_sources_produce_a_measured_points_stdev(self, league_settings):
        import statistics

        registry = self._one_player_registry()
        rows = [
            self._stat_row(source="ffc", yards=1000.0),
            self._stat_row(source="other", yards=1200.0),
        ]
        valuation = self._blend_one(registry, rows, league_settings)
        assert valuation.points_stdev == pytest.approx(statistics.stdev([100.0, 120.0]))

    def test_single_source_fallback_widens_with_expert_disagreement(self, league_settings):
        registry = self._one_player_registry()
        calm = self._blend_one(registry, [self._stat_row()], league_settings)
        argued = self._blend_one(
            registry, [self._stat_row(ecr_std=10.0)], league_settings
        )
        # Fallback is 12% of the projection; a rank_std twice the neutral 5.0 doubles it.
        assert calm.points_stdev == pytest.approx(12.0)
        assert argued.points_stdev == pytest.approx(24.0)

    def test_snapshot_v2_round_trips_ecr_std_and_refuses_v1(self, tmp_path):
        from ff_helper.rankings import cache

        snapshot = cache.Snapshot(
            league_key="461.l.9",
            fetched_at=0.0,
            rows=[self._stat_row(ecr_std=4.5)],
        )
        path = tmp_path / "snap.json"
        cache.save(snapshot, path)
        loaded = cache.load("461.l.9", path=path)
        assert loaded is not None
        assert loaded.rows[0].ecr_std == pytest.approx(4.5)

        # A version-1 snapshot predates ecr_std: refuse it whole (forcing a one-minute
        # re-fetch) rather than loading rows that silently lack the field.
        payload = json.loads(path.read_text())
        payload["version"] = 1
        path.write_text(json.dumps(payload))
        assert cache.load("461.l.9", path=path) is None
