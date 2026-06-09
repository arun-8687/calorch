"""Data-source provider layer — live authenticated data only.

No stub data, no mock data, no curated demo data. Every provider delegates
to a live API client that requires credentials or is a free, ToS-compliant
public endpoint (SEC EDGAR, FRED, FOMC H.15).

When a provider cannot be initialised (missing key, network error), it
returns empty data with a clear ``note`` explaining what's missing.
The report's Data Sources table at the bottom makes this transparent.

Provider resolution at startup:
  * Macro      → FRED (with or without key) + FOMC H.15 (always free)
  * Segments   → SEC iXBRL (free, fair-use rate limited)
  * Narrative  → SEC EFTS (free, fair-use rate limited)
  * Price      → Tiingo (requires TIINGO_API_KEY; empty otherwise)
  * Consensus  → Tiingo (requires TIINGO_API_KEY; empty otherwise)
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import httpx
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("calorch.providers")


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------
@runtime_checkable
class PriceProvider(Protocol):
    def quote(self, ticker: str) -> dict[str, Any]: ...
    def ohlcv(self, ticker: str, *, days: int = 252) -> list[dict[str, Any]]: ...


@runtime_checkable
class ConsensusProvider(Protocol):
    def estimates(self, ticker: str) -> dict[str, Any]: ...
    def recommendations(self, ticker: str) -> dict[str, Any]: ...


@runtime_checkable
class MacroProvider(Protocol):
    def snapshot(self) -> dict[str, dict[str, Any]]: ...


@runtime_checkable
class SegmentProvider(Protocol):
    def latest_segments(self, cik: str, ticker: str, *, axis: str = "product") -> list[dict[str, Any]]: ...


@runtime_checkable
class FundamentalsProvider(Protocol):
    def latest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]: ...


@runtime_checkable
class NarrativeProvider(Protocol):
    def guidance_hits(self, cik: str, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Bundle — carries providers + source metadata
# ---------------------------------------------------------------------------
@dataclass
class ProviderBundle:
    price: PriceProvider
    consensus: ConsensusProvider
    fundamentals: FundamentalsProvider
    macro: MacroProvider
    segments: SegmentProvider
    narrative: NarrativeProvider
    sources: list[dict[str, str]] = field(default_factory=list)
    """List of {source_name: str, status: 'active'|'missing'|'error', detail: str}."""


# ---------------------------------------------------------------------------
# Free / live implementations
# ---------------------------------------------------------------------------
@dataclass
class FreeMacroProvider:
    """Wraps FRED (preferred) and falls back to FOMC H.15."""

    H15_TO_LABEL = {
        "DFF": "fed_funds", "DGS1MO": "treasury_1mo", "DGS3MO": "treasury_3mo",
        "DGS6MO": "treasury_6mo", "DGS1": "treasury_1y", "DGS2": "treasury_2y",
        "DGS3": "treasury_3y", "DGS5": "treasury_5y", "DGS7": "treasury_7y",
        "DGS10": "treasury_10y", "DGS20": "treasury_20y", "DGS30": "treasury_30y",
    }

    def __init__(self, fred: Any, fed_h15: Any) -> None:
        self._fred = fred
        self._fed_h15 = fed_h15

    def snapshot(self) -> dict[str, dict[str, Any]]:
        try:
            snap = self._fred.snapshot()
        except (httpx.HTTPError, ConnectionError, TimeoutError, RuntimeError, ValueError, KeyError, AttributeError) as e:
            log.warning("FRED snapshot failed: %s", e)
            snap = {}
        if self._fed_h15:
            try:
                h15 = self._fed_h15.snapshot()
                for sid, label in self.H15_TO_LABEL.items():
                    if label in snap and snap[label].get("value") is not None:
                        continue
                    h15_entry = h15.get(sid)
                    if h15_entry and h15_entry.get("value") is not None:
                        snap[label] = {
                            "value": h15_entry["value"], "date": h15_entry.get("date", ""),
                            "change_1w_bps": h15_entry.get("change_1w_bps"),
                            "series_id": sid, "series_name": h15_entry.get("series_name", sid),
                            "source": "fed-h15",
                        }
            except (httpx.HTTPError, ConnectionError, TimeoutError, RuntimeError, ValueError, KeyError, AttributeError) as e:
                log.warning("FOMC H.15 snapshot failed: %s", e)
        for k, v in snap.items():
            if "source" not in v:
                v["source"] = "fred"
        return snap


@dataclass
class TiingoPriceProvider:
    """Live Tiingo EOD price data."""

    def __init__(self, tiingo: Any) -> None:
        self._t = tiingo

    def quote(self, ticker: str) -> dict[str, Any]:
        if self._t is None:
            return self._empty("TIINGO_API_KEY not set")
        try:
            return self._t.quote(ticker)
        except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
            log.warning("Tiingo quote failed for %s: %s", ticker, e)
            return self._empty(f"Tiingo error: {e}")
        except (ValueError, KeyError) as e:
            log.warning("Tiingo quote parse error for %s: %s", ticker, e)
            return self._empty(f"Tiingo error: {e}")

    def ohlcv(self, ticker: str, *, days: int = 252) -> list[dict[str, Any]]:
        if self._t is None:
            return []
        try:
            return self._t.ohlcv(ticker, days=days)
        except (httpx.HTTPError, ConnectionError, TimeoutError, ValueError, KeyError):
            return []

    @staticmethod
    def _empty(note: str) -> dict[str, Any]:
        return {"price": None, "market_cap": None, "52w_low": None, "52w_high": None,
                "1w_pct": None, "1m_pct": None, "ytd_pct": None, "beta": None,
                "as_of": None, "source": "none", "note": note}


@dataclass
class TiingoConsensusProvider:
    """Live Tiingo consensus / analyst data."""

    def __init__(self, tiingo: Any) -> None:
        self._t = tiingo

    def estimates(self, ticker: str) -> dict[str, Any]:
        if self._t is None:
            return self._empty("TIINGO_API_KEY not set")
        try:
            return self._t.estimates(ticker)
        except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
            log.warning("Tiingo estimates network failure for %s: %s", ticker, e)
            return self._empty(f"Tiingo error: {e}")
        except (ValueError, KeyError) as e:
            log.warning("Tiingo estimates parse error for %s: %s", ticker, e)
            return self._empty(f"Tiingo error: {e}")

    def recommendations(self, ticker: str) -> dict[str, Any]:
        if self._t is None:
            return self._empty("TIINGO_API_KEY not set")
        try:
            est = self._t.estimates(ticker)
            return {
                "buy": est.get("buy"), "hold": est.get("hold"), "sell": est.get("sell"),
                "mean_target": est.get("mean_target"), "high_target": est.get("high_target"),
                "low_target": est.get("low_target"), "as_of": est.get("as_of"),
                "source": est.get("source", "tiingo"),
            }
        except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
            log.warning("Tiingo recommendations network failure for %s: %s", ticker, e)
            return self._empty(f"Tiingo error: {e}")
        except (ValueError, KeyError) as e:
            log.warning("Tiingo recommendations parse error for %s: %s", ticker, e)
            return self._empty(f"Tiingo error: {e}")

    @staticmethod
    def _empty(note: str) -> dict[str, Any]:
        return {"buy": None, "hold": None, "sell": None, "mean_target": None,
                "high_target": None, "low_target": None, "as_of": None,
                "source": "none", "note": note}


@dataclass
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


@dataclass
class EftsNarrativeProvider:
    """Live SEC EFTS narrative excerpts."""

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
# Factory — wired at startup
# ---------------------------------------------------------------------------
def build_providers(settings: Any) -> ProviderBundle:
    """Build ProviderBundle.

    When blob storage is configured and ``USE_BLOB_PROVIDERS=true`` (default),
    reads pre-ingested data from Azure Blob Storage instead of making live API calls.
    The data ingestion pipeline (data_ingestion.py) must have run first.

    When blob storage is not configured or ``USE_BLOB_PROVIDERS=false``,
    falls back to live API clients.
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

    # ---- Fall back to live providers ----
    return _build_live_providers(settings)


