"""The news bridge: vocabulary, extraction, storage, and what it does to a projection."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ff_helper.rankings.players import PlayerRegistry
from ff_helper.rankings.sources import weekly_csv
from ff_helper.season.news import flags as vocab
from ff_helper.season.news.extract import extract
from ff_helper.season.news.server import create_news_app
from ff_helper.season.news.store import Article, Caveat, NewsFlag, NewsStore, article_id_for
from ff_helper.season.valuation import weekly_blend
from ff_helper.yahoo.models import YahooPlayer
from tests.test_season import WEEKLY_CSV, weekly_settings

FIXTURES = Path(__file__).parent / "fixtures"
ARTICLES = json.loads((FIXTURES / "news_articles.json").read_text())

WEEK = 7
NOW = 1_760_000_000.0


def news_players() -> list[YahooPlayer]:
    """A pool with the traps in it: two Robinsons, a same-team RB pair, a QB for weather."""

    def player(key, name, team, position, bye=10):
        return YahooPlayer(
            player_key=key,
            player_id=key.rsplit(".", 1)[-1],
            full_name=name,
            team_abbr=team,
            display_position=position,
            eligible_positions=(position,),
            bye_week=bye,
        )

    return [
        player("461.p.1", "Ja'Marr Chase", "CIN", "WR"),
        player("461.p.2", "Kenneth Walker III", "SEA", "RB"),
        player("461.p.3", "Rome Odunze", "CHI", "WR"),
        player("461.p.4", "Harrison Butker", "KC", "K"),
        player("461.p.5", "Josh Allen", "BUF", "QB"),
        # The pair the crosswalk must refuse to choose between on "B. Robinson".
        player("461.p.6", "Bijan Robinson", "ATL", "RB"),
        player("461.p.9", "Brian Robinson Jr.", "WAS", "RB"),
        player("461.p.7", "Cole Kmet", "CHI", "TE"),
        # Same team and position as Odunze, so a ruled-out Odunze benefits him.
        player("461.p.8", "DJ Moore", "CHI", "WR"),
    ]


def registry_for() -> PlayerRegistry:
    return PlayerRegistry(news_players())


def positions_for() -> dict[str, str]:
    return {player.player_key: player.primary_position for player in news_players()}


def article_from(name: str, *, week: int = WEEK, captured_at: float = NOW) -> Article:
    spec = ARTICLES[name]
    return Article(
        article_id=article_id_for(spec["url"], spec["title"], captured_at),
        url=spec["url"],
        title=spec["title"],
        source="4for4.com",
        captured_at=captured_at,
        week=week,
        text=spec["text"],
    )


class TestVocabulary:
    def test_every_multiplier_is_a_constant_in_the_table(self):
        for name, spec in vocab.VOCABULARY.items():
            assert vocab.multiplier_for(name) == spec.multiplier
            assert vocab.TOTAL_FLOOR <= spec.multiplier <= vocab.TOTAL_CEILING

    def test_an_unknown_flag_is_loud_rather_than_pricing_as_one(self):
        with pytest.raises(vocab.UnknownFlag):
            vocab.multiplier_for("VIBES_ARE_BAD")

    def test_ruled_out_absorbs_everything(self):
        assert vocab.combine([vocab.RULED_OUT, vocab.LEAD_BACK, vocab.PROMOTED_TO_STARTER]) == 0.0

    def test_same_family_flags_do_not_stack(self):
        """One injury described twice must be charged for once, most pessimistically."""
        both = vocab.combine([vocab.QUESTIONABLE, vocab.LIMITED_SNAPS])
        assert both == pytest.approx(min(0.80, 0.75))

    def test_different_families_multiply(self):
        combined = vocab.combine([vocab.QUESTIONABLE, vocab.BACKFIELD_COMMITTEE])
        assert combined == pytest.approx(0.80 * 0.85)

    def test_the_product_is_clamped(self):
        # Two upside flags in different families would otherwise exceed the ceiling.
        combined = vocab.combine([vocab.LEAD_BACK, vocab.TEAMMATE_OUT_BENEFICIARY])
        assert combined == pytest.approx(min(1.20 * 1.15, vocab.TOTAL_CEILING))
        assert combined <= vocab.TOTAL_CEILING

    def test_conflicting_role_flags_take_the_pessimistic_one(self):
        combined = vocab.combine([vocab.LEAD_BACK, vocab.BACKFIELD_COMMITTEE])
        assert combined == pytest.approx(0.85)

    def test_dominant_reports_only_the_flags_that_survived(self):
        surviving = vocab.dominant([vocab.LEAD_BACK, vocab.BACKFIELD_COMMITTEE])
        assert surviving == [vocab.BACKFIELD_COMMITTEE]

    def test_no_flags_is_exactly_one(self):
        assert vocab.combine([]) == 1.0

    def test_the_derived_flag_has_no_text_triggers(self):
        """Nothing anybody writes should be able to produce it directly."""
        spec = vocab.VOCABULARY[vocab.TEAMMATE_OUT_BENEFICIARY]
        assert spec.derived_only is True
        assert spec.patterns == ()


class TestExtraction:
    @pytest.mark.parametrize("name", sorted(ARTICLES))
    def test_fixture_produces_exactly_the_expected_flags(self, name):
        spec = ARTICLES[name]
        found, _ = extract(article_from(name), registry_for(), position_of=positions_for())

        # Derived flags are asserted separately; these fixtures pin the read of the text.
        explicit = sorted(
            (flag.player_key, flag.flag)
            for flag in found
            if flag.confidence == "explicit"
        )
        expected = sorted(tuple(pair) for pair in spec["expect_flags"])
        assert explicit == expected, spec["why"]

    def test_every_flag_carries_the_sentence_that_caused_it(self):
        found, _ = extract(
            article_from("clean_ruled_out"), registry_for(), position_of=positions_for()
        )
        flag = found[0]
        assert "ruled out Ja'Marr Chase" in flag.quote
        assert flag.quote in ARTICLES["clean_ruled_out"]["text"]

    def test_a_negated_trigger_becomes_a_caveat_rather_than_vanishing(self):
        found, caveats = extract(
            article_from("negated_ruled_out"), registry_for(), position_of=positions_for()
        )
        assert found == []
        assert any("not been ruled out" in caveat.quote for caveat in caveats)

    def test_an_ambiguous_name_resolves_to_nobody(self):
        found, _ = extract(
            article_from("ambiguous_initial"), registry_for(), position_of=positions_for()
        )
        # Two Robinsons in the pool. Guessing would zero the wrong starter, confidently.
        assert found == []

    def test_weather_never_fires_from_prose(self):
        """A forecast sentence names no player, so nothing here can attribute it to one.

        The flag exists for a forecast source keyed to the game. Matching it from an
        article would mean attaching it to whoever happens to be mentioned nearby, and a
        mis-attributed weather flag quietly cuts a quarterback playing in a dome.
        """
        found, _ = extract(article_from("weather"), registry_for(), position_of=positions_for())
        assert found == []
        assert vocab.VOCABULARY[vocab.WEATHER_SEVERE].derived_only is True

    def test_boilerplate_produces_nothing_at_all(self):
        found, caveats = extract(
            article_from("boilerplate_only"), registry_for(), position_of=positions_for()
        )
        assert found == []
        assert caveats == []

    def test_analysis_with_no_reportable_fact_yields_no_flags(self):
        found, caveats = extract(
            article_from("pure_fluff"), registry_for(), position_of=positions_for()
        )
        assert found == []
        # It is still worth showing beside the recommendation.
        assert caveats

    def test_a_ruled_out_teammate_produces_an_inferred_beneficiary(self):
        found, _ = extract(
            article_from("two_players_one_sentence"),
            registry_for(),
            position_of=positions_for(),
        )
        derived = [flag for flag in found if flag.confidence == "inferred"]
        # Odunze (CHI WR) is out, so DJ Moore (CHI WR) benefits. Kmet is a TE and gets
        # nothing from a receiver's absence.
        assert [flag.player_key for flag in derived] == ["461.p.8"]
        assert derived[0].flag == vocab.TEAMMATE_OUT_BENEFICIARY
        assert derived[0].quote.startswith("[Rome Odunze ruled out]")

    def test_the_beneficiary_is_marked_inferred_not_explicit(self):
        found, _ = extract(
            article_from("two_players_one_sentence"),
            registry_for(),
            position_of=positions_for(),
        )
        beneficiary = next(f for f in found if f.flag == vocab.TEAMMATE_OUT_BENEFICIARY)
        # The article never mentions him. The reader deserves to know we joined two facts.
        assert beneficiary.confidence == "inferred"

    def test_one_flag_per_player_per_sentence(self):
        article = Article(
            article_id="x",
            url="",
            title="t",
            source="paste",
            captured_at=NOW,
            week=WEEK,
            text="Ja'Marr Chase is questionable and remains a game-time decision.",
        )
        found, _ = extract(article, registry_for(), position_of=positions_for())
        assert len(found) == 1


class TestStore:
    def store(self, tmp_path) -> NewsStore:
        return NewsStore(tmp_path / "news.jsonl")

    def flag(self, name: str, *, player="461.p.1", week=WEEK, captured_at=NOW) -> NewsFlag:
        return NewsFlag(
            article_id="a1",
            player_key=player,
            flag=name,
            quote="a sentence",
            week=week,
            captured_at=captured_at,
        )

    def test_a_flag_looks_its_multiplier_up_rather_than_accepting_one(self):
        flag = NewsFlag(
            article_id="a1",
            player_key="461.p.1",
            flag=vocab.DOUBTFUL,
            quote="q",
            week=WEEK,
            captured_at=NOW,
            multiplier=99.0,
        )
        assert flag.multiplier == pytest.approx(0.35)

    def test_an_unknown_flag_cannot_be_constructed(self):
        with pytest.raises(vocab.UnknownFlag):
            NewsFlag(
                article_id="a1",
                player_key="p",
                flag="MADE_UP",
                quote="q",
                week=WEEK,
                captured_at=NOW,
            )

    def test_round_trips_through_the_log(self, tmp_path):
        store = self.store(tmp_path)
        article = article_from("clean_ruled_out")
        store.append(article, [self.flag(vocab.RULED_OUT)], [])

        reloaded = NewsStore(store.path)
        reloaded_store = NewsStore.load("x", "y", path=store.path)
        assert reloaded_store.has_article(article.article_id)
        assert len(reloaded_store.flags) == 1
        assert reloaded_store.flags[0].multiplier == 0.0
        assert reloaded.path == store.path

    def test_re_posting_the_same_article_adds_nothing(self, tmp_path):
        store = self.store(tmp_path)
        article = article_from("clean_ruled_out")
        assert store.append(article, [self.flag(vocab.RULED_OUT)], []) is True
        assert store.append(article, [self.flag(vocab.RULED_OUT)], []) is False
        assert len(store.flags) == 1

    def test_the_same_url_captured_twice_is_one_article(self):
        first = article_id_for("https://x/y", "T", 1.0)
        second = article_id_for("https://x/y", "T", 9999.0)
        assert first == second

    def test_a_hand_paste_without_a_url_stays_distinct(self):
        first = article_id_for("", "T", 1.0)
        second = article_id_for("", "T", 2.0)
        assert first != second

    def test_expiry_is_a_read_filter_not_a_delete(self, tmp_path):
        store = self.store(tmp_path)
        store.append(article_from("clean_ruled_out"), [self.flag(vocab.RULED_OUT)], [])

        assert store.flags_for("461.p.1", WEEK)
        assert store.flags_for("461.p.1", WEEK + 1) == []
        # Still on disk, because the record is what makes the model checkable later.
        assert len(NewsStore.load("x", "y", path=store.path).flags) == 1

    def test_a_role_flag_outlives_an_injury_flag(self, tmp_path):
        store = self.store(tmp_path)
        store.append(
            article_from("committee"),
            [self.flag(vocab.BACKFIELD_COMMITTEE, player="461.p.2")],
            [],
        )
        assert store.flags_for("461.p.2", WEEK + 2)
        assert store.flags_for("461.p.2", WEEK + 3) == []

    def test_as_of_hides_news_captured_later(self, tmp_path):
        store = self.store(tmp_path)
        store.append(
            article_from("clean_ruled_out"),
            [self.flag(vocab.RULED_OUT, captured_at=NOW + 3600)],
            [],
        )
        assert store.flags_for("461.p.1", WEEK, as_of=NOW) == []
        assert store.flags_for("461.p.1", WEEK, as_of=NOW + 7200)

    def test_the_same_flag_from_two_articles_is_counted_once(self, tmp_path):
        store = self.store(tmp_path)
        store.append(
            article_from("questionable"),
            [self.flag(vocab.QUESTIONABLE, captured_at=NOW)],
            [],
        )
        store.append(
            article_from("clean_ruled_out"),
            [self.flag(vocab.QUESTIONABLE, captured_at=NOW + 60)],
            [],
        )
        live = store.flags_for("461.p.1", WEEK)
        assert len(live) == 1
        assert live[0].captured_at == NOW + 60  # the most recent wins

    def test_adjustment_splits_availability_from_role(self, tmp_path):
        store = self.store(tmp_path)
        store.append(
            article_from("questionable"),
            [
                self.flag(vocab.QUESTIONABLE),
                self.flag(vocab.BACKFIELD_COMMITTEE),
            ],
            [],
        )
        availability, role, live = store.adjustment_for("461.p.1", WEEK)
        assert availability == pytest.approx(0.80)
        assert role == pytest.approx(0.85)
        assert len(live) == 2

    def test_a_torn_last_line_does_not_cost_the_season(self, tmp_path):
        store = self.store(tmp_path)
        store.append(article_from("clean_ruled_out"), [self.flag(vocab.RULED_OUT)], [])
        with store.path.open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "flag", "data": {"player_ke')

        reloaded = NewsStore.load("x", "y", path=store.path)
        assert len(reloaded.flags) == 1

    def test_a_record_naming_a_retired_flag_is_skipped_not_fatal(self, tmp_path):
        store = self.store(tmp_path)
        store.append(article_from("clean_ruled_out"), [], [])
        with store.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "kind": "flag",
                        "data": {
                            "article_id": "a1",
                            "player_key": "p",
                            "flag": "RETIRED_FLAG",
                            "quote": "q",
                            "week": WEEK,
                            "captured_at": NOW,
                        },
                    }
                )
                + "\n"
            )
        reloaded = NewsStore.load("x", "y", path=store.path)
        assert reloaded.flags == []

    def test_a_long_quote_is_truncated(self, tmp_path):
        caveat = Caveat("a1", "p", "x" * 900, WEEK, NOW)
        assert len(caveat.quote) == 300


class TestAppliedToProjections:
    def blend(self, store: NewsStore | None, *, as_of: float | None = None):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=WEEK)
        return weekly_blend(
            registry_for(),
            projections,
            weekly_settings(),
            WEEK,
            news=store,
            as_of=as_of,
        )

    def stored(self, tmp_path, *names: str) -> NewsStore:
        store = NewsStore(tmp_path / "news.jsonl")
        for name in names:
            article = article_from(name)
            found, caveats = extract(article, registry_for(), position_of=positions_for())
            store.append(article, found, caveats)
        return store

    def test_a_ruled_out_player_projects_zero(self, tmp_path):
        store = self.stored(tmp_path, "clean_ruled_out")
        chase = self.blend(store).valuations["461.p.1"]

        assert chase.projected_points == 0.0
        assert chase.is_playable is False
        assert [a.flag for a in chase.adjustments] == [vocab.RULED_OUT]

    def test_the_causing_sentence_travels_with_the_number(self, tmp_path):
        store = self.stored(tmp_path, "questionable")
        chase = self.blend(store).valuations["461.p.1"]

        assert chase.adjustments
        adjustment = chase.adjustments[0]
        assert "questionable" in adjustment.quote.lower()
        assert adjustment.url == ARTICLES["questionable"]["url"]
        assert adjustment.multiplier == pytest.approx(0.80)

    def test_base_points_are_kept_so_the_change_is_printable(self, tmp_path):
        store = self.stored(tmp_path, "questionable")
        chase = self.blend(store).valuations["461.p.1"]

        assert chase.base_points == pytest.approx(16.89, abs=0.01)
        assert chase.projected_points == pytest.approx(16.89 * 0.80, abs=0.01)
        assert chase.effective_multiplier == pytest.approx(0.80, abs=0.001)

    def test_news_and_yahoo_do_not_double_count_one_injury(self, tmp_path):
        """Yahoo says Q, the article says questionable. One injury, charged once."""
        players = news_players()
        players[0] = YahooPlayer(
            player_key="461.p.1",
            player_id="1",
            full_name="Ja'Marr Chase",
            team_abbr="CIN",
            display_position="WR",
            eligible_positions=("WR",),
            bye_week=10,
            status="Q",
        )
        store = self.stored(tmp_path, "questionable")
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=WEEK)
        chase = weekly_blend(
            PlayerRegistry(players), projections, weekly_settings(), WEEK, news=store
        ).valuations["461.p.1"]

        # 0.80, not 0.80 * 0.80.
        assert chase.effective_multiplier == pytest.approx(0.80, abs=0.001)

    def test_yahoo_wins_the_floor(self, tmp_path):
        """An article cannot talk a player Yahoo has ruled out back into the lineup."""
        players = news_players()
        players[0] = YahooPlayer(
            player_key="461.p.1",
            player_id="1",
            full_name="Ja'Marr Chase",
            team_abbr="CIN",
            display_position="WR",
            eligible_positions=("WR",),
            bye_week=10,
            status="O",
        )
        store = self.stored(tmp_path, "questionable")
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=WEEK)
        chase = weekly_blend(
            PlayerRegistry(players), projections, weekly_settings(), WEEK, news=store
        ).valuations["461.p.1"]

        assert chase.projected_points == 0.0

    def test_a_role_flag_multiplies_on_top_of_availability(self, tmp_path):
        store = self.stored(tmp_path, "committee")
        walker = self.blend(store).valuations["461.p.2"]
        assert walker.effective_multiplier == pytest.approx(0.85, abs=0.001)

    def test_an_upside_flag_actually_raises_the_projection(self, tmp_path):
        store = self.stored(tmp_path, "promotion")
        with_news = self.blend(store).valuations["461.p.3"]
        without = self.blend(None).valuations["461.p.3"]

        assert with_news.projected_points > without.projected_points
        assert with_news.effective_multiplier == pytest.approx(1.30, abs=0.001)

    def test_no_store_means_nothing_moves(self, tmp_path):
        self.stored(tmp_path, "clean_ruled_out")
        chase = self.blend(None).valuations["461.p.1"]
        assert chase.adjustments == ()
        assert chase.projected_points == pytest.approx(16.89, abs=0.01)

    def test_as_of_replays_what_was_known_then(self, tmp_path):
        store = NewsStore(tmp_path / "news.jsonl")
        article = article_from("clean_ruled_out", captured_at=NOW + 7200)
        found, caveats = extract(article, registry_for(), position_of=positions_for())
        store.append(article, found, caveats)

        before = self.blend(store, as_of=NOW).valuations["461.p.1"]
        after = self.blend(store, as_of=NOW + 10800).valuations["461.p.1"]

        assert before.projected_points > 0.0
        assert after.projected_points == 0.0

    def test_a_bye_is_not_further_adjusted(self, tmp_path):
        store = self.stored(tmp_path, "committee")
        # Walker is on bye in week 7 in this pool, so the committee flag has nothing to
        # scale -- zero times anything is still zero, and a bye is not an injury.
        players = news_players()
        players[1] = YahooPlayer(
            player_key="461.p.2",
            player_id="2",
            full_name="Kenneth Walker III",
            team_abbr="SEA",
            display_position="RB",
            eligible_positions=("RB",),
            bye_week=WEEK,
        )
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=WEEK)
        walker = weekly_blend(
            PlayerRegistry(players), projections, weekly_settings(), WEEK, news=store
        ).valuations["461.p.2"]

        assert walker.is_bye is True
        assert walker.projected_points == 0.0
        assert walker.adjustments == ()


class TestNewsServer:
    def client(self, tmp_path, *, token: str = "") -> tuple[TestClient, NewsStore]:
        store = NewsStore(tmp_path / "news.jsonl")
        app = create_news_app(
            store, registry_for(), WEEK, bridge_token=token, position_of=positions_for()
        )
        return TestClient(app), store

    def test_a_paste_is_stored_and_reports_its_flags(self, tmp_path):
        client, store = self.client(tmp_path)
        spec = ARTICLES["clean_ruled_out"]
        response = client.post(
            "/api/news/paste",
            json={"text": spec["text"], "url": spec["url"], "title": spec["title"]},
        )

        body = response.json()
        assert body["stored"] is True
        assert [flag["player"] for flag in body["flags"]] == ["Ja'Marr Chase"]
        assert body["flags"][0]["flag"] == vocab.RULED_OUT
        assert "ruled out" in body["flags"][0]["quote"]
        assert len(store.flags) == 1

    def test_an_unresolvable_name_does_not_refuse_the_article(self, tmp_path):
        """The inverse of the draft bridge's asymmetry, and deliberately so."""
        client, store = self.client(tmp_path)
        text = (
            "Somebody Nobodyknows has been ruled out for Sunday. "
            "Ja'Marr Chase has also been ruled out for Sunday's game. "
            "The team will be short-handed at receiver for this important divisional game. "
            "The staff declined to offer a timeline for either player and said it would "
            "evaluate the position group again after the weekend, as it has all season."
        )
        body = client.post("/api/news/paste", json={"text": text, "url": "https://x/1"}).json()

        assert body["stored"] is True
        # The one that did resolve is kept rather than thrown away with the one that did not.
        assert [flag["player"] for flag in body["flags"]] == ["Ja'Marr Chase"]

    def test_a_short_paste_is_refused_without_storing(self, tmp_path):
        client, store = self.client(tmp_path)
        body = client.post("/api/news/paste", json={"text": "Chase is out."}).json()
        assert body["stored"] is False
        assert "too short" in body["reason"]
        assert store.is_empty

    def test_re_posting_the_same_url_is_idempotent(self, tmp_path):
        client, store = self.client(tmp_path)
        spec = ARTICLES["clean_ruled_out"]
        payload = {"text": spec["text"], "url": spec["url"], "title": spec["title"]}

        assert client.post("/api/news/paste", json=payload).json()["stored"] is True
        second = client.post("/api/news/paste", json=payload).json()
        assert second["stored"] is False
        assert second["reason"] == "already captured"
        assert len(store.flags) == 1

    def test_the_paste_page_is_served_same_origin(self, tmp_path):
        client, _ = self.client(tmp_path)
        response = client.get("/")
        assert response.status_code == 200
        assert "Paste an article" in response.text
        assert f"week {WEEK}" in response.text

    def test_an_external_origin_without_a_token_is_refused(self, tmp_path):
        client, store = self.client(tmp_path, token="secret")
        response = client.post(
            "/api/news/paste",
            json={"text": "x" * 400},
            headers={"Origin": "https://www.4for4.com"},
        )
        assert response.status_code == 401
        assert store.is_empty

    def test_an_external_origin_with_the_token_is_accepted(self, tmp_path):
        client, _ = self.client(tmp_path, token="secret")
        spec = ARTICLES["clean_ruled_out"]
        response = client.post(
            "/api/news/paste",
            json={"text": spec["text"], "url": spec["url"]},
            headers={"Origin": "https://www.4for4.com", "X-Bridge-Token": "secret"},
        )
        assert response.status_code == 200
        assert response.json()["stored"] is True

    def test_a_local_request_needs_no_token(self, tmp_path):
        client, _ = self.client(tmp_path, token="secret")
        spec = ARTICLES["questionable"]
        response = client.post(
            "/api/news/paste", json={"text": spec["text"], "url": spec["url"]}
        )
        assert response.status_code == 200

    def test_state_reports_what_is_stored(self, tmp_path):
        client, _ = self.client(tmp_path)
        spec = ARTICLES["clean_ruled_out"]
        client.post("/api/news/paste", json={"text": spec["text"], "url": spec["url"]})

        state = client.get("/api/news/state").json()
        assert state["week"] == WEEK
        assert state["articles"] == 1
        assert state["players_with_news"] == ["461.p.1"]

    def test_the_captured_time_is_taken_from_the_client(self, tmp_path):
        client, store = self.client(tmp_path)
        spec = ARTICLES["questionable"]
        client.post(
            "/api/news/paste",
            json={"text": spec["text"], "url": spec["url"], "captured_at": NOW},
        )
        assert store.flags[0].captured_at == NOW
        assert store.flags[0].age_hours(now=NOW + 7200) == pytest.approx(2.0)


