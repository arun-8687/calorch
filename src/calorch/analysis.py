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
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import httpx

from calorch import fin_metrics as fm
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
# Event date/time — real values from the calendar event, never hardcoded.
# ---------------------------------------------------------------------------
def event_datetime_ctx(ev: Any) -> dict[str, str]:
    """Real ``event_date``/``event_time`` derived from ``ev.start``.

    Handles both a ``datetime`` (the normal ``CalendarEvent.start`` shape)
    and a raw ISO string. Degrades to empty strings — never raises, never
    fabricates a placeholder time.
    """
    start = getattr(ev, "start", None)
    dt: datetime | None = None
    if isinstance(start, datetime):
        dt = start
    elif isinstance(start, str):
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None:
        return {"event_date": "", "event_time": ""}
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return {"event_date": dt.strftime("%Y-%m-%d"), "event_time": dt.strftime("%H:%M UTC")}


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
    """Insert a sentiment table if a score is available (AlphaSense or the
    free SEC-lexicon backend — labelled via ``sentiment['source']``).
    """
    if sentiment and sentiment.get("mean_sentiment") is not None:
        table: dict[str, Any] = {
            "headers": ["Sentiment", "Value"],
            "rows": [
                ["Mean sentiment (-1..1)", f"{sentiment['mean_sentiment']:+.2f}"],
                ["Label", str(sentiment.get("label", "—")).title()],
                ["Documents sampled", str(sentiment.get("sample", "—"))],
            ],
        }
        if sentiment.get("source") == "sec-lexicon":
            table["source_note"] = "Local finance-lexicon score over SEC filing text"
        data_tables["sentiment"] = table


