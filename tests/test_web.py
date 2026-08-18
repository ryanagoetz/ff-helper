"""End-to-end tests over a synthetic league.

These are the closest thing to a live run available offline: a full player pool, real
league settings, the actual blend/replacement/VONA path, the draft board, and the HTTP
layer. Everything except the network is exercised.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from ff_helper.assistant import Assistant
from ff_helper.draft.state import DraftState
from ff_helper.rankings.cache import Snapshot
from ff_helper.rankings.players import SourceRow
from ff_helper.web.app import create_app
from ff_helper.yahoo.models import (
    STAT_REC,
    STAT_REC_TD,
    STAT_REC_YDS,
    STAT_RUSH_TD,
    STAT_RUSH_YDS,
    DraftAnalysis,
    DraftPick,
    League,
    LeagueSettings,
    RosterSlot,
    Team,
    YahooPlayer,
)

NUM_TEAMS = 12
MY_SLOT = 5

# A plausible positional shape: plenty of WR/RB, a steep TE cliff, flat QBs.
POSITION_POOL = {
    "QB": (18, 300.0, 6.0),
    "RB": (60, 290.0, 4.2),
    "WR": (70, 285.0, 3.6),
    "TE": (20, 230.0, 11.0),
    "K": (14, 0.0, 0.0),
    "DEF": (14, 0.0, 0.0),
}


def build_snapshot() -> Snapshot:
    players: list[YahooPlayer] = []
    rows: list[SourceRow] = []
    adp_counter = 1.0

    # Interleave positions so ADP order looks like a real board rather than blocks.
    buckets = {pos: [] for pos in POSITION_POOL}
    for position, (count, top, decay) in POSITION_POOL.items():
        for index in range(count):
            buckets[position].append((index, top - index * decay))

    ordering: list[tuple[str, int, float]] = []
    for position in ("RB", "WR", "QB", "TE"):
        for index, points in buckets[position]:
            ordering.append((position, index, points))
    ordering.sort(key=lambda item: -item[2])
    for position in ("K", "DEF"):
        ordering.extend((position, index, points) for index, points in buckets[position])

    for position, index, points in ordering:
        key = f"461.p.{position}{index}"
        adp = adp_counter
        adp_counter += 1.0
        players.append(
            YahooPlayer(
                player_key=key,
                player_id=f"{position}{index}",
                full_name=f"{position} Player{index}",
                team_abbr="FA",
                display_position=position,
                eligible_positions=(position,),
                bye_week=(index % 14) + 4,
                draft_analysis=DraftAnalysis(average_pick=adp),
            )
        )
        rows.append(
            SourceRow(
                name=f"{position} Player{index}",
                position=position,
                team="FA",
                source="ffc",
                adp=adp,
                adp_stdev=max(1.5, adp * 0.3),
                ecr=adp,
                tier=index // 5 + 1,
            )
        )
        if position in {"QB", "RB", "WR", "TE"}:
            # Give the skill positions real stat lines so scoring has something to work on.
            rows.append(
                SourceRow(
                    name=f"{position} Player{index}",
                    position=position,
                    team="FA",
                    source="fantasypros",
                    stats={
                        "rush_yds": points * 2.0 if position == "RB" else 0.0,
                        "rush_td": points / 40.0 if position == "RB" else 0.0,
                        "rec": points / 3.0 if position in {"WR", "TE", "RB"} else 0.0,
                        "rec_yds": points * 2.4 if position in {"WR", "TE"} else 0.0,
                        "rec_td": points / 35.0 if position in {"WR", "TE"} else 0.0,
                    },
                )
            )

    return Snapshot(league_key="461.l.1", fetched_at=0.0, players=players, rows=rows)


def build_league() -> League:
    settings = LeagueSettings(
        roster_slots=(
            RosterSlot("QB", 1),
            RosterSlot("RB", 2),
            RosterSlot("WR", 2),
            RosterSlot("TE", 1),
            RosterSlot("W/R/T", 1),
            RosterSlot("K", 1),
            RosterSlot("DEF", 1),
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
    return League(
        league_key="461.l.1",
        league_id="1",
        name="Synthetic League",
        num_teams=NUM_TEAMS,
        season="2026",
        draft_status="drafting",
        scoring_type="head",
        settings=settings,
    )


@pytest.fixture
def assistant() -> Assistant:
    league = build_league()
    teams = [
        Team(
            team_key=f"461.l.1.t.{i}",
            team_id=str(i),
            name=f"Team {i}",
            is_mine=(i == MY_SLOT),
            draft_position=i,
        )
        for i in range(1, NUM_TEAMS + 1)
    ]
    state = DraftState(league=league, teams=teams)
    return Assistant.build(league, state, build_snapshot(), lock=threading.Lock())


@pytest.fixture
def client(assistant) -> TestClient:
    return TestClient(create_app(assistant, sync=None))


class TestAssistantWiring:
    def test_every_player_gets_a_valuation(self, assistant):
        expected = sum(count for count, _, _ in POSITION_POOL.values())
        assert len(assistant.valuations.valuations) == expected

    def test_kickers_and_defenses_survive_without_projections(self, assistant):
        # They have no stat lines, so they must come through the interpolation path
        # rather than being dropped -- you do have to draft them eventually.
        positions = {v.position for v in assistant.valuations.valuations.values()}
        assert {"K", "DEF"} <= positions

    def test_replacement_levels_computed_for_each_position(self, assistant):
        assert {"QB", "RB", "WR", "TE"} <= set(assistant.levels.points)

    def test_recommendations_are_ordered_and_explained(self, assistant):
        picks = assistant.recommendations(limit=5)
        assert len(picks) == 5
        scores = [pick.score for pick in picks]
        assert scores == sorted(scores, reverse=True)
        assert all(pick.reason for pick in picks)

    def test_drafted_players_stop_being_recommended(self, assistant):
        first = assistant.recommendations(limit=1)[0]
        assistant.state.record_manual(first.valuation.player_key)
        assert all(
            pick.valuation.player_key != first.valuation.player_key
            for pick in assistant.recommendations(limit=8)
        )

    def test_ranking_is_stable_as_my_turn_approaches(self, assistant):
        """The board must not lurch just because the clock moved.

        Anchoring the VONA horizon to whoever is on the clock would make pick 4 a
        near-pure-value ranking (next pick one away, everything survives) that flips to a
        scarcity ranking at pick 5. The horizon follows my turn instead, so the advice
        only refines as players come off the board.
        """
        seen: list[list[str]] = []
        for _ in range(MY_SLOT - 1):
            seen.append([pick.name for pick in assistant.recommendations(limit=5)])
            # Someone else picks a player the engine was not recommending to me.
            pool = assistant.available()
            assistant.state.record_manual(pool[-1].player_key)

        seen.append([pick.name for pick in assistant.recommendations(limit=5)])
        assert assistant.state.is_my_turn
        # The top pick should be the same player throughout, not a different one the
        # moment the turn arrives.
        assert len({names[0] for names in seen}) == 1

    def test_a_tight_end_surfaces_when_waiting_starts_to_cost(self, assistant):
        """The plan defers a safe TE, then surfaces him once deferring stops being free.

        At pick 1 every tight end survives to my next several turns, so burning an early
        pick on one is a reach and none belongs near the top of the board. Deep into the
        draft, with the skill pools thinned and my TE slot still open, the best one left
        must rise to the top -- raw points alone would never put him there.
        """
        state = assistant.state
        early = assistant.recommendations(limit=12)
        assert all(pick.position != "TE" for pick in early)

        # The room drafts to ADP but leaves every tight end on the board.
        ordered = sorted(assistant.valuations.valuations.values(), key=lambda v: v.adp)
        number = 0
        for valuation in ordered:
            if number >= 42:
                break
            if valuation.position == "TE":
                continue
            number += 1
            state.record_manual(
                valuation.player_key,
                pick=number,
                team_key=state.team_for_pick(number).team_key,
            )

        picks = assistant.recommendations(limit=3)
        best_te = next((pick for pick in picks if pick.position == "TE"), None)
        assert best_te is not None
        assert best_te.vona > 0

    def test_opponent_rosters_shape_position_demand(self, assistant):
        """Once every rival has a quarterback, the room's QB demand collapses.

        Demand is what lets survival say "he'll still be there": ADP thinks every room
        needs everything, but a rival with a QB is not taking another one as a starter.
        """
        state = assistant.state
        qb_keys = [
            key for key, value in assistant.valuations.valuations.items() if value.position == "QB"
        ]
        rivals = [team for team in state.teams if not team.is_mine]
        for offset, (team, key) in enumerate(zip(rivals, qb_keys, strict=False)):
            state.manual[100 + offset] = DraftPick(
                pick=100 + offset, round=9, team_key=team.team_key, player_key=key
            )

        current = state.current_pick
        target = next(pick for pick in state.my_picks if pick >= current)
        futures = [pick for pick in state.my_picks if pick > target]
        demand = assistant._position_demand(current, futures, assistant.position_of)

        first = futures[0]
        assert demand[first]["QB"] == 0.0
        # Nobody has a tight end yet, so TE demand is untouched.
        assert demand[first]["TE"] == 1.0

    def test_roster_needs_shift_recommendations(self, assistant):
        # Put four running backs on my roster, then check the engine stops pushing them.
        rb_keys = [
            key for key, value in assistant.valuations.valuations.items() if value.position == "RB"
        ][:4]
        for offset, key in enumerate(rb_keys):
            pick_number = 100 + offset
            assistant.state.manual[pick_number] = DraftPick(
                pick=pick_number,
                round=9,
                team_key=f"461.l.1.t.{MY_SLOT}",
                player_key=key,
            )

        counts = assistant.state.my_roster_counts(assistant.position_of)
        assert counts.get("RB") == 4
        top = assistant.recommendations(limit=1)[0]
        assert top.position != "RB"


class TestAPI:
    def test_index_serves_the_page(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "ff-helper" in response.text

    def test_state_endpoint(self, client):
        payload = client.get("/api/state").json()
        assert payload["current_pick"] == 1
        assert payload["my_slot"] == MY_SLOT
        assert payload["league"] == "Synthetic League"
        # 15 roster spots (9 starters + 6 bench) x 12 teams.
        assert payload["total_picks"] == NUM_TEAMS * 15
        assert payload["round"] == 1

    def test_recommend_endpoint_shape(self, client):
        payload = client.get("/api/recommend?limit=3").json()
        assert len(payload["recommendations"]) == 3
        first = payload["recommendations"][0]
        assert {"name", "position", "vor", "vona", "adp", "survival", "reason"} <= set(first)

    def test_search_only_returns_undrafted_players(self, client):
        results = client.get("/api/search?q=RB Player0").json()["results"]
        assert results
        target = results[0]["player_key"]

        client.post("/api/pick", json={"player_key": target})
        after = client.get("/api/search?q=RB Player0").json()["results"]
        assert all(row["player_key"] != target for row in after)

    def test_manual_pick_advances_the_board(self, client):
        before = client.get("/api/state").json()["current_pick"]
        target = client.get("/api/recommend?limit=1").json()["recommendations"][0]
        client.post("/api/pick", json={"player_key": target["player_key"]})
        assert client.get("/api/state").json()["current_pick"] == before + 1

    def test_undo_reverses_a_manual_pick(self, client):
        target = client.get("/api/recommend?limit=1").json()["recommendations"][0]
        client.post("/api/pick", json={"player_key": target["player_key"]})
        assert client.post("/api/undo").status_code == 200
        assert client.get("/api/state").json()["current_pick"] == 1

    def test_undo_with_nothing_to_undo_is_a_clean_error(self, client):
        assert client.post("/api/undo").status_code == 400

    def test_unknown_player_is_rejected(self, client):
        response = client.post("/api/pick", json={"player_key": "461.p.NOPE"})
        assert response.status_code == 404

    def test_my_turn_is_reported_at_my_slot(self, client):
        for _ in range(MY_SLOT - 1):
            target = client.get("/api/recommend?limit=1").json()["recommendations"][0]
            client.post("/api/pick", json={"player_key": target["player_key"]})

        payload = client.get("/api/state").json()
        assert payload["current_pick"] == MY_SLOT
        assert payload["is_my_turn"] is True
        # Slot 5 in a 12-team snake picks again at 20.
        assert payload["next_pick"] == 20


class TestFullDraftSimulation:
    def test_a_whole_draft_runs_without_breaking(self, assistant):
        """Drive every pick through the engine, as a live draft would.

        This is the closest offline proxy for draft day: it exercises the recommendation
        path at every pick, including the late rounds where the pool thins out and the
        interpolated K/DEF valuations come into play.
        """
        state = assistant.state
        total = state.total_picks
        seen: set[str] = set()

        for _ in range(total):
            picks = assistant.recommendations(limit=1)
            if not picks:
                break
            chosen = picks[0].valuation.player_key
            assert chosen not in seen, "a drafted player was recommended again"
            seen.add(chosen)
            state.record_manual(chosen)

        assert state.picks_made == total
        assert len(seen) == total

    def test_late_draft_still_produces_reasons(self, assistant):
        state = assistant.state
        for _ in range(state.total_picks - 1):
            picks = assistant.recommendations(limit=1)
            if not picks:
                break
            state.record_manual(picks[0].valuation.player_key)

        final = assistant.recommendations(limit=1)
        assert final and final[0].reason