class TestQuoteHygiene:
    def test_a_quote_never_exceeds_the_cap(self):
        long_sentence = "Ja'Marr Chase " + "very " * 200 + "is ruled out"
        article = Article(
            article_id="x",
            url="",
            title="t",
            source="paste",
            captured_at=time.time(),
            week=WEEK,
            text=long_sentence,
        )
        found, _ = extract(article, registry_for(), position_of=positions_for())
        assert found
        assert len(found[0].quote) <= 300


class TestNewsIsAttributedHonestly:
    """News must be credited with what it did, not with what Yahoo had already done."""

    def hurt_pool(self) -> list[YahooPlayer]:
        players = news_players()
        players[2] = YahooPlayer(
            player_key="461.p.3",
            player_id="3",
            full_name="Rome Odunze",
            team_abbr="CHI",
            display_position="WR",
            eligible_positions=("WR",),
            bye_week=10,
            status="Q",
        )
        return players

    def blend_with(self, players, store):
        projections, _ = weekly_csv.load(WEEKLY_CSV, week=WEEK)
        return weekly_blend(
            PlayerRegistry(players), projections, weekly_settings(), WEEK, news=store
        )

    def stored(self, tmp_path, name: str) -> NewsStore:
        store = NewsStore(tmp_path / "news.jsonl")
        article = article_from(name)
        found, caveats = extract(article, registry_for(), position_of=positions_for())
        store.append(article, found, caveats)
        return store

    def test_a_role_flag_is_measured_against_the_post_status_number(self, tmp_path):
        store = self.stored(tmp_path, "promotion")
        odunze = self.blend_with(self.hurt_pool(), store).valuations["461.p.3"]

        # Yahoo's Q already cut him to 0.80 of the stat line; the promotion is worth 1.30
        # on top. Reporting the whole 0.80 * 1.30 span as "what the news did" would be a
        # claim about the article that the article does not make.
        assert odunze.pre_news_points == pytest.approx(odunze.base_points * 0.80, abs=0.01)
        assert odunze.news_multiplier == pytest.approx(1.30, abs=0.001)
        assert odunze.effective_multiplier == pytest.approx(0.80 * 1.30, abs=0.001)

    def test_an_article_repeating_yahoo_gets_credited_with_nothing(self, tmp_path):
        """Yahoo says Q, the article says questionable. The article changed no number."""
        players = news_players()
        players[0] = YahooPlayer(
            player_key="461.p.1",
            player_id="1",
            full_name="Ja'Marr Chase",
            team_abbr="CIN",
            display_position="WR",
            eligible_positions=("WR",),
            bye_week=10,
            status="Q",
        )
        store = self.stored(tmp_path, "questionable")
        chase = self.blend_with(players, store).valuations["461.p.1"]

        assert chase.news_multiplier == pytest.approx(1.0, abs=0.001)
        assert chase.effective_multiplier == pytest.approx(0.80, abs=0.001)
        # It still shows up, because the sentence is worth reading even when the number
        # did not move.
        assert chase.adjustments

    def test_with_no_yahoo_status_the_two_multipliers_agree(self, tmp_path):
        store = self.stored(tmp_path, "questionable")
        chase = self.blend_with(news_players(), store).valuations["461.p.1"]
        assert chase.news_multiplier == pytest.approx(chase.effective_multiplier, abs=0.001)
