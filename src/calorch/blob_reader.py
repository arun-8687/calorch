"""Blob Storage Reader — reads pre-ingested data from Azure Blob Storage.

This module replaces the live provider calls in the orchestrator.
The data ingestion pipeline (data_ingestion.py) writes to blob storage.
This module reads from blob storage and returns the same data structures.

Path conventions:
  inputs/macro/{date}.json           — FRED + H.15 macro snapshot
  inputs/price/{ticker}/{date}.json  — Tiingo price data
  inputs/consensus/{ticker}/{date}.json — Tiingo consensus estimates
  inputs/fundamentals/{cik}/{ticker}/{date}.json — SEC iXBRL fundamentals
  inputs/segments/{cik}/{ticker}/{date}.json — SEC iXBRL segments
  inputs/narrative/{cik}/{ticker}/{date}.json — SEC EFTS guidance excerpts
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC
from typing import Any

from calorch.blob_store import BlobStore
from calorch.providers import (
    ProviderBundle,
)

log = logging.getLogger("calorch.blob_reader")

_INPUT_CONTAINER = "calorch-inputs"


def _today() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d")


def _blob_path(provider: str, ticker: str = "", cik: str = "", date: str = "") -> str:
    """Build a blob path for pre-ingested data."""
    if not date:
        date = _today()
    if cik:
        return f"inputs/{provider}/{cik}/{ticker}/{date}.json"
    if ticker:
        return f"inputs/{provider}/{ticker}/{date}.json"
    return f"inputs/{provider}/{date}.json"


# ---------------------------------------------------------------------------
# Blob-based implementations of provider protocols
# ---------------------------------------------------------------------------
class BlobMacroProvider:
    """Read macro snapshot from blob storage (FRED + H.15)."""

    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        path = _blob_path("macro", date=self._date)
        data = self._blob.download_json(_INPUT_CONTAINER, path)
        if data is None:
            log.warning("Macro snapshot not found in blob: %s", path)
            return {}
        return data


class BlobPriceProvider:
    """Read Tiingo price data from blob storage."""

    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def quote(self, ticker: str) -> dict[str, Any]:
        path = _blob_path("price", ticker=ticker, date=self._date)
        data = self._blob.download_json(_INPUT_CONTAINER, path)
        if data is None:
            return _empty_price(f"No blob for {ticker} on {self._date}")
        return data

    def ohlcv(self, ticker: str, *, days: int = 252) -> list[dict[str, Any]]:
        # OHLCV is stored in a separate blob
        path = f"inputs/price/{ticker}/{self._date}_ohlcv.json"
        data = self._blob.download_json(_INPUT_CONTAINER, path)
        if data is None:
            return []
        return data


def _empty_price(note: str) -> dict[str, Any]:
    return {"price": None, "market_cap": None, "52w_low": None, "52w_high": None,
            "1w_pct": None, "1m_pct": None, "ytd_pct": None, "beta": None,
            "as_of": None, "source": "none", "note": note}


class BlobConsensusProvider:
    """Read Tiingo consensus data from blob storage."""

    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def estimates(self, ticker: str) -> dict[str, Any]:
        path = _blob_path("consensus", ticker=ticker, date=self._date)
        data = self._blob.download_json(_INPUT_CONTAINER, path)
        if data is None:
            return _empty_consensus(f"No consensus blob for {ticker} on {self._date}")
        return data

    def recommendations(self, ticker: str) -> dict[str, Any]:
        # Recommendations are stored alongside estimates
        path = _blob_path("consensus", ticker=ticker, date=self._date)
        data = self._blob.download_json(_INPUT_CONTAINER, path)
        if data is None:
            return _empty_consensus(f"No consensus blob for {ticker}")
        return {
            "buy": data.get("buy"),
            "hold": data.get("hold"),
            "sell": data.get("sell"),
            "mean_target": data.get("mean_target"),
            "high_target": data.get("high_target"),
            "low_target": data.get("low_target"),
            "as_of": data.get("as_of"),
            "source": data.get("source", "blob"),
        }


def _empty_consensus(note: str) -> dict[str, Any]:
    return {"buy": None, "hold": None, "sell": None, "mean_target": None,
            "high_target": None, "low_target": None, "as_of": None,
            "source": "none", "note": note}


class BlobSegmentProvider:
    """Read SEC iXBRL segment data from blob storage."""

    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def latest_segments(self, cik: str, ticker: str, *, axis: str = "product") -> list[dict[str, Any]]:
        path = f"inputs/segments/{cik}/{ticker}/{self._date}_{axis}.json"
        data = self._blob.download_json(_INPUT_CONTAINER, path)
        if data is None:
            return []
        return data


class BlobFundamentalsProvider:
    """Read SEC iXBRL fundamentals from blob storage."""

    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def latest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]:
        path = f"inputs/fundamentals/{cik}/{ticker}/{self._date}.json"
        data = self._blob.download_json(_INPUT_CONTAINER, path)
        if data is None:
            return {"source": "blob", "ticker": ticker, "note": f"No fundamentals blob for {ticker} on {self._date}"}
        return data


class BlobNarrativeProvider:
    """Read SEC EFTS narrative excerpts from blob storage."""

    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def guidance_hits(self, cik: str, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        path = f"inputs/narrative/{cik}/{ticker}/{self._date}.json"
        data = self._blob.download_json(_INPUT_CONTAINER, path)
        if data is None:
            return []
        return data[:limit]


# ---------------------------------------------------------------------------
# Factory — builds blob-based providers
# ---------------------------------------------------------------------------
def build_blob_providers(
    blob_store: BlobStore,
    *,
    date: str = "",
    sources: list[dict[str, str]] | None = None,
) -> ProviderBundle:
    """Build a ProviderBundle that reads from blob storage.

    The data ingestion pipeline (data_ingestion.py) must have run before
    this bundle is used. If a blob is missing, the provider returns empty
    data with a ``note`` explaining what's missing.
    """
    if sources is None:
        sources = [
            {"source_name": "FRED", "status": "active", "detail": "Pre-ingested from blob storage"},
            {"source_name": "FOMC H.15", "status": "active", "detail": "Pre-ingested from blob storage"},
            {"source_name": "SEC iXBRL", "status": "active", "detail": "Pre-ingested from blob storage"},
            {"source_name": "SEC EFTS", "status": "active", "detail": "Pre-ingested from blob storage"},
            {"source_name": "Tiingo", "status": "active", "detail": "Pre-ingested from blob storage"},
        ]

    return ProviderBundle(
        price=BlobPriceProvider(blob_store, date=date),
        consensus=BlobConsensusProvider(blob_store, date=date),
        fundamentals=BlobFundamentalsProvider(blob_store, date=date),
        macro=BlobMacroProvider(blob_store, date=date),
        segments=BlobSegmentProvider(blob_store, date=date),
        narrative=BlobNarrativeProvider(blob_store, date=date),
        sources=sources,
    )
