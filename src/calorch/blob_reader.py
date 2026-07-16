"""Blob Storage Reader — reads pre-ingested SEC + AlphaSense data.

The ingestion pipeline (data_ingestion.py) writes provider responses to
blob storage; this module reads them back into the same ProviderBundle
shape so the orchestrator never makes live API calls during a run.

Path conventions (under the input container):
  inputs/fundamentals/{cik}/{ticker}/{date}.json     — SEC iXBRL fundamentals
  inputs/fundamentals_history/{cik}/{ticker}/{date}.json — quarterly history
                                                        (edgartools backend only)
  inputs/segments/{cik}/{ticker}/{date}_{axis}.json  — SEC iXBRL segments
  inputs/filings/{cik}/{ticker}/{date}.json          — SEC EFTS guidance excerpts
  inputs/narrative/{ticker}/{date}.json              — AlphaSense guidance excerpts
  inputs/transcripts/{ticker}/{date}.json            — AlphaSense transcript hits
  inputs/sentiment/{ticker}/{date}.json              — AlphaSense sentiment
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC
from typing import Any

from calorch.blob_store import BlobStore
from calorch.providers import ProviderBundle

log = logging.getLogger("calorch.blob_reader")


def _today() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# SEC blob providers
# ---------------------------------------------------------------------------
class BlobSegmentProvider:
    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def latest_segments(self, cik: str, ticker: str, *, axis: str = "product") -> list[dict[str, Any]]:
        path = f"inputs/segments/{cik}/{ticker}/{self._date}_{axis}.json"
        return self._blob.download_json(self._blob.input_container, path) or []


class BlobFundamentalsProvider:
    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def latest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]:
        path = f"inputs/fundamentals/{cik}/{ticker}/{self._date}.json"
        data = self._blob.download_json(self._blob.input_container, path)
        if data is None:
            return {"source": "blob", "ticker": ticker, "note": f"No fundamentals blob for {ticker} on {self._date}"}
        return data

    def fundamentals_history(self, cik: str, ticker: str, *, quarters: int = 5) -> dict[str, Any]:
        prefix = f"inputs/fundamentals_history/{cik}/{ticker}/"
        path = f"{prefix}{self._date}.json"
        data = self._blob.download_json(self._blob.input_container, path)
        if data is not None:
            return data

        # Same-day miss -> fall back to the latest available date for this
        # cik/ticker (blob names are `{prefix}{YYYYMMDD}.json`, so lexical
        # sort order matches chronological order).
        try:
            names = self._blob.list_blobs(self._blob.input_container, prefix)
        except Exception as e:  # noqa: BLE001 - defensive: degrade, never raise
            log.warning("fundamentals_history list_blobs failed for %s: %s", ticker, e)
            names = []
        if names:
            latest = sorted(names)[-1]
            data = self._blob.download_json(self._blob.input_container, latest)
            if data is not None:
                return data

        return {
            "source": "blob", "ticker": ticker, "cik": cik, "quarterly": [],
            "note": f"No fundamentals_history blob for {ticker} (checked {self._date} and prior dates)",
        }


class BlobFilingsProvider:
    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def guidance_hits(self, cik: str, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        path = f"inputs/filings/{cik}/{ticker}/{self._date}.json"
        data = self._blob.download_json(self._blob.input_container, path)
        return (data or [])[:limit]


# ---------------------------------------------------------------------------
# AlphaSense blob providers
# ---------------------------------------------------------------------------
class BlobNarrativeProvider:
    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def guidance_hits(self, cik: str, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        path = f"inputs/narrative/{ticker}/{self._date}.json"
        data = self._blob.download_json(self._blob.input_container, path)
        return (data or [])[:limit]


class BlobTranscriptProvider:
    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def transcript_hits(self, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        path = f"inputs/transcripts/{ticker}/{self._date}.json"
        data = self._blob.download_json(self._blob.input_container, path)
        return (data or [])[:limit]


class BlobSentimentProvider:
    def __init__(self, blob_store: BlobStore, date: str = "") -> None:
        self._blob = blob_store
        self._date = date or _today()

    def sentiment(self, ticker: str) -> dict[str, Any]:
        path = f"inputs/sentiment/{ticker}/{self._date}.json"
        data = self._blob.download_json(self._blob.input_container, path)
        if data is None:
            return {"ticker": ticker, "mean_sentiment": None, "sample": 0, "source": "blob",
                    "note": f"No sentiment blob for {ticker} on {self._date}"}
        return data


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_blob_providers(
    blob_store: BlobStore,
    *,
    date: str = "",
    sources: list[dict[str, str]] | None = None,
) -> ProviderBundle:
    """Build a ProviderBundle that reads pre-ingested SEC + AlphaSense data."""
    if sources is None:
        sources = [
            {"source_name": "SEC iXBRL", "status": "active", "detail": "Pre-ingested from blob storage"},
            {"source_name": "SEC EFTS", "status": "active", "detail": "Pre-ingested from blob storage"},
            {"source_name": "AlphaSense", "status": "active", "detail": "Pre-ingested from blob storage"},
        ]
    return ProviderBundle(
        fundamentals=BlobFundamentalsProvider(blob_store, date=date),
        segments=BlobSegmentProvider(blob_store, date=date),
        filings=BlobFilingsProvider(blob_store, date=date),
        narrative=BlobNarrativeProvider(blob_store, date=date),
        transcripts=BlobTranscriptProvider(blob_store, date=date),
        sentiment=BlobSentimentProvider(blob_store, date=date),
        sources=sources,
    )
