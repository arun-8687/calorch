"""Data ingestion pipeline — fetches data from live APIs and stores in blob storage.

This is a separate pipeline from the orchestrator. It runs independently
(typically on a timer, e.g., daily after market close) to fetch fresh data
from SEC, FRED, Tiingo, etc. and store it in Azure Blob Storage.

The orchestrator reads from the same blob storage — it never makes live API calls.

Usage (standalone):
    from calorch.data_ingestion import IngestionPipeline
    pipeline = IngestionPipeline()
    pipeline.run(tickers=["AAPL", "MSFT", "NVDA"])

Usage (Azure Durable Functions activity):
    @bp.activity_trigger(input_name="input")
    def activity_ingest(input):
        pipeline = IngestionPipeline()
        pipeline.run(tickers=input["tickers"])
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from calorch.blob_store import BlobStore, make_blob_store
from calorch.config import get_settings

log = logging.getLogger("calorch.data_ingestion")

_INPUT_CONTAINER = "calorch-inputs"


def _today() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d")


def _blob_path(provider: str, ticker: str = "", cik: str = "", date: str = "", suffix: str = ".json") -> str:
    if not date:
        date = _today()
    if cik:
        return f"inputs/{provider}/{cik}/{ticker}/{date}{suffix}"
    if ticker:
        return f"inputs/{provider}/{ticker}/{date}{suffix}"
    return f"inputs/{provider}/{date}{suffix}"


# ---------------------------------------------------------------------------
# Ingestion pipeline
# ---------------------------------------------------------------------------
class IngestionPipeline:
    """Fetch data from live APIs and store in blob storage.

    Each ingest method:
      1. Calls the live API client
      2. Stores the raw JSON response in blob storage
      3. Returns metadata about what was stored

    The pipeline is idempotent — running it twice with the same date
    overwrites the previous day's data.
    """

    def __init__(self, blob_store: BlobStore | None = None, date: str = "") -> None:
        self._blob = blob_store or make_blob_store(
            connection_string=get_settings().azure_storage_connection_string,
            account_url=get_settings().azure_storage_account_url,
            local_root=get_settings().blob_local_root,
        )
        self._date = date or _today()
        self._s = get_settings()
        self._log: list[str] = []

    # -- Macro: FRED + H.15 ------------------------------------------------
    def ingest_macro(self) -> dict[str, Any]:
        """Ingest FRED + H.15 macro snapshot."""
        from calorch.fred import FredClient
        from calorch.fed_h15 import FedH15Client

        snap: dict[str, Any] = {}
        errors: list[str] = []

        if self._s.fred_api_key or self._s.use_fred:
            try:
                fred = FredClient(api_key=self._s.fred_api_key, cache_dir=Path(".cache/fred"))
                snap.update(fred.snapshot())
                self._log.append("FRED ingested")
            except Exception as e:
                log.warning("FRED ingestion failed: %s", e)
                errors.append(f"fred:{e}")

        if self._s.use_fed_h15:
            try:
                h15 = FedH15Client(cache_dir=Path(".cache/fed"))
                h15_snap = h15.snapshot()
                # Merge H.15 data into the same snapshot
                for sid, entry in h15_snap.items():
                    if isinstance(entry, dict) and "value" in entry:
                        snap.setdefault(sid, entry)
                self._log.append("FOMC H.15 ingested")
            except Exception as e:
                log.warning("H.15 ingestion failed: %s", e)
                errors.append(f"h15:{e}")

        # Store in blob
        path = _blob_path("macro", date=self._date)
        self._blob.upload_json(_INPUT_CONTAINER, path, snap, metadata={"date": self._date, "sources": "fred,h15"})
        return {"status": "ok", "date": self._date, "path": path, "errors": errors, "log": self._log[-2:]}

    # -- Price: Tiingo ----------------------------------------------------
    def ingest_price(self, ticker: str) -> dict[str, Any]:
        """Ingest Tiingo price data for one ticker."""
        from calorch.tiingo import TiingoClient

        if not self._s.tiingo_api_key:
            return {"status": "skipped", "ticker": ticker, "reason": "TIINGO_API_KEY not set"}

        try:
            client = TiingoClient(api_key=self._s.tiingo_api_key, cache_dir=Path(".cache/tiingo"))
            quote = client.quote(ticker)
            ohlcv = client.ohlcv(ticker)
        except Exception as e:
            log.warning("Tiingo price ingestion failed for %s: %s", ticker, e)
            return {"status": "error", "ticker": ticker, "error": str(e)}

        # Store quote
        path = _blob_path("price", ticker=ticker, date=self._date)
        self._blob.upload_json(_INPUT_CONTAINER, path, quote, metadata={"ticker": ticker, "date": self._date})

        # Store OHLCV
        ohlcv_path = f"inputs/price/{ticker}/{self._date}_ohlcv.json"
        self._blob.upload_json(_INPUT_CONTAINER, ohlcv_path, ohlcv, metadata={"ticker": ticker, "date": self._date})

        self._log.append(f"Tiingo price {ticker} ingested")
        return {"status": "ok", "ticker": ticker, "path": path, "ohlcv_path": ohlcv_path}

    # -- Consensus: Tiingo --------------------------------------------------
    def ingest_consensus(self, ticker: str) -> dict[str, Any]:
        """Ingest Tiingo consensus estimates for one ticker."""
        from calorch.tiingo import TiingoClient

        if not self._s.tiingo_api_key:
            return {"status": "skipped", "ticker": ticker, "reason": "TIINGO_API_KEY not set"}

        try:
            client = TiingoClient(api_key=self._s.tiingo_api_key, cache_dir=Path(".cache/tiingo"))
            estimates = client.estimates(ticker)
        except Exception as e:
            log.warning("Tiingo consensus ingestion failed for %s: %s", ticker, e)
            return {"status": "error", "ticker": ticker, "error": str(e)}

        path = _blob_path("consensus", ticker=ticker, date=self._date)
        self._blob.upload_json(_INPUT_CONTAINER, path, estimates, metadata={"ticker": ticker, "date": self._date})

        self._log.append(f"Tiingo consensus {ticker} ingested")
        return {"status": "ok", "ticker": ticker, "path": path}

    # -- Fundamentals: SEC iXBRL ------------------------------------------
    def ingest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]:
        """Ingest SEC iXBRL fundamentals for one ticker."""
        from calorch.sec_ixbrl import SecIxbrlClient

        try:
            client = SecIxbrlClient(user_agent=self._s.sec_user_agent, cache_dir=self._s.sec_cache_dir / "ixbrl")
            fundamentals = client.latest_fundamentals(cik, ticker)
        except Exception as e:
            log.warning("SEC iXBRL fundamentals ingestion failed for %s: %s", ticker, e)
            return {"status": "error", "cik": cik, "ticker": ticker, "error": str(e)}

        path = _blob_path("fundamentals", ticker=ticker, cik=cik, date=self._date)
        self._blob.upload_json(_INPUT_CONTAINER, path, fundamentals, metadata={"ticker": ticker, "cik": cik, "date": self._date})

        self._log.append(f"SEC fundamentals {ticker} ingested")
        return {"status": "ok", "cik": cik, "ticker": ticker, "path": path}

    # -- Segments: SEC iXBRL ----------------------------------------------
    def ingest_segments(self, cik: str, ticker: str) -> dict[str, Any]:
        """Ingest SEC iXBRL segment data for one ticker."""
        from calorch.sec_ixbrl import SecIxbrlClient

        try:
            client = SecIxbrlClient(user_agent=self._s.sec_user_agent, cache_dir=self._s.sec_cache_dir / "ixbrl")
            product_segments = client.latest_revenue_segments(cik, ticker)
            geo_segments = client.latest_revenue_geo(cik, ticker)
        except Exception as e:
            log.warning("SEC iXBRL segments ingestion failed for %s: %s", ticker, e)
            return {"status": "error", "cik": cik, "ticker": ticker, "error": str(e)}

        # Store product segments
        path = f"inputs/segments/{cik}/{ticker}/{self._date}_product.json"
        self._blob.upload_json(_INPUT_CONTAINER, path, product_segments, metadata={"ticker": ticker, "cik": cik, "date": self._date, "axis": "product"})

        # Store geographic segments
        geo_path = f"inputs/segments/{cik}/{ticker}/{self._date}_geographic.json"
        self._blob.upload_json(_INPUT_CONTAINER, geo_path, geo_segments, metadata={"ticker": ticker, "cik": cik, "date": self._date, "axis": "geographic"})

        self._log.append(f"SEC segments {ticker} ingested")
        return {"status": "ok", "cik": cik, "ticker": ticker, "path": path, "geo_path": geo_path}

    # -- Narrative: SEC EFTS ----------------------------------------------
    def ingest_narrative(self, cik: str, ticker: str) -> dict[str, Any]:
        """Ingest SEC EFTS guidance excerpts for one ticker."""
        from calorch.sec_efts import SecEftsClient

        try:
            client = SecEftsClient(user_agent=self._s.sec_user_agent, cache_dir=self._s.sec_cache_dir / "efts")
            guidance = client.search_guidance(cik=cik, ticker=ticker, limit=10)
        except Exception as e:
            log.warning("SEC EFTS narrative ingestion failed for %s: %s", ticker, e)
            return {"status": "error", "cik": cik, "ticker": ticker, "error": str(e)}

        path = _blob_path("narrative", ticker=ticker, cik=cik, date=self._date)
        self._blob.upload_json(_INPUT_CONTAINER, path, guidance, metadata={"ticker": ticker, "cik": cik, "date": self._date})

        self._log.append(f"SEC EFTS {ticker} ingested")
        return {"status": "ok", "cik": cik, "ticker": ticker, "path": path}

    # -- Run all for a list of tickers ------------------------------------
    def run(self, tickers: list[str], cik_lookup: Any | None = None) -> dict[str, Any]:
        """Run the full ingestion pipeline for a list of tickers.

        Steps:
          1. Ingest macro data (FRED + H.15)
          2. For each ticker:
             a. Ingest price + consensus (Tiingo)
             b. Ingest fundamentals + segments + narrative (SEC)
        """
        results: dict[str, Any] = {"macro": self.ingest_macro(), "tickers": {}}

        # Resolve CIK for SEC data
        if cik_lookup is None:
            try:
                from calorch.sec import SecEdgarClient
                sec = SecEdgarClient(user_agent=self._s.sec_user_agent, cache_dir=self._s.sec_cache_dir)
                cik_lookup = sec.cik_for
            except Exception as e:
                log.warning("CIK lookup unavailable, skipping SEC ingestion: %s", e)
                cik_lookup = None

        for ticker in tickers:
            ticker_results: dict[str, Any] = {}
            ticker_results["price"] = self.ingest_price(ticker)
            ticker_results["consensus"] = self.ingest_consensus(ticker)

            if cik_lookup:
                try:
                    cik = cik_lookup(ticker)
                    if cik:
                        ticker_results["fundamentals"] = self.ingest_fundamentals(cik, ticker)
                        ticker_results["segments"] = self.ingest_segments(cik, ticker)
                        ticker_results["narrative"] = self.ingest_narrative(cik, ticker)
                except Exception as e:
                    log.warning("SEC ingestion failed for %s: %s", ticker, e)
                    ticker_results["sec_error"] = str(e)

            results["tickers"][ticker] = ticker_results

        results["log"] = self._log
        return results


# ---------------------------------------------------------------------------
# Timer-triggered ingestion activity (Azure Durable Functions)
# ---------------------------------------------------------------------------
def run_daily_ingestion(tickers: list[str] | None = None) -> dict[str, Any]:
    """Run the daily ingestion pipeline."""
    s = get_settings()
    tickers = tickers or s.sec_watchlist
    pipeline = IngestionPipeline()
    return pipeline.run(tickers)
