"""Tests for the AlphaSense client (auth, search parsing, helper shapes)."""
from __future__ import annotations

import json

import httpx
import pytest

from calorch.alphasense import AlphaSenseClient, _excerpt


def _client(handler) -> AlphaSenseClient:
    c = AlphaSenseClient(
        api_key="k", client_id="cid", client_secret="sec",
        username="u@x.com", password="pw", base_url="https://api.alpha-sense.com",
    )
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_requires_all_credentials():
    with pytest.raises(ValueError):
        AlphaSenseClient(api_key="", client_id="c", client_secret="s", username="u", password="p")


def test_auth_then_search_parses_documents():
    calls = {"auth": 0, "gql": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/auth":
            calls["auth"] += 1
            assert req.headers["x-api-key"] == "k"
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        if req.url.path == "/gql":
            calls["gql"] += 1
            assert req.headers["Authorization"] == "Bearer tok-1"
            body = json.loads(req.content)
            assert "AAPL" in body["variables"]["filter"]["keyword"]["query"]
            return httpx.Response(200, json={"data": {"search": {"documents": [
                {"id": "d1", "title": "AAPL Q2 transcript", "releasedAt": "2026-05-01",
                 "type": "TRANSCRIPT", "company": {"name": "Apple", "ticker": "AAPL"},
                 "sentiment": {"score": 0.4}},
            ], "cursor": None}}})
        return httpx.Response(404)

    c = _client(handler)
    docs = c.search("AAPL guidance")
    assert docs[0]["id"] == "d1"
    # token is cached — a second search reuses it
    c.search("AAPL outlook")
    assert calls["auth"] == 1 and calls["gql"] == 2


def test_search_returns_empty_on_graphql_errors():
    def handler(req):
        if req.url.path == "/auth":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"errors": [{"message": "bad filter"}]})

    assert _client(handler).search("AAPL") == []


def test_search_empty_query_short_circuits():
    def handler(req):  # should never be called
        raise AssertionError("no HTTP for empty query")

    assert _client(handler).search("   ") == []


def test_sentiment_aggregates_scores():
    def handler(req):
        if req.url.path == "/auth":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"data": {"search": {"documents": [
            {"id": "1", "sentiment": {"score": 0.6}},
            {"id": "2", "sentiment": {"score": -0.2}},
            {"id": "3", "sentiment": {}},  # no score — ignored
        ]}}})

    s = _client(handler).sentiment("AAPL")
    assert s["sample"] == 2
    assert s["mean_sentiment"] == pytest.approx(0.2)
    assert s["label"] == "positive"


def test_sentiment_no_scores_is_none():
    def handler(req):
        if req.url.path == "/auth":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"data": {"search": {"documents": []}}})

    s = _client(handler).sentiment("AAPL")
    assert s["mean_sentiment"] is None and s["sample"] == 0


def test_excerpt_normalises_document():
    doc = {"id": "x", "title": "T", "releasedAt": "2026-01-02", "type": "TRANSCRIPT",
           "company": {"name": "Apple", "ticker": "AAPL"}, "sentiment": {"score": 0.3}}
    e = _excerpt(doc)
    assert e == {
        "title": "T", "date": "2026-01-02", "type": "TRANSCRIPT", "company": "Apple",
        "ticker": "AAPL", "sentiment": 0.3, "source": "alphasense", "doc_id": "x",
    }
