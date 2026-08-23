"""Transport behaviour: retries, and the 401 that must not be retried."""

from __future__ import annotations

import httpx
import pytest

from ff_helper.config import Settings
from ff_helper.yahoo.auth import Token
from ff_helper.yahoo.client import NOT_APPROVED_PROBLEM, YahooAPIError, YahooClient


def settings() -> Settings:
    return Settings(
        client_id="id",
        client_secret="secret",
        redirect_uri="oob",
        league_key=None,
        poll_interval=2.0,
    )


def client_with(handler) -> tuple[YahooClient, list[int]]:
    """A client whose transport is a callable, plus a counter of requests made."""
    calls: list[int] = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return handler(request)

    token = Token(access_token="a", refresh_token="r", expires_at=1e12)
    client = YahooClient(settings(), token=token)
    client._http = httpx.Client(transport=httpx.MockTransport(counting))
    return client, calls


class TestApprovalFailure:
    def response(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            headers={
                "www-authenticate": f'OAuth oauth_problem="{NOT_APPROVED_PROBLEM}", '
                'realm="yahooapis.com"'
            },
            json={"error": {"description": "Please provide valid credentials."}},
        )

    def test_it_names_the_real_problem(self):
        client, _ = client_with(self.response)
        with client, pytest.raises(YahooAPIError) as caught:
            client.get("league/461.l.1")

        message = str(caught.value)
        assert NOT_APPROVED_PROBLEM in message
        assert "not your token" in message
        assert "sports.yahoo.com/developer/access" in message

    def test_it_fails_immediately_rather_than_retrying(self):
        """Four attempts at something that can never succeed only delays the answer.

        It also burns a token refresh per attempt, and the refresh is the one operation
        here with a rate limit worth respecting.
        """
        client, calls = client_with(self.response)
        with client, pytest.raises(YahooAPIError):
            client.get("league/461.l.1")
        assert len(calls) == 1


class TestOtherFailures:
    def test_a_plain_401_still_tries_a_refresh(self, monkeypatch):
        """An expired token looks identical from the outside, and does deserve a retry."""
        import ff_helper.yahoo.client as client_module

        refreshed: list[int] = []

        def fake_refresh(_settings, token):
            refreshed.append(1)
            return token

        monkeypatch.setattr(client_module, "refresh", fake_refresh)
        monkeypatch.setattr(Token, "save", lambda self: None)

        client, calls = client_with(lambda request: httpx.Response(401))
        with client, pytest.raises(YahooAPIError):
            client.get("league/461.l.1")

        assert len(calls) > 1
        assert refreshed

    def test_a_server_error_is_retried_then_reported(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _seconds: None)
        client, calls = client_with(lambda request: httpx.Response(503))
        with client, pytest.raises(YahooAPIError, match="503"):
            client.get("league/461.l.1")
        assert len(calls) > 1

    def test_a_404_is_not_retried(self):
        client, calls = client_with(lambda request: httpx.Response(404, text="nope"))
        with client, pytest.raises(YahooAPIError, match="404"):
            client.get("league/461.l.1")
        assert len(calls) == 1

    def test_a_good_response_comes_back_parsed(self):
        client, _ = client_with(lambda request: httpx.Response(200, json={"ok": True}))
        with client:
            assert client.get("league/461.l.1") == {"ok": True}

    def test_the_format_parameter_is_always_appended(self):
        seen: list[str] = []

        def capture(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={})

        client, _ = client_with(capture)
        with client:
            client.get("league/461.l.1")
            # Yahoo's own filters are matrix parameters on the path, not query string,
            # so this still needs the '?' form.
            client.get("league/461.l.1/players;start=0;count=25")
            client.get("league/461.l.1?already=set")

        assert seen[0].endswith("?format=json")
        assert seen[1].endswith("?format=json")
        assert seen[2].endswith("&format=json")
