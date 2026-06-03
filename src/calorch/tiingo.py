"""Tiingo API client for real-time prices and fundamentals.

Tiingo offers a $50/mo Business tier with:
  * EOD prices (IEX feed)
  * Fundamental data (EPS, revenue, shares outstanding)
  * News sentiment

Docs: https://api.tiingo.com/documentation
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from calorch.http_client import get_client

TIINGO_API = "https://api.tiingo.com"


class TiingoClient:
    """Minimal Tiingo client — prices + fundamentals."""

    def __init__(self, api_key: str, *, cache_dir: Path | None = None) -> None:
        self._key = api_key
        self._cache_dir = cache_dir or (Path.cwd() / ".cache" / "tiingo")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_req = 0.0
        self._min_interval = 0.1  # 10 req/sec max

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_req
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_req = time.monotonic()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._key}", "Content-Type": "application/json"}

    def _cache_path(self, endpoint: str, ticker: str) -> Path:
        safe = endpoint.replace("/", "_")
        return self._cache_dir / f"{safe}_{ticker}.json"

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._rate_limit()
        url = f"{TIINGO_API}{endpoint}"
        client = get_client()
        r = client.get(url, headers=self._headers(), params=params or {}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    def quote(self, ticker: str) -> dict[str, Any]:
        """Return latest price, market cap, 52w range, YTD %, etc."""
        cache = self._cache_path("quote", ticker)
        if cache.exists() and (datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)) < timedelta(hours=6):
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        data = self._get(f"/tiingo/daily/{ticker}/prices", {"startDate": (date.today() - timedelta(days=365)).isoformat()})
        if not data:
            return {"source": "tiingo", "error": "no data"}
        latest = data[0]
        # Need to fetch meta for market cap, 52w range
        meta = self._get(f"/tiingo/daily/{ticker}")
        result = {
            "price": latest.get("close"),
            "change_pct": latest.get("close") / meta.get("previousClose", latest.get("close")) - 1 if meta.get("previousClose") else None,
            "market_cap": meta.get("marketCap"),
            "52w_low": meta.get("fiftyTwoWeekLow"),
            "52w_high": meta.get("fiftyTwoWeekHigh"),
            "volume": latest.get("volume"),
            "as_of": latest.get("date"),
            "source": "tiingo",
        }
        cache.write_text(json.dumps(result), encoding="utf-8")
        return result

    def fundamentals(self, ticker: str) -> dict[str, Any]:
        """Return fundamentals: EPS, revenue, margins, balance sheet items."""
        cache = self._cache_path("fundamentals", ticker)
        if cache.exists() and (datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)) < timedelta(hours=12):
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        # Tiingo fundamentals endpoint
        try:
            data = self._get(f"/tiingo/fundamentals/{ticker}/statements", {"asReported": "true"})
        except httpx.HTTPError:
            return {"source": "tiingo", "error": "fundamentals unavailable"}
        cache.write_text(json.dumps(data), encoding="utf-8")
        return {"source": "tiingo", "data": data}

    def estimates(self, ticker: str) -> dict[str, Any]:
        """Return analyst estimates: EPS, revenue, price target."""
        cache = self._cache_path("estimates", ticker)
        if cache.exists() and (datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)) < timedelta(hours=12):
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        try:
            data = self._get(f"/tiingo/fundamentals/{ticker}/ratios")
        except httpx.HTTPError:
            return {"source": "tiingo", "error": "estimates unavailable"}
        cache.write_text(json.dumps(data), encoding="utf-8")
        return {"source": "tiingo", "data": data}