def _build_live_providers(settings: Any) -> ProviderBundle:
    """Build ProviderBundle from live clients.

    Every provider either works (real API) or returns empty data with a note.
    """
    from .fred import FredClient
    from .fed_h15 import FedH15Client
    from .sec_ixbrl import SecIxbrlClient
    from .sec_efts import SecEftsClient
    from .tiingo import TiingoClient

    sources: list[dict[str, str]] = []

    # ---- Macro: FRED + FOMC H.15 ----
    fred = None
    if getattr(settings, "use_fred", True):
        key = getattr(settings, "fred_api_key", None)
        try:
            fred = FredClient(api_key=key, cache_dir=Path(".cache/fred"))
            sources.append({"source_name": "FRED", "status": "active", "detail": "Federal Reserve Economic Data"})
        except (OSError, ValueError, ImportError) as e:
            sources.append({"source_name": "FRED", "status": "error", "detail": str(e)})
    else:
        sources.append({"source_name": "FRED", "status": "disabled", "detail": "USE_FRED=false"})

    fed_h15 = None
    if getattr(settings, "use_fed_h15", True):
        try:
            fed_h15 = FedH15Client(cache_dir=Path(".cache/fed"))
            sources.append({"source_name": "FOMC H.15", "status": "active", "detail": "US Treasury / Fed rates"})
        except (OSError, ValueError, ImportError) as e:
            sources.append({"source_name": "FOMC H.15", "status": "error", "detail": str(e)})

    macro = FreeMacroProvider(fred=fred, fed_h15=fed_h15)

    # ---- Segments: SEC iXBRL ----
    ixbrl = None
    if getattr(settings, "use_ixbrl_segments", True):
        try:
            ixbrl = SecIxbrlClient(user_agent=settings.sec_user_agent, cache_dir=settings.sec_cache_dir / "ixbrl")
            sources.append({"source_name": "SEC iXBRL", "status": "active", "detail": "Company facts + segment revenue"})
        except (OSError, ValueError, ImportError) as e:
            sources.append({"source_name": "SEC iXBRL", "status": "error", "detail": str(e)})

    segments = IxbrlSegmentProvider(ixbrl=ixbrl)

    # ---- Fundamentals: SEC iXBRL (same client, different method) ----------
    fundamentals = IxbrlFundamentalsProvider(ixbrl=ixbrl)
    if getattr(settings, "use_ixbrl_segments", True):
        sources.append({"source_name": "SEC iXBRL Fundamentals", "status": "active",
                        "detail": "Revenue, EPS, margins, balance sheet, cash flow"})

    # ---- Narrative: SEC EFTS ----
    efts = None
    if getattr(settings, "use_sec_efts", True):
        try:
            efts = SecEftsClient(user_agent=settings.sec_user_agent, cache_dir=settings.sec_cache_dir / "efts")
            sources.append({"source_name": "SEC EFTS", "status": "active", "detail": "Full-text filing search"})
        except (OSError, ValueError, ImportError) as e:
            sources.append({"source_name": "SEC EFTS", "status": "error", "detail": str(e)})

    narrative = EftsNarrativeProvider(efts=efts)

    # ---- Price / Consensus: Tiingo ----
    tiingo = None
    if getattr(settings, "tiingo_api_key", None):
        try:
            tiingo = TiingoClient(api_key=settings.tiingo_api_key, cache_dir=Path(".cache/tiingo"))
            sources.append({"source_name": "Tiingo", "status": "active", "detail": "Prices + analyst estimates"})
        except (OSError, ValueError, ImportError) as e:
            sources.append({"source_name": "Tiingo", "status": "error", "detail": str(e)})
    else:
        sources.append({"source_name": "Tiingo", "status": "missing", "detail": "TIINGO_API_KEY not set"})

    price = TiingoPriceProvider(tiingo=tiingo)
    consensus = TiingoConsensusProvider(tiingo=tiingo)

    return ProviderBundle(
        price=price,
        consensus=consensus,
        fundamentals=fundamentals,
        macro=macro,
        segments=segments,
        narrative=narrative,
        sources=sources,
    )