def truncate_text(text: str, limit: int = 200) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def guidance_filings_table(filings_hits: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """SEC EFTS full-text guidance hits -> Date | Form | Excerpt table."""
    if not filings_hits:
        return None
    rows = [
        [h.get("file_date") or "—", h.get("form") or "—", truncate_text(h.get("snippet"), 200) or "—"]
        for h in filings_hits[:8]
    ]
    if not rows:
        return None
    return {
        "headers": ["Date", "Form", "Excerpt"],
        "rows": rows,
        "source_note": "Source: SEC EDGAR full-text search (EFTS)",
    }


def _snippet_provenance_suffix(hits: list[dict[str, Any]]) -> str:
    """" (excerpts LLM-selected)" / " (keyword-selected)" / " (mixed)" based on
    each hit's ``snippet_source`` ("llm"|"heuristic", set at ingestion time).

    Hits missing the field entirely (older/ad-hoc data) are ignored; if none
    of the hits carry the field, no suffix is added.
    """
    sources = {h.get("snippet_source") for h in hits if h.get("snippet_source")}
    if not sources:
        return ""
    if sources == {"llm"}:
        return " (excerpts LLM-selected)"
    if sources == {"heuristic"}:
        return " (keyword-selected)"
    return " (mixed)"


def narrative_docs_table(narrative_hits: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """SEC narrative / AlphaSense guidance excerpts -> Date | Type | Excerpt table."""
    if not narrative_hits:
        return None
    hits = narrative_hits[:8]
    rows = [
        [h.get("date") or "—", h.get("type") or "—", truncate_text(h.get("snippet"), 200) or "—"]
        for h in hits
    ]
    if not rows:
        return None
    return {
        "headers": ["Date", "Type", "Excerpt"],
        "rows": rows,
        "source_note": "Source: SEC 8-K press release / MD&A" + _snippet_provenance_suffix(hits),
    }


def guidance_excerpts_str(narrative_hits: list[dict[str, Any]] | None, n: int = 3) -> str:
    """Top-``n`` narrative snippets joined for LLM context (``guidance_excerpts``)."""
    if not narrative_hits:
        return "—"
    parts = [truncate_text(h.get("snippet"), 200) for h in narrative_hits[:n] if h.get("snippet")]
    return " | ".join(parts) if parts else "—"


_GENERIC_EMAIL_DOMAINS = {"gmail", "outlook", "hotmail", "yahoo", "icloud", "me", "aol"}


def counterpart_from_event(ev: Any) -> tuple[str, str]:
    """Counterpart name/affiliation from the event organizer / first external attendee.

    Name prefers ``ev.organizer`` (a display name in the calendar payload);
    affiliation is guessed from the first attendee email's domain, skipping
    generic consumer webmail domains. Degrades to ("—", "—") — never
    fabricates a name or firm.
    """
    name = getattr(ev, "organizer", "") or ""
    firm = ""
    for addr in getattr(ev, "attendees", []) or []:
        if "@" not in addr:
            continue
        local, _, domain = addr.partition("@")
        domain_root = domain.split(".")[0].lower()
        if domain_root in _GENERIC_EMAIL_DOMAINS:
            continue
        if not name:
            name = local.replace(".", " ").title()
        firm = domain_root.replace("-", " ").title()
        break
    return name or "—", firm or "—"


def latest_filing_str(funds: dict[str, Any] | None) -> str:
    """"{form} — period ended {period}" from a ``latest_fundamentals()`` dict, or "—"."""
    if not funds:
        return "—"
    form = funds.get("revenue_form")
    period = funds.get("revenue_period")
    if form and period:
        return f"{form} — period ended {period}"
    return "—"


def recent_doc_titles_str(narrative_hits: list[dict[str, Any]] | None, n: int = 5) -> str:
    """Recent document titles joined for LLM context (``recent_doc_titles``)."""
    if not narrative_hits:
        return "—"
    titles = [h.get("title") for h in narrative_hits[:n] if h.get("title")]
    return " | ".join(titles) if titles else "—"


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


def _cash_flow_rows(history: dict[str, Any]) -> list[list[str]]:
    """OCF / CapEx / FCF / FCF margin / Buybacks / Dividends — latest Q + TTM."""
    quarterly = history.get("quarterly") or []
    if not quarterly:
        return []
    latest = quarterly[0]

    def _ttm_b(key: str) -> str:
        v = fm.ttm(history, key)
        return fmt_b(v) if v is not None else "—"

    rows = [
        ["Operating cash flow", fmt_b(latest.get("ocf")), _ttm_b("ocf")],
        ["CapEx", fmt_b(latest.get("capex")), _ttm_b("capex")],
        ["Free cash flow", fmt_b(latest.get("fcf")), _ttm_b("fcf")],
        ["FCF margin", fmt_pct(latest.get("fcf_margin")), _fcf_margin_ttm(history)],
        ["Buybacks", fmt_b(latest.get("buybacks")), _ttm_b("buybacks")],
        ["Dividends paid", fmt_b(latest.get("dividends_paid")), _ttm_b("dividends_paid")],
    ]
    return rows


def _fcf_margin_ttm(history: dict[str, Any]) -> str:
    fcf_ttm = fm.ttm(history, "fcf")
    rev_ttm = fm.ttm(history, "revenue")
    if fcf_ttm is None or not rev_ttm:
        return "—"
    return fmt_pct(fcf_ttm / rev_ttm * 100)


def _fcf_conversion_trend_str(history: dict[str, Any], *, max_quarters: int = 5) -> str:
    """Per-quarter FCF/net-income conversion ratio, for the "what changed"
    LLM prompt — makes FCF-diverging-from-earnings visible even though
    :func:`fin_metrics.trend_summary_strings` doesn't track it (it only
    covers revenue/margin/FCF, not the FCF-vs-NI comparison).
    """
    quarterly = (history.get("quarterly") or [])[:max_quarters]
    parts: list[str] = []
    for r in quarterly:
        fcf = r.get("fcf")
        ni = r.get("net_income")
        if not isinstance(fcf, (int, float)) or not isinstance(ni, (int, float)) or not ni:
            continue
        label = r.get("label", "—")
        parts.append(f"{label}: FCF/NI {fcf / ni * 100:.0f}%")
    return " | ".join(parts) if parts else "—"


def ticker_trends(ticker: str, providers: Any, cik: str | None) -> dict[str, Any]:
    """Multi-quarter SEC trend data for one ticker: tables + LLM context.

    Degrades cleanly to an empty shape when ``providers``/``cik`` are
    missing or the backend has no history (native iXBRL backend, blob
    miss): ``tables`` is empty and every ``ctx``/``strings`` value is a
    safe default, so downstream template rows simply suppress.
    """
    history: dict[str, Any] = {}
    if providers and cik and ticker:
        try:
            history = providers.fundamentals.fundamentals_history(cik, ticker, quarters=5) or {}
        except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
            log.warning("fundamentals_history fetch failed for %s: %s", ticker, e)
        except (KeyError, TypeError, ValueError) as e:
            log.warning("fundamentals_history parse failed for %s: %s", ticker, e)

    quarterly = history.get("quarterly") or []
    tables: dict[str, Any] = {}

    trend_rows = fm.trend_rows(history)
    if quarterly and trend_rows:
        tables["quarterly_trend"] = {
            "headers": fm.trend_headers(history),
            "rows": trend_rows,
            "source_note": "Source: SEC quarterly filings (XBRL)",
        }

    if quarterly and quarterly[0].get("ocf") is not None:
        cf_rows = _cash_flow_rows(history)
        if cf_rows:
            tables["cash_flow"] = {
                "headers": ["Metric", quarterly[0].get("label", "Latest Q"), "TTM"],
                "rows": cf_rows,
                "source_note": "Source: SEC quarterly filings (XBRL)",
            }

    ctx: dict[str, Any] = {}
    if quarterly:
        ctx["last_quarter_label"] = quarterly[0].get("label") or "latest quarter"
        if len(quarterly) > 1:
            ctx["prev_quarter_label"] = quarterly[1].get("label") or "—"
        if len(quarterly) > 4:
            ctx["prior_year_quarter_label"] = quarterly[4].get("label") or "—"
        rev_yoy = fm.yoy(history, "revenue")
        if rev_yoy is not None:
            ctx["rev_yoy"] = f"{rev_yoy:+.1f}%"
        eps_yoy = fm.yoy(history, "eps_diluted")
        if eps_yoy is not None:
            ctx["eps_yoy"] = f"{eps_yoy:+.1f}%"

    strings = fm.trend_summary_strings(history)
    strings["fcf_conversion_trend"] = _fcf_conversion_trend_str(history)

    return {
        "history": history,
        "tables": tables,
        "ctx": ctx,
        "strings": strings,
    }


def ticker_context(
    ticker: str,
    providers: Any,
    *,
    event_id: str = "",
    event_subject: str = "",
    event_date: str = "",
    event_time: str = "",
    cik: str = "",
    trends: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a template context dict for one ticker from SEC fundamentals.

    Every figure here comes from SEC iXBRL company facts (or the current
    quarter's fundamentals history). There is no price/consensus/valuation
    source, so those fields are simply absent — a template row referencing
    a key that isn't here resolves to an unfilled ``{placeholder}`` and the
    engine's dash-row suppression drops it, rather than us fabricating "—".

    ``trends``: pass the return value of a prior :func:`ticker_trends` call
    when the caller already fetched it, so ``fundamentals_history`` isn't
    hit twice (``ticker_trends`` fetches it internally) for a single
    ticker/event. Omit it and this fetches trends itself, as before.
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
    if trends is None:
        trends = ticker_trends(ticker, providers, cik) if (providers and cik) else {"ctx": {}, "strings": {}}
    tctx = trends.get("ctx", {})

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
        # ---- fiscal labels: real, from fundamentals_history; safe fallback ----
        "last_quarter_label": tctx.get("last_quarter_label", "latest quarter"),
        "prev_quarter_label": tctx.get("prev_quarter_label", "—"),
        "prior_year_quarter_label": tctx.get("prior_year_quarter_label", "—"),
        "rev_yoy": tctx.get("rev_yoy", "—"),
        "eps_yoy": tctx.get("eps_yoy", "—"),
        # ---- SEC iXBRL fundamentals ----
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
        "ocf": _get("ocf", fmt_fn=fmt_b),
        "fcf": _get("fcf", fmt_fn=fmt_b),
        "fcf_margin": _get("fcf_margin", fmt_fn=fmt_pct),
        "event_date": event_date,
        "event_time": event_time,
        "conference_name": event_subject,
        "confidence": 0.0,
        "tickers": [ticker],
        # ---- LLM context: multi-quarter trend strings ----
        "revenue_trend": trends.get("strings", {}).get("revenue_trend", "—"),
        "margin_trend": trends.get("strings", {}).get("margin_trend", "—"),
        "fcf_trend": trends.get("strings", {}).get("fcf_trend", "—"),
        "fcf_conversion_trend": trends.get("strings", {}).get("fcf_conversion_trend", "—"),
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
