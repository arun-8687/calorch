"""Analysis model and the shared toolkit used by event-type agents.

This is the input side of report generation: the :class:`EventAnalysis`
container that every agent populates, plus the reusable helpers agents
draw on (provider enrichment, ticker/CIK resolution, template
instantiation, value formatting, macro/segment tables).

The rendering side — turning an ``EventAnalysis`` into DOCX/HTML — lives
in :mod:`calorch.renderers`. Keeping the two apart lets each agent module
depend only on this toolkit, never on the renderers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from calorch.state import CalendarEvent, ClassificationResult, EventType
from calorch.telemetry import start_span

log = logging.getLogger("calorch.analysis")


# ---------------------------------------------------------------------------
# Analysis container — what each event agent returns before rendering.
# ---------------------------------------------------------------------------
@dataclass
class EventAnalysis:
    event_id: str
    event_type: EventType
    title: str
    sections: list[tuple[str, list[str]]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    source_attribution: str = ""
    role_focus: str = ""
    confidence: float = 0.0
    data_sources: list[dict[str, str]] = field(default_factory=list)
    """[{source_name, status, detail}] for the Data Sources table at report end."""


# ---------------------------------------------------------------------------
# Value formatters
# ---------------------------------------------------------------------------
def fmt_price(val):
    if val is None:
        return "—"
    return f"${val:,.2f}"


def fmt_b(val):
    if val is None:
        return "—"
    return f"${val / 1e9:,.2f}B"


def fmt_pct(val):
    if val is None:
        return "—"
    return f"{val:,.1f}%"


def fmt_x(val):
    if val is None:
        return "—"
    return f"{val:.1f}x"


# ---------------------------------------------------------------------------
# Subject / ticker helpers
# ---------------------------------------------------------------------------
def tickers_from_subject(subject: str) -> list[str]:
    """Extract valid tickers from subject text, excluding false positives."""
    from calorch.nodes import _tickers

    return _tickers(subject)


def base_analysis(
    title: str, ev: CalendarEvent, cls: ClassificationResult, ed: dict[str, Any]
) -> EventAnalysis:
    """Build the EventAnalysis skeleton shared by all builders."""
    return EventAnalysis(
        event_id=ev.id,
        event_type=cls.final_label,
        title=title,
        confidence=cls.confidence,
        tickers=tickers_from_subject(ev.subject) or list(ed.get("snapshots", {}).keys())[:3],
        source_attribution=(
            f"Source: {ed.get('source', 'mock-enterprise-data')} @ {ed.get('as_of', '')}"
        ),
    )


def resolve_primary_ticker_and_cik(
    a_base: EventAnalysis, cik_lookup: Any
) -> tuple[str | None, str | None]:
    """Resolve primary ticker and CIK from an analysis base."""
    primary_ticker = (a_base.tickers or [None])[0]
    cik = None
    if cik_lookup and primary_ticker:
        try:
            cik = cik_lookup(primary_ticker)
        except (KeyError, ValueError) as e:
            log.debug("CIK lookup miss for %s: %s", primary_ticker, e)
        except httpx.HTTPError as e:
            log.warning("CIK lookup network error for %s: %s", primary_ticker, e)
    return primary_ticker, cik


# ---------------------------------------------------------------------------
# Enrichment: SEC segments/filings + AlphaSense guidance/transcripts/sentiment
# ---------------------------------------------------------------------------
def enrich_segments(providers: Any, cik: str | None, ticker: str | None) -> list[dict[str, Any]] | None:
    if providers is None or not ticker or not cik:
        return None
    try:
        return providers.segments.latest_segments(cik, ticker, axis="product")
    except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
        log.warning("Segment fetch failed for %s: %s", ticker, e)
        return None
    except (KeyError, TypeError, ValueError) as e:
        log.warning("Segment data malformed for %s: %s", ticker, e)
        return None


def enrich_geo(providers: Any, cik: str | None, ticker: str | None) -> list[dict[str, Any]] | None:
    if providers is None or not ticker or not cik:
        return None
    try:
        return providers.segments.latest_segments(cik, ticker, axis="geographic")
    except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
        log.warning("Geographic fetch failed for %s: %s", ticker, e)
        return None
    except (KeyError, TypeError, ValueError) as e:
        log.warning("Geographic data malformed for %s: %s", ticker, e)
        return None


def enrich_guidance(providers: Any, cik: str | None, ticker: str | None) -> list[dict[str, Any]] | None:
    """AlphaSense guidance/outlook excerpts (cik accepted for call-site symmetry)."""
    if providers is None or not ticker:
        return None
    try:
        return providers.narrative.guidance_hits(cik or "", ticker, limit=5)
    except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
        log.warning("Guidance fetch failed for %s: %s", ticker, e)
        return None
    except (KeyError, TypeError, ValueError) as e:
        log.warning("Guidance data malformed for %s: %s", ticker, e)
        return None


def enrich_filings(providers: Any, cik: str | None, ticker: str | None) -> list[dict[str, Any]] | None:
    """SEC EFTS filing-guidance excerpts for the brief."""
    if providers is None or not ticker or not cik:
        return None
    try:
        return providers.filings.guidance_hits(cik, ticker, limit=5)
    except (httpx.HTTPError, ConnectionError, TimeoutError, KeyError, TypeError, ValueError) as e:
        log.warning("Filings fetch failed for %s: %s", ticker, e)
        return None


def enrich_transcripts(providers: Any, ticker: str | None) -> list[dict[str, Any]] | None:
    """AlphaSense transcript / expert-call matches for the brief."""
    if providers is None or not ticker:
        return None
    try:
        return providers.transcripts.transcript_hits(ticker, limit=5)
    except (httpx.HTTPError, ConnectionError, TimeoutError, KeyError, TypeError, ValueError) as e:
        log.warning("Transcript fetch failed for %s: %s", ticker, e)
        return None


def enrich_sentiment(providers: Any, ticker: str | None) -> dict[str, Any] | None:
    """AlphaSense aggregate sentiment for the brief, or None when unavailable."""
    if providers is None or not ticker:
        return None
    try:
        snap = providers.sentiment.sentiment(ticker)
    except (httpx.HTTPError, ConnectionError, TimeoutError, KeyError, TypeError, ValueError) as e:
        log.warning("Sentiment fetch failed for %s: %s", ticker, e)
        return None
    return snap if snap and snap.get("mean_sentiment") is not None else None


# ---------------------------------------------------------------------------
# Table helpers
# ---------------------------------------------------------------------------
def add_sentiment_table_to(data_tables: dict[str, Any], sentiment: dict[str, Any] | None) -> None:
    """Insert an AlphaSense sentiment table if a score is available."""
    if sentiment and sentiment.get("mean_sentiment") is not None:
        data_tables["sentiment"] = {
            "headers": ["AlphaSense sentiment", "Value"],
            "rows": [
                ["Mean sentiment (-1..1)", f"{sentiment['mean_sentiment']:+.2f}"],
                ["Label", str(sentiment.get("label", "—")).title()],
                ["Documents sampled", str(sentiment.get("sample", "—"))],
            ],
        }


def segment_table_rows(seg: list[dict[str, Any]] | None) -> list[list[str]]:
    if not seg:
        return []
    rows: list[list[str]] = []
    for d in seg[:6]:
        label = d.get("segment_label") or d.get("segment_member", "—")
        val = d.get("value")
        period = d.get("period_end", "")
        if isinstance(val, (int, float)):
            val_str = f"${val/1e9:.2f}B"
        else:
            val_str = "—"
        rows.append([label, val_str, period])
    return rows


# ---------------------------------------------------------------------------
# Provider-driven ticker context (consumed by several builders)
# ---------------------------------------------------------------------------
def data_sources(providers: Any) -> list[dict[str, Any]]:
    return providers.sources if providers else []


def ticker_context(
    ticker: str,
    providers: Any,
    *,
    event_id: str = "",
    event_subject: str = "",
    event_date: str = "",
    cik: str = "",
) -> dict[str, Any]:
    """Build a template context dict for one ticker from SEC fundamentals.

    Financial figures come from SEC iXBRL company facts. Market-data fields
    (price, valuation multiples, analyst consensus) have no SEC/AlphaSense
    source and render as "—"; the qualitative side is supplied separately by
    the AlphaSense narrative/transcript/sentiment helpers.
    """
    funds: dict[str, Any] = {}
    if providers and cik:
        try:
            funds = providers.fundamentals.latest_fundamentals(cik, ticker) or {}
        except (KeyError, TypeError, ValueError) as e:
            log.warning("SEC iXBRL fundamentals parse failed for %s: %s", ticker, e)
        except httpx.HTTPError as e:
            log.warning("SEC iXBRL fetch failed for %s: %s", ticker, e)

    f = funds

    def _get(*keys: str, fmt_fn=None):
        for k in keys:
            v = f.get(k)
            if v is not None:
                return fmt_fn(v) if fmt_fn else v
        return "—"

    return {
        "event_id": event_id,
        "primary_ticker": ticker,
        "company_name": f.get("company_name") or f.get("company") or ticker,
        # ---- market data: no SEC/AlphaSense source ----
        "price": "—",
        "market_cap": "—",
        "sector": "—",
        "ceo_name": "—",
        "employees": "—",
        "consensus_rating": "—",
        "mean_target": "—",
        "upside_pct": "—",
        "pe_ttm": "—",
        "forward_pe": "—",
        "ev_ebitda": "—",
        "price_sales": "—",
        "price_book": "—",
        "buy": "—", "hold": "—", "sell": "—",
        "buy_pct": "—", "hold_pct": "—", "sell_pct": "—",
        "num_analysts": "—",
        "change_1w": "—", "change_1m": "—", "change_ytd": "—",
        "range_52w": "—",
        # ---- SEC iXBRL fundamentals ----
        "last_quarter_label": "Q1 FY2026",
        "rev_actual": _get("revenue", fmt_fn=fmt_b),
        "eps_actual": _get("eps_diluted", fmt_fn=fmt_price),
        "net_income": _get("net_income", fmt_fn=fmt_b),
        "operating_income": _get("operating_income", fmt_fn=fmt_b),
        "gross_margin": _get("gross_margin", fmt_fn=fmt_pct),
        "operating_margin": _get("operating_margin", fmt_fn=fmt_pct),
        "net_margin": _get("net_margin", fmt_fn=fmt_pct),
        "roe": _get("roe", fmt_fn=fmt_pct),
        "roa": _get("roa", fmt_fn=fmt_pct),
        "cash": _get("cash", fmt_fn=fmt_b),
        "total_debt": _get("long_term_debt", fmt_fn=fmt_b),
        "net_debt": _get("net_debt", fmt_fn=fmt_b),
        "debt_equity": _get("debt_equity", fmt_fn=fmt_x),
        "current_ratio": _get("current_ratio", fmt_fn=lambda v: f"{v:.2f}"),
        "event_date": event_date,
        "event_time": "09:00 AM IST",
        "conference_name": event_subject,
        "confidence": 0.0,
        "tickers": [ticker],
    }


# ---------------------------------------------------------------------------
# Template instantiation
# ---------------------------------------------------------------------------
def build_with_template(
    template: str | Path,
    context: dict[str, Any],
    data_tables: dict[str, Any] | None,
    llm_call: Any,
    providers: Any,
    *,
    analysis: EventAnalysis | None = None,
) -> EventAnalysis:
    """Instantiate a template, run the TemplateEngine, return the EventAnalysis.

    ``template`` is a built-in template name or an explicit ``Path`` to a
    template file (the latter lets out-of-tree agents ship their own).
    """
    from calorch.templates import TemplateEngine, load_template

    tpl = load_template(template)
    engine = TemplateEngine(tpl, llm_client=llm_call)
    a = engine.build(
        context=context,
        data_tables=data_tables or {},
        data_sources=data_sources(providers),
    )
    if analysis:
        a.role_focus = analysis.role_focus
    return a


# ---------------------------------------------------------------------------
# Registry dispatch
# ---------------------------------------------------------------------------
def build_analysis(
    event_type: EventType,
    event: CalendarEvent,
    cls: ClassificationResult,
    enterprise_data: dict[str, Any],
    llm_call,
    *,
    providers: Any = None,
    cik_lookup: Any = None,
) -> EventAnalysis:
    """Run the analysis builder registered for ``event_type``.

    The builder is resolved through the agent registry, so each event
    type's analysis logic is declared in exactly one place — its module
    under :mod:`calorch.agents.builtin`. ``providers`` is the calorch
    ``ProviderBundle``; builders that accept it pull real macro context
    segment splits (SEC iXBRL) and qualitative context (AlphaSense).
    """
    from calorch.agents import get_agent

    with start_span("calorch.analysis.build", event_type=event_type.value):
        return get_agent(event_type).analysis_builder(
            event, cls, enterprise_data, llm_call,
            providers=providers, cik_lookup=cik_lookup,
        )
