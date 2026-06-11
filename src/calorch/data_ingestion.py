"""Data ingestion pipeline — fetches data from live APIs and stores in blob storage.

This is a separate pipeline from the orchestrator. It runs independently
(typically on a timer, e.g., daily after market close) to fetch fresh data
from SEC EDGAR and AlphaSense and store it in Azure Blob Storage.

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
from typing import Any

from calorch.blob_store import BlobStore, make_blob_store
from calorch.config import get_settings

log = logging.getLogger("calorch.data_ingestion")



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
            input_container=get_settings().blob_input_container,
            output_container=get_settings().blob_output_container,
        )
        self._date = date or _today()
        self._s = get_settings()
        self._log: list[str] = []

    # -- AlphaSense: narrative + transcripts + sentiment ------------------
    def _alphasense(self) -> Any:
        """Build the shared AlphaSense client, or None when unconfigured."""
        if not (self._s.use_alphasense and self._s.alphasense_api_key):
            return None
        try:
            from calorch.alphasense import AlphaSenseClient

            return AlphaSenseClient(
                api_key=self._s.alphasense_api_key,
                client_id=self._s.alphasense_client_id,
                client_secret=self._s.alphasense_client_secret,
                username=self._s.alphasense_username,
                password=self._s.alphasense_password,
                base_url=self._s.alphasense_base_url,
            )
        except (ValueError, ImportError) as e:
            log.warning("AlphaSense client unavailable: %s", e)
            return None

    def ingest_alphasense(self, ticker: str, client: Any | None = None) -> dict[str, Any]:
        """Ingest AlphaSense narrative, transcripts and sentiment for a ticker."""
        client = client or self._alphasense()
        if client is None:
            return {"status": "skipped", "ticker": ticker, "reason": "AlphaSense not configured"}
        try:
            narrative = client.guidance_hits(ticker, limit=10)
            transcripts = client.transcript_hits(ticker, limit=10)
            sentiment = client.sentiment(ticker)
        except Exception as e:
            log.warning("AlphaSense ingestion failed for %s: %s", ticker, e)
            return {"status": "error", "ticker": ticker, "error": str(e)}

        meta = {"ticker": ticker, "date": self._date}
        self._blob.upload_json(self._blob.input_container, f"inputs/narrative/{ticker}/{self._date}.json", narrative, metadata=meta)
        self._blob.upload_json(self._blob.input_container, f"inputs/transcripts/{ticker}/{self._date}.json", transcripts, metadata=meta)
        self._blob.upload_json(self._blob.input_container, f"inputs/sentiment/{ticker}/{self._date}.json", sentiment, metadata=meta)
        self._log.append(f"AlphaSense {ticker} ingested")
        return {"status": "ok", "ticker": ticker}


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
        self._blob.upload_json(self._blob.input_container, path, fundamentals, metadata={"ticker": ticker, "cik": cik, "date": self._date})

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
        self._blob.upload_json(self._blob.input_container, path, product_segments, metadata={"ticker": ticker, "cik": cik, "date": self._date, "axis": "product"})

        # Store geographic segments
        geo_path = f"inputs/segments/{cik}/{ticker}/{self._date}_geographic.json"
        self._blob.upload_json(self._blob.input_container, geo_path, geo_segments, metadata={"ticker": ticker, "cik": cik, "date": self._date, "axis": "geographic"})

        self._log.append(f"SEC segments {ticker} ingested")
        return {"status": "ok", "cik": cik, "ticker": ticker, "path": path, "geo_path": geo_path}

    # -- Filings: SEC EFTS ------------------------------------------------
    def ingest_filings(self, cik: str, ticker: str) -> dict[str, Any]:
        """Ingest SEC EFTS filing guidance excerpts for one ticker."""
        from calorch.sec_efts import SecEftsClient

        try:
            client = SecEftsClient(user_agent=self._s.sec_user_agent, cache_dir=self._s.sec_cache_dir / "efts")
            guidance = client.search_guidance(cik=cik, ticker=ticker, limit=10)
        except Exception as e:
            log.warning("SEC EFTS filings ingestion failed for %s: %s", ticker, e)
            return {"status": "error", "cik": cik, "ticker": ticker, "error": str(e)}

        path = f"inputs/filings/{cik}/{ticker}/{self._date}.json"
        self._blob.upload_json(self._blob.input_container, path, guidance, metadata={"ticker": ticker, "cik": cik, "date": self._date})

        self._log.append(f"SEC EFTS filings {ticker} ingested")
        return {"status": "ok", "cik": cik, "ticker": ticker, "path": path}

    # -- Run all for a list of tickers ------------------------------------
    def run(self, tickers: list[str], cik_lookup: Any | None = None) -> dict[str, Any]:
        """Run the full ingestion pipeline for a list of tickers.

        Per ticker:
          * SEC EDGAR  — fundamentals + segments + filing guidance (needs CIK)
          * AlphaSense — narrative + transcripts + sentiment (keyed on ticker)
        """
        results: dict[str, Any] = {"tickers": {}}

        if cik_lookup is None:
            try:
                from calorch.sec import SecEdgarClient
                sec = SecEdgarClient(user_agent=self._s.sec_user_agent, cache_dir=self._s.sec_cache_dir)
                cik_lookup = sec.cik_for
            except Exception as e:
                log.warning("CIK lookup unavailable, skipping SEC ingestion: %s", e)
                cik_lookup = None

        alphasense = self._alphasense()  # build once, reuse across tickers

        for ticker in tickers:
            ticker_results: dict[str, Any] = {}

            if cik_lookup:
                try:
                    cik = cik_lookup(ticker)
                    if cik:
                        ticker_results["fundamentals"] = self.ingest_fundamentals(cik, ticker)
                        ticker_results["segments"] = self.ingest_segments(cik, ticker)
                        ticker_results["filings"] = self.ingest_filings(cik, ticker)
                except Exception as e:
                    log.warning("SEC ingestion failed for %s: %s", ticker, e)
                    ticker_results["sec_error"] = str(e)

            ticker_results["alphasense"] = self.ingest_alphasense(ticker, client=alphasense)
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
