"""Data-source provider layer — SEC EDGAR + AlphaSense only.

Two sources, cleanly split by what each does well:

  * **SEC EDGAR** (free, fair-use) — the structured numbers:
      - ``fundamentals`` : SEC iXBRL company facts (revenue, EPS, margins,
        balance sheet, cash flow)
      - ``segments``     : SEC iXBRL product / geographic revenue splits
      - ``filings``      : SEC EFTS full-text filing search (guidance excerpts)
  * **AlphaSense** (credentialed) — the qualitative side:
      - ``narrative``    : guidance / outlook excerpts across filings + transcripts
      - ``transcripts``  : earnings-call / expert-call transcript matches
      - ``sentiment``    : document-level sentiment (-1..1) for a ticker

There is no price, consensus, or macro provider: those required third-party
market-data vendors (Tiingo / FRED / FOMC H.15) that are out of scope. A
provider with no credentials returns empty data with a ``note``; the report's
Data Sources table makes that transparent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

log = logging.getLogger("calorch.providers")


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------
@runtime_checkable
class FundamentalsProvider(Protocol):
    def latest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]: ...


@runtime_checkable
class SegmentProvider(Protocol):
    def latest_segments(self, cik: str, ticker: str, *, axis: str = "product") -> list[dict[str, Any]]: ...


@runtime_checkable
class FilingsProvider(Protocol):
    def guidance_hits(self, cik: str, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]: ...


@runtime_checkable
class NarrativeProvider(Protocol):
    def guidance_hits(self, cik: str, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]: ...


@runtime_checkable
class TranscriptProvider(Protocol):
    def transcript_hits(self, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]: ...


@runtime_checkable
class SentimentProvider(Protocol):
    def sentiment(self, ticker: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Bundle — carries providers + source metadata
# ---------------------------------------------------------------------------
@dataclass
class ProviderBundle:
    fundamentals: FundamentalsProvider
    segments: SegmentProvider
    filings: FilingsProvider
    narrative: NarrativeProvider
    transcripts: TranscriptProvider
    sentiment: SentimentProvider
    sources: list[dict[str, str]] = field(default_factory=list)
    """List of {source_name: str, status: 'active'|'missing'|'error', detail: str}."""


# ---------------------------------------------------------------------------
# SEC-backed implementations
# ---------------------------------------------------------------------------
class IxbrlSegmentProvider:
    """Live SEC iXBRL segment data."""

    def __init__(self, ixbrl: Any) -> None:
        self._ixbrl = ixbrl

    def latest_segments(self, cik: str, ticker: str, *, axis: str = "product") -> list[dict[str, Any]]:
        if self._ixbrl is None:
            return []
        try:
            if axis == "product":
                return self._ixbrl.latest_revenue_segments(cik, ticker)
            elif axis == "geographic":
                return self._ixbrl.latest_revenue_geo(cik, ticker)
        except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
            log.warning("iXBRL segments network failure for %s: %s", ticker, e)
        except (ValueError, KeyError, TypeError) as e:
            log.warning("iXBRL segments parse failure for %s: %s", ticker, e)
        return []


class IxbrlFundamentalsProvider:
    """Live SEC iXBRL fundamentals — income statement, balance sheet, cash flow."""

    def __init__(self, ixbrl: Any) -> None:
        self._ixbrl = ixbrl

    def latest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]:
        if self._ixbrl is None:
            return {"source": "sec-ixbrl", "ticker": ticker, "note": "iXBRL client not available"}
        try:
            return self._ixbrl.latest_fundamentals(cik, ticker)
        except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
            log.warning("iXBRL fundamentals network failure for %s: %s", ticker, e)
            return {"source": "sec-ixbrl", "ticker": ticker, "note": f"network error: {e}"}
        except (ValueError, KeyError, TypeError) as e:
            log.warning("iXBRL fundamentals parse failure for %s: %s", ticker, e)
            return {"source": "sec-ixbrl", "ticker": ticker, "note": str(e)}


class EftsFilingsProvider:
    """Live SEC EFTS filing full-text search (guidance excerpts)."""

    def __init__(self, efts: Any) -> None:
        self._efts = efts

    def guidance_hits(self, cik: str, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if self._efts is None:
            return []
        try:
            return self._efts.search_guidance(cik=cik, ticker=ticker, limit=limit)
        except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
            log.warning("EFTS guidance network failure for %s: %s", ticker, e)
            return []
        except (ValueError, KeyError) as e:
            log.warning("EFTS guidance parse failure for %s: %s", ticker, e)
            return []


# ---------------------------------------------------------------------------
# AlphaSense-backed implementations
# ---------------------------------------------------------------------------
class AlphaSenseNarrativeProvider:
    """Qualitative guidance / outlook from AlphaSense (filings + transcripts).

    Signature mirrors the SEC filings provider (``cik, ticker``) so callers
    are interchangeable; ``cik`` is unused (AlphaSense keys on ticker).
    """

    def __init__(self, client: Any) -> None:
        self._c = client

    def guidance_hits(self, cik: str, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if self._c is None:
            return []
        return self._c.guidance_hits(ticker, limit=limit)


class AlphaSenseTranscriptProvider:
    def __init__(self, client: Any) -> None:
        self._c = client

    def transcript_hits(self, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if self._c is None:
            return []
        return self._c.transcript_hits(ticker, limit=limit)


class AlphaSenseSentimentProvider:
    def __init__(self, client: Any) -> None:
        self._c = client

    def sentiment(self, ticker: str) -> dict[str, Any]:
        if self._c is None:
            return {"ticker": ticker, "mean_sentiment": None, "sample": 0, "source": "none",
                    "note": "AlphaSense not configured"}
        return self._c.sentiment(ticker)


# ---------------------------------------------------------------------------
# Factory — wired at startup
# ---------------------------------------------------------------------------
def build_providers(settings: Any) -> ProviderBundle:
    """Build ProviderBundle.

    When blob storage is configured and ``USE_BLOB_PROVIDERS=true`` (default),
    reads pre-ingested data from Azure Blob Storage instead of making live API
    calls (the ingestion pipeline must have run first). Otherwise falls back to
    live SEC + AlphaSense clients.
    """
    use_blob = getattr(settings, "use_blob_providers", True)
    has_blob = bool(
        getattr(settings, "azure_storage_connection_string", None)
        or getattr(settings, "azure_storage_account_url", None)
        or getattr(settings, "blob_local_root", None)
    )
    if use_blob and has_blob:
        from calorch.blob_reader import build_blob_providers
        from calorch.blob_store import make_blob_store
        blob = make_blob_store(
            connection_string=getattr(settings, "azure_storage_connection_string", None),
            account_url=getattr(settings, "azure_storage_account_url", None),
            local_root=getattr(settings, "blob_local_root", None),
        )
        return build_blob_providers(blob)
    return _build_live_providers(settings)


def _build_live_providers(settings: Any) -> ProviderBundle:
    """Build ProviderBundle from live SEC + AlphaSense clients."""

    from .sec_efts import SecEftsClient
    from .sec_ixbrl import SecIxbrlClient

    sources: list[dict[str, str]] = []

    # ---- SEC iXBRL: fundamentals + segments ----
    ixbrl = None
    if getattr(settings, "use_ixbrl_segments", True):
        try:
            ixbrl = SecIxbrlClient(user_agent=settings.sec_user_agent, cache_dir=settings.sec_cache_dir / "ixbrl")
            sources.append({"source_name": "SEC iXBRL", "status": "active",
                            "detail": "Fundamentals + product/geographic segments"})
        except (OSError, ValueError, ImportError) as e:
            sources.append({"source_name": "SEC iXBRL", "status": "error", "detail": str(e)})

    # ---- SEC EFTS: filing full-text search ----
    efts = None
    if getattr(settings, "use_sec_efts", True):
        try:
            efts = SecEftsClient(user_agent=settings.sec_user_agent, cache_dir=settings.sec_cache_dir / "efts")
            sources.append({"source_name": "SEC EFTS", "status": "active", "detail": "Full-text filing search"})
        except (OSError, ValueError, ImportError) as e:
            sources.append({"source_name": "SEC EFTS", "status": "error", "detail": str(e)})

    # ---- AlphaSense: narrative + transcripts + sentiment ----
    alphasense = _build_alphasense(settings, sources)

    return ProviderBundle(
        fundamentals=IxbrlFundamentalsProvider(ixbrl=ixbrl),
        segments=IxbrlSegmentProvider(ixbrl=ixbrl),
        filings=EftsFilingsProvider(efts=efts),
        narrative=AlphaSenseNarrativeProvider(client=alphasense),
        transcripts=AlphaSenseTranscriptProvider(client=alphasense),
        sentiment=AlphaSenseSentimentProvider(client=alphasense),
        sources=sources,
    )


def _build_alphasense(settings: Any, sources: list[dict[str, str]]) -> Any:
    """Construct the shared AlphaSense client, or None when unconfigured."""
    if not getattr(settings, "use_alphasense", True):
        sources.append({"source_name": "AlphaSense", "status": "disabled", "detail": "USE_ALPHASENSE=false"})
        return None
    if not getattr(settings, "alphasense_api_key", None):
        sources.append({"source_name": "AlphaSense", "status": "missing", "detail": "ALPHASENSE_API_KEY not set"})
        return None
    try:
        from .alphasense import AlphaSenseClient

        client = AlphaSenseClient(
            api_key=settings.alphasense_api_key,
            client_id=settings.alphasense_client_id,
            client_secret=settings.alphasense_client_secret,
            username=settings.alphasense_username,
            password=settings.alphasense_password,
            base_url=settings.alphasense_base_url,
        )
        sources.append({"source_name": "AlphaSense", "status": "active",
                        "detail": "Guidance, transcripts/expert calls, sentiment"})
        return client
    except (ValueError, ImportError) as e:
        sources.append({"source_name": "AlphaSense", "status": "error", "detail": str(e)})
        return None
