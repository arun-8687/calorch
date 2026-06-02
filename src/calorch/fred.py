"""FRED API client (St. Louis Fed).

Provides macro data for the weekly briefing, portfolio benchmark, and
channel-check context. Free, official, ToS-compliant. A key is
recommended (request quota) but not strictly required for low-volume use.

Common series used by calorch:
  * VIXCLS                 — CBOE VIX (close)
  * DGS10                  — 10-year Treasury constant maturity yield
  * DGS2                   — 2-year Treasury yield
  * DCOILWTICO             — WTI crude oil spot
  * GOLDAMGBD228NLBM       — Gold fixing price (London PM)
  * CBBTCUSD               — Coinbase Bitcoin (USD)
  * SP500                  — S&P 500 (not dividend-adjusted)
  * DEXUSEU                — USD/EUR exchange rate
  * DFF                    — Effective federal funds rate
  * CPIAUCSL               — CPI (Urban Consumer)
  * UNRATE                 — Civilian unemployment rate
  * GDPC1                  — Real GDP
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx


FRED_API = "https://api.stlouisfed.org/fred/series/observations"


@dataclass(frozen=True)
class FredPoint:
    series_id: str
    date: date
    value: float
    realtime_start: str
    realtime_end: str


class FredClient:
    """Minimal FRED client — no third-party SDK required, just httpx."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        cache_dir: Path | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._key = api_key
        cache = cache_dir or (Path.cwd() / ".cache" / "fred")
        cache.mkdir(parents=True, exist_ok=True)
        self._cache_dir = cache
        self._timeout = timeout

    def _cache_path(self, series_id: str, start: str, end: str) -> Path:
        return self._cache_dir / f"{series_id}_{start}_{end}.json"

    def get_series(
        self,
        series_id: str,
        start: str | None = None,
        end: str | None = None,
        *,
        limit: int = 1000,
    ) -> list[FredPoint]:
        """Return up to ``limit`` most recent observations for a series.

        ``start`` / ``end`` are FRED date strings (YYYY-MM-DD). Defaults to
        the last 365 days.
        """
        end = end or date.today().isoformat()
        start = start or (date.today() - timedelta(days=365)).isoformat()

        cache_path = self._cache_path(series_id, start, end)
        if cache_path.exists() and (datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)) < timedelta(hours=12):
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                return [FredPoint(**p) for p in cached]
            except (json.JSONDecodeError, TypeError):
                pass  # corrupt cache; refetch

        params: dict[str, Any] = {
            "series_id": series_id,
            "observation_start": start,
            "observation_end": end,
            "file_type": "json",
            "sort_order": "desc",
            "limit": str(limit),
        }
        if self._key:
            params["api_key"] = self._key

        r = httpx.get(FRED_API, params=params, timeout=self._timeout)
        r.raise_for_status()
        payload = r.json()
        if "observations" not in payload:
            # FRED returns 200 with an error JSON when no key
            # ({"error_code": 400, "error_message": "..."}). Treat as no data.
            return []
        obs = payload.get("observations", [])
        points: list[FredPoint] = []
        for o in obs:
            v = o.get("value", ".")
            if v == "." or v == "":
                continue  # FRED convention: "." = missing
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            try:
                d = date.fromisoformat(o["date"])
            except (KeyError, ValueError):
                continue
            points.append(FredPoint(
                series_id=series_id,
                date=d,
                value=fv,
                realtime_start=o.get("realtime_start", ""),
                realtime_end=o.get("realtime_end", ""),
            ))
        cache_path.write_text(
            json.dumps([p.__dict__ for p in points], default=str), encoding="utf-8"
        )
        return points

    def latest(self, series_id: str) -> float | None:
        """Return the most recent non-missing observation."""
        pts = self.get_series(series_id, limit=10)
        return pts[0].value if pts else None

    def change_1w(self, series_id: str) -> float | None:
        """Return (latest - 5_trading_days_ago) / 5_trading_days_ago * 100."""
        pts = self.get_series(series_id, limit=10)
        if len(pts) < 2:
            return None
        latest = pts[0].value
        # find observation ~5 trading days back
        prior = pts[-1].value
        if prior == 0:
            return None
        return (latest - prior) / prior * 100.0

    def snapshot(
        self,
        series_ids: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return a flat dict ``{display_name: {value, date, change_1w}}``.

        ``series_ids`` maps display label → FRED series id. Defaults to
        the standard calorch macro set.
        """
        series_ids = series_ids or {
            "vix": "VIXCLS",
            "treasury_10y": "DGS10",
            "treasury_2y": "DGS2",
            "fed_funds": "DFF",
            "wti_oil": "DCOILWTICO",
            "gold": "GOLDAMGBD228NLBM",
            "btc_usd": "CBBTCUSD",
            "sp500": "SP500",
            "usd_eur": "DEXUSEU",
            "cpi": "CPIAUCSL",
            "unemployment": "UNRATE",
        }
        out: dict[str, dict[str, Any]] = {}
        for label, sid in series_ids.items():
            try:
                pts = self.get_series(sid, limit=15)
                if not pts:
                    out[label] = {"value": None, "date": None, "change_1w": None, "series_id": sid}
                    continue
                latest_pt = pts[0]
                change = None
                if len(pts) >= 6:
                    prior = pts[5].value
                    if prior != 0:
                        change = (latest_pt.value - prior) / prior * 100.0
                out[label] = {
                    "value": latest_pt.value,
                    "date": latest_pt.date.isoformat(),
                    "change_1w": change,
                    "series_id": sid,
                }
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                out[label] = {"value": None, "date": None, "change_1w": None, "series_id": sid, "error": str(exc)}
        return out


