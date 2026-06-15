"""AlphaSense API client — qualitative market intelligence.

AlphaSense indexes SEC filings, earnings-call & expert-call transcripts,
broker research and news, with document-level sentiment. calorch uses it
for the *qualitative* side of a brief — guidance excerpts, transcript /
expert-call search, and sentiment — complementing the structured numbers
from SEC EDGAR (fundamentals, segments).

API shape (https://developer.alpha-sense.com):
  * Auth   — POST {base}/auth  (OAuth2 password grant), form fields
             grant_type/username/password/client_id/client_secret, header
             ``x-api-key``. Returns ``access_token`` (+ ``expires_in`` and
             ``refresh_token``).
  * Search — POST {base}/gql  GraphQL ``search(filter, limit, sorting)``
             returning ``documents[]`` with id/title/releasedAt/type/
             company/sentiment, plus a ``cursor``.

This client manages the bearer token (thread-safe, refreshed on expiry),
and exposes high-level helpers used by the provider layer. Every method
degrades to an empty result with a logged warning rather than raising, so
a brief never fails because AlphaSense is unavailable.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("calorch.alphasense")

# GraphQL search query. Field selection is deliberately conservative
# (documented fields only); adjust the selection set when validating
# against live credentials. Sentiment is populated for transcript-type docs.
_SEARCH_GQL = """
query Search($filter: SearchFilter, $limit: Int, $sorting: SearchSorting) {
  search(filter: $filter, limit: $limit, sorting: $sorting) {
    documents { id title releasedAt type company { name ticker } sentiment { score } }
    cursor
  }
}
""".strip()


class AlphaSenseClient:
    """Thin, resilient client for the AlphaSense Search API."""

    def __init__(
        self,
        *,
        api_key: str,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        base_url: str = "https://api.alpha-sense.com",
        timeout: float = 20.0,
    ) -> None:
        if not (api_key and client_id and client_secret and username and password):
            raise ValueError("AlphaSense requires api_key, client_id, client_secret, username, password")
        self._api_key = api_key
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._token_lock = threading.Lock()

    # -- auth -------------------------------------------------------------
    def _acquire_token(self) -> str:
        if self._token and time.time() < self._token_expires - 30:
            return self._token
        with self._token_lock:
            if self._token and time.time() < self._token_expires - 30:
                return self._token
            resp = self._http.post(
                f"{self._base}/auth",
                headers={"x-api-key": self._api_key, "Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "password",
                    "username": self._username,
                    "password": self._password,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            self._token = body["access_token"]
            self._token_expires = time.time() + int(body.get("expires_in", 3600))
            return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "clientid": self._client_id,
            "Authorization": f"Bearer {self._acquire_token()}",
            "Content-Type": "application/json",
        }

    # -- raw search -------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        doc_type: str | None = None,
        date_preset: str = "LAST_90_DAYS",
    ) -> list[dict[str, Any]]:
        """Return raw AlphaSense documents matching *query*.

        Each document: ``{id, title, releasedAt, type, company, sentiment}``.
        Returns ``[]`` on any failure (logged), never raises.
        """
        if not query.strip():
            return []
        filt: dict[str, Any] = {
            "keyword": {"query": query},
            "date": {"preset": date_preset},
        }
        if doc_type:
            filt["types"] = {"ids": [doc_type]}
        variables = {
            "filter": filt,
            "limit": min(max(limit, 1), 100),
            "sorting": {"field": "DATE", "direction": "DESC"},
        }
        try:
            resp = self._http.post(
                f"{self._base}/gql",
                headers=self._headers(),
                json={"query": _SEARCH_GQL, "variables": variables},
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("errors"):
                log.warning("AlphaSense GraphQL errors: %s", payload["errors"])
                return []
            return (payload.get("data", {}).get("search", {}) or {}).get("documents", []) or []
        except (httpx.HTTPError, ConnectionError, TimeoutError, ValueError, KeyError, TypeError) as e:
            log.warning("AlphaSense search failed for %r: %s", query[:60], e)
            return []

    # -- high-level helpers (consumed by the provider layer) --------------
    def guidance_hits(self, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Guidance / outlook excerpts for a ticker (filings + transcripts)."""
        docs = self.search(f"{ticker} guidance outlook forecast", limit=limit)
        return [_excerpt(d) for d in docs]

    def transcript_hits(self, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Earnings-call / expert-call transcript matches for a ticker."""
        docs = self.search(f"{ticker} earnings call expert", limit=limit, doc_type="TRANSCRIPT")
        return [_excerpt(d) for d in docs]

    def sentiment(self, ticker: str, *, limit: int = 10) -> dict[str, Any]:
        """Aggregate document sentiment for a ticker (mean of -1..1 scores)."""
        docs = self.search(f"{ticker}", limit=limit, doc_type="TRANSCRIPT")
        scores = [
            s for d in docs
            if isinstance((s := (d.get("sentiment") or {}).get("score")), (int, float))
        ]
        if not scores:
            return {"ticker": ticker, "mean_sentiment": None, "sample": 0, "source": "alphasense"}
        mean = sum(scores) / len(scores)
        return {
            "ticker": ticker,
            "mean_sentiment": round(mean, 3),
            "label": "positive" if mean > 0.1 else "negative" if mean < -0.1 else "neutral",
            "sample": len(scores),
            "source": "alphasense",
        }

    def close(self) -> None:
        self._http.close()


def _excerpt(doc: dict[str, Any]) -> dict[str, Any]:
    """Normalise an AlphaSense document to calorch's narrative-hit shape."""
    company = doc.get("company") or {}
    return {
        "title": doc.get("title", ""),
        "date": doc.get("releasedAt", ""),
        "type": doc.get("type", ""),
        "company": company.get("name", ""),
        "ticker": company.get("ticker", ""),
        "sentiment": (doc.get("sentiment") or {}).get("score"),
        "source": "alphasense",
        "doc_id": doc.get("id", ""),
    }
