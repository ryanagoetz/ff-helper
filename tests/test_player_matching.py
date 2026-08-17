"""Regression tests for player identity.

A missed match drops a player silently. A *wrong* match is worse: it merges two players
into one valuation, averaging their projections and their ADP, and the result looks
entirely plausible on the board. Both failure modes are covered here.
"""

from __future__ import annotations

from ff_helper.rankings.players import (
    PlayerRegistry,
    SourceRow,
    is_initial_form,
    name_variants,
)
from ff_helper.yahoo.models import YahooPlayer


def _player(key: str, name: str, position: str, team: str) -> YahooPlayer:
    return YahooPlayer(
        player_key=key,
        player_id=key,
        full_name=name,
        team_abbr=team,
        display_position=position,
        eligible_positions=(position,),
    )


# Both are Atlanta running backs whose names differ by one letter, so neither position
# nor team can separate them. This pair broke the board in August 2026.
ROBINSONS = [
    _player("p.1", "Bijan Robinson", "RB", "ATL"),
    _player("p.2", "Brian Robinson", "RB", "ATL"),
]


class TestNameVariants:
    def test_full_name_comes_first(self):
        """Order is load-bearing: an abbreviation must never be tried before the name."""
        variants = name_variants("Bijan Robinson")
        assert variants[0] == "bijan robinson"

    def test_initial_form_is_offered_but_last(self):
        variants = name_variants("Kenneth Walker")
        assert variants[-1] == "k walker"

    def test_middle_name_drop_ranks_above_the_initial(self):
        """Dropping a middle name still identifies a person; reducing to an initial does not."""
        variants = name_variants("Amon-Ra St. Brown")
        assert variants.index("amonra brown") < variants.index("a st brown")

    def test_generational_suffixes_are_normalized_away(self):
        assert name_variants("Kenneth Walker III")[0] == "kenneth walker"

    def test_is_initial_form(self):
        assert is_initial_form("b robinson") is True
        assert is_initial_form("bijan robinson") is False


class TestTheRobinsonCollision:
    """The bug: "b robinson" matched both, and whichever was indexed first won."""

    def test_each_robinson_resolves_to_himself(self):
        registry = PlayerRegistry(ROBINSONS)
        for name in ("Bijan Robinson", "Brian Robinson"):
            row = SourceRow(name=name, position="RB", team="ATL", source="t")
            assert registry.find(row).full_name == name

    def test_order_of_the_pool_does_not_change_the_answer(self):
        """The original failure depended on which player happened to be indexed first."""
        for pool in (ROBINSONS, list(reversed(ROBINSONS))):
            registry = PlayerRegistry(pool)
            row = SourceRow(name="Brian Robinson", position="RB", team="ATL", source="t")
            assert registry.find(row).full_name == "Brian Robinson"

    def test_they_do_not_share_a_crosswalk_group(self):
        registry = PlayerRegistry(ROBINSONS)
        rows = [
            SourceRow(name="Bijan Robinson", position="RB", team="ATL", source="csv", adp=2.0),
            SourceRow(name="Brian Robinson", position="RB", team="ATL", source="csv", adp=155.0),
        ]
        grouped, _ = registry.crosswalk(rows)
        assert len(grouped) == 2
        assert all(len(group) == 1 for group in grouped.values())

    def test_an_ambiguous_initial_does_not_pick_a_winner(self):
        """"B. Robinson" genuinely cannot be resolved, so it must not be guessed."""
        registry = PlayerRegistry(ROBINSONS)
        row = SourceRow(name="B Robinson", position="RB", team="ATL", source="t")
        assert registry.find(row) is None


class TestInitialsStillWorkWhenUnambiguous:
    def test_initial_matches_a_lone_candidate(self):
        registry = PlayerRegistry([_player("p.1", "Kenneth Walker", "RB", "SEA")])
        row = SourceRow(name="K. Walker", position="RB", team="SEA", source="t")
        assert registry.find(row).full_name == "Kenneth Walker"

    def test_truncated_first_name_still_matches(self):
        """The surname-plus-compatible-first-name rule, which initials do not cover."""
        registry = PlayerRegistry([_player("p.1", "Kenneth Walker", "RB", "SEA")])
        row = SourceRow(name="Ken Walker III", position="RB", team="SEA", source="t")
        assert registry.find(row).full_name == "Kenneth Walker"

    def test_same_name_different_team_is_split_by_team(self):
        pool = [
            _player("p.1", "Mike Williams", "WR", "NYJ"),
            _player("p.2", "Mike Williams", "WR", "LAC"),
        ]
        registry = PlayerRegistry(pool)
        row = SourceRow(name="Mike Williams", position="WR", team="LAC", source="t")
        assert registry.find(row).player_key == "p.2"


class TestTeamDefences:
    """A defense is identified by its team; every source spells the name differently."""

    def _pool(self):
        return [
            _player("d.1", "Seattle Defense", "DEF", "SEA"),
            _player("d.2", "Dallas Defense", "DEF", "DAL"),
        ]

    def test_matches_across_wildly_different_names(self):
        registry = PlayerRegistry(self._pool())
        for name in ("Seattle Seahawks", "Seahawks", "Seattle D/ST", "Seattle Defense"):
            row = SourceRow(name=name, position="DEF", team="SEA", source="t")
            assert registry.find(row).player_key == "d.1", name

    def test_team_aliases_still_resolve(self):
        registry = PlayerRegistry([_player("d.1", "Jacksonville Defense", "DEF", "JAX")])
        row = SourceRow(name="Jaguars", position="DEF", team="JAC", source="t")
        assert registry.find(row).player_key == "d.1"

    def test_a_shared_team_abbreviation_is_not_resolved_by_team(self):
        """Two defenses on one abbreviation means the team identifies nobody."""
        pool = [
            _player("d.1", "Alpha Defense", "DEF", "FA"),
            _player("d.2", "Beta Defense", "DEF", "FA"),
        ]
        registry = PlayerRegistry(pool)
        row = SourceRow(name="Beta Defense", position="DEF", team="FA", source="t")
        # Falls through to name matching rather than collapsing onto the first.
        assert registry.find(row).player_key == "d.2"

    def test_a_defence_without_a_team_falls_back_to_the_name(self):
        registry = PlayerRegistry(self._pool())
        row = SourceRow(name="Dallas Defense", position="DEF", team="", source="t")
        assert registry.find(row).player_key == "d.2"

    def test_skill_players_are_unaffected(self):
        registry = PlayerRegistry([_player("p.1", "Kenneth Walker", "RB", "SEA")])
        row = SourceRow(name="Someone Else", position="RB", team="SEA", source="t")
        assert registry.find(row) is None
