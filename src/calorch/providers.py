"""Data-source provider layer — SEC EDGAR (+ AlphaSense where configured).

Sources, cleanly split by what each does well:

  * **SEC EDGAR** (free, fair-use) — the structured numbers, and (now) the
    qualitative side too:
      - ``fundamentals`` : SEC iXBRL company facts (revenue, EPS, margins,
        balance sheet, cash flow)
      - ``segments``     : SEC iXBRL product / geographic revenue splits
      - ``filings``      : SEC EFTS full-text filing search (guidance excerpts)
      - ``narrative``    : guidance / outlook excerpts extracted from the
        latest 8-K press release + 10-Q/10-K MD&A (`sec_narrative.py`)
      - ``sentiment``    : local finance-lexicon score (-1..1) over those
        same filing texts (`sentiment_lexicon.py`)
  * **AlphaSense** (credentialed, optional) — the licensed qualitative side:
      - ``narrative``, ``transcripts``, ``sentiment``

``narrative``/``sentiment`` are backend-selectable (`NARRATIVE_BACKEND` /
`SENTIMENT_BACKEND`, default `"auto"` = AlphaSense when configured, else the
free SEC-derived backend) — see `_build_live_providers`. ``transcripts``
stays AlphaSense-only; with no AlphaSense credentials it degrades to empty,
same as today.

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
    def fundamentals_history(self, cik: str, ticker: str, *, quarters: int = 5) -> dict[str, Any]: ...


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
    ops: Any = None
    """Delivery repository (``calorch.tools.Repository``), for the internal_review
    agent's real pipeline-activity stats. ``None`` when not wired (e.g. tests
    that build a ``ProviderBundle`` directly) — that agent degrades to
    omitting its ops-derived sections, never fabricating stats."""


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

    def fundamentals_history(self, cik: str, ticker: str, *, quarters: int = 5) -> dict[str, Any]:
        """Delegates to the wrapped client's `fundamentals_history` when it has
        one (the opt-in edgartools backend); the native iXBRL client has no
        equivalent, so it degrades to an empty quarterly series.
        """
        if self._ixbrl is None or not hasattr(self._ixbrl, "fundamentals_history"):
            return {"source": "sec-ixbrl", "ticker": ticker, "quarterly": []}
        try:
            return self._ixbrl.fundamentals_history(cik, ticker, quarters=quarters)
        except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
            log.warning("iXBRL fundamentals_history network failure for %s: %s", ticker, e)
            return {"source": "sec-ixbrl", "ticker": ticker, "quarterly": [], "note": f"network error: {e}"}
        except (ValueError, KeyError, TypeError) as e:
            log.warning("iXBRL fundamentals_history parse failure for %s: %s", ticker, e)
            return {"source": "sec-ixbrl", "ticker": ticker, "quarterly": [], "note": str(e)}


class EftsFilingsProvider:
    """Live SEC EFTS filing full-text search (guidance excerpts)."""

    def __init__(self, efts: Any) -> None:
        self._efts = efts

    def guidance_hits(self, cik: str, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if self._efts is None:
            return []
        try:
            return self._efts.search_for_guidance(cik=cik, ticker=ticker, limit=limit)
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
# SEC-narrative / lexicon-backed implementations (free AlphaSense replacement
# for the `narrative` and `sentiment` slots; see `sec_narrative.py` and
# `sentiment_lexicon.py`). `transcripts` has no SEC-derived equivalent and
# stays AlphaSense-only.
# ---------------------------------------------------------------------------
class SecNarrativeProvider:
    """Guidance / outlook excerpts extracted from SEC filings.

    Signature mirrors `AlphaSenseNarrativeProvider` (``cik, ticker``); ``cik``
    is unused (the client keys on ticker).
    """

    def __init__(self, client: Any) -> None:
        self._c = client

    def guidance_hits(self, cik: str, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if self._c is None:
            return []
        try:
            return self._c.narrative_docs(ticker, limit=limit)
        except Exception as e:  # noqa: BLE001 - third-party surface, degrade not raise
            log.warning("SEC narrative guidance_hits failed for %s: %s", ticker, e)
            return []


class LexiconSentimentProvider:
    """Local finance-lexicon sentiment over the same SEC filing texts."""

    def __init__(self, client: Any) -> None:
        self._c = client

    def sentiment(self, ticker: str) -> dict[str, Any]:
        if self._c is None:
            return {"ticker": ticker, "mean_sentiment": None, "sample": 0, "source": "none",
                    "note": "SEC narrative client not configured"}
        try:
            from .sentiment_lexicon import aggregate

            texts = [doc["text"] for doc in self._c.full_texts(ticker, limit=5)]
            agg = aggregate(texts)
            return {"ticker": ticker, "source": "sec-lexicon", **agg}
        except Exception as e:  # noqa: BLE001 - third-party surface, degrade not raise
            log.warning("Lexicon sentiment failed for %s: %s", ticker, e)
            return {"ticker": ticker, "mean_sentiment": None, "sample": 0, "source": "sec-lexicon",
                    "note": str(e)}


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
    # `fundamentals` alone can be swapped to the opt-in edgartools backend
    # (SEC_BACKEND=edgartools); segments stay on the native iXBRL client
    # regardless, since edgartools has no equivalent segment extraction.
    ixbrl = None
    if getattr(settings, "use_ixbrl_segments", True):
        try:
            ixbrl = SecIxbrlClient(user_agent=settings.sec_user_agent, cache_dir=settings.sec_cache_dir / "ixbrl")
            sources.append({"source_name": "SEC iXBRL", "status": "active",
                            "detail": "Fundamentals + product/geographic segments"})
        except (OSError, ValueError, ImportError) as e:
            sources.append({"source_name": "SEC iXBRL", "status": "error", "detail": str(e)})

    fundamentals_client = ixbrl
    if getattr(settings, "sec_backend", "native") == "edgartools":
        try:
            from .sec_edgartools import EdgarToolsClient

            fundamentals_client = EdgarToolsClient(
                user_agent=settings.sec_user_agent, cache_dir=settings.sec_cache_dir / "edgartools"
            )
            sources.append({"source_name": "SEC edgartools", "status": "active",
                            "detail": "Fundamentals (opt-in backend, SEC_BACKEND=edgartools)"})
        except ImportError as e:
            log.warning("SEC_BACKEND=edgartools but edgartools is not installed, "
                        "falling back to native iXBRL fundamentals: %s", e)
            sources.append({"source_name": "SEC edgartools", "status": "error",
                            "detail": f"edgartools not installed, using native fallback: {e}"})

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

    # ---- narrative / sentiment backend resolution ----
    narrative_backend = _resolve_backend(
        getattr(settings, "narrative_backend", "auto"), alphasense_configured=alphasense is not None, fallback="sec"
    )
    sentiment_backend = _resolve_backend(
        getattr(settings, "sentiment_backend", "auto"), alphasense_configured=alphasense is not None,
        fallback="lexicon",
    )
    sec_narrative_client = None
    if narrative_backend == "sec" or sentiment_backend == "lexicon":
        try:
            from .sec_narrative import SecNarrativeClient

            sec_narrative_client = SecNarrativeClient(
                user_agent=settings.sec_user_agent, cache_dir=settings.sec_cache_dir / "narrative"
            )
            sources.append({"source_name": "SEC narrative", "status": "active",
                            "detail": "Guidance excerpts from 8-K press release + 10-Q/10-K MD&A"})
        except ImportError as e:
            log.warning("SEC narrative backend requires edgartools, which is not installed: %s", e)
            sources.append({"source_name": "SEC narrative", "status": "error",
                            "detail": f"edgartools not installed: {e}"})

    # Reported *after* client construction: a slot is only "active" if the
    # client backing it actually exists. Claiming "active" up front made the
    # pack's Data Sources section overstate coverage whenever the chosen
    # backend's dependency or credentials were missing.
    def _slot_status(backend: str) -> dict[str, str]:
        client = alphasense if backend == "alphasense" else sec_narrative_client
        if client is not None:
            return {"status": "active", "detail": f"backend={backend}"}
        return {"status": "unavailable", "detail": f"backend={backend} selected but its client is unavailable"}

    sources.append({"source_name": "narrative", **_slot_status(narrative_backend)})
    sources.append({"source_name": "sentiment", **_slot_status(sentiment_backend)})

    narrative_provider: Any = (
        AlphaSenseNarrativeProvider(client=alphasense) if narrative_backend == "alphasense"
        else SecNarrativeProvider(client=sec_narrative_client)
    )
    sentiment_provider: Any = (
        AlphaSenseSentimentProvider(client=alphasense) if sentiment_backend == "alphasense"
        else LexiconSentimentProvider(client=sec_narrative_client)
    )

    return ProviderBundle(
        fundamentals=IxbrlFundamentalsProvider(ixbrl=fundamentals_client),
        segments=IxbrlSegmentProvider(ixbrl=ixbrl),
        filings=EftsFilingsProvider(efts=efts),
        narrative=narrative_provider,
        transcripts=AlphaSenseTranscriptProvider(client=alphasense),
        sentiment=sentiment_provider,
        sources=sources,
    )


def _resolve_backend(configured: str, *, alphasense_configured: bool, fallback: str) -> str:
    """Resolve an "auto"/explicit backend setting to a concrete backend name.

    "auto" -> "alphasense" when the shared AlphaSense client was built,
    else `fallback` (the free backend: "sec" for narrative, "lexicon" for
    sentiment).
    """
    if configured != "auto":
        return configured
    return "alphasense" if alphasense_configured else fallback


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
