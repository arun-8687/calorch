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
# Free-source enrichment: macro box, segment table, guidance excerpts
# ---------------------------------------------------------------------------
def enrich_macro(providers: Any) -> dict[str, dict[str, Any]] | None:
    """Return the FRED/H.15 macro snapshot for the brief, or None on failure."""
    if providers is None or getattr(providers, "macro", None) is None:
        return None
    try:
        snap = providers.macro.snapshot()
    except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
        log.warning("Macro snapshot failed: %s", e)
        return None
    except (KeyError, TypeError, ValueError) as e:
        log.warning("Macro snapshot returned malformed data: %s", e)
        return None
    return snap or None


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
    if providers is None or not ticker or not cik:
        return None
    try:
        return providers.narrative.guidance_hits(cik, ticker, limit=5)
    except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
        log.warning("Guidance fetch failed for %s: %s", ticker, e)
        return None
    except (KeyError, TypeError, ValueError) as e:
        log.warning("Guidance data malformed for %s: %s", ticker, e)
        return None


# ---------------------------------------------------------------------------
# Table helpers
# ---------------------------------------------------------------------------
def add_macro_table_to(data_tables: dict[str, Any], macro: dict[str, Any] | None) -> None:
    """Insert a macro data table if the snapshot is non-empty."""
    if macro:
        data_tables["macro"] = {
            "headers": ["Macro indicator", "Value", "1W Δ", "As of"],
            "rows": macro_table(macro),
        }


def macro_table(snap: dict[str, dict[str, Any]] | None) -> list[list[str]]:
    """Return rows ``[label, value, 1W, date]`` for the macro box."""
    if not snap:
        return [["Macro context unavailable", "—", "—", "—"]]
    label_map = {
        "vix": "VIX",
        "sp500": "S&P 500",
        "treasury_1mo": "1M UST",
        "treasury_3mo": "3M UST",
        "treasury_6mo": "6M UST",
        "treasury_1y": "1Y UST",
        "treasury_2y": "2Y UST",
        "treasury_3y": "3Y UST",
        "treasury_5y": "5Y UST",
        "treasury_7y": "7Y UST",
        "treasury_10y": "10Y UST",
        "treasury_20y": "20Y UST",
        "treasury_30y": "30Y UST",
        "fed_funds": "Fed Funds",
        "wti_oil": "WTI Oil",
        "gold": "Gold",
        "btc_usd": "BTC/USD",
        "usd_eur": "USD/EUR",
        "cpi": "CPI",
        "unemployment": "Unemployment",
    }
    rows: list[list[str]] = []
    seen: set[str] = set()
    for k, entry in snap.items():
        label = label_map.get(k, k)
        if label in seen:
            continue
        seen.add(label)
        val = entry.get("value")
        val_str = f"{val:,.2f}" if isinstance(val, (int, float)) else "—"
        change = entry.get("change_1w")
        if change is None:
            change = entry.get("change_1w_bps")
        if isinstance(change, (int, float)) and abs(change) < 50 and entry.get("change_1w") is not None:
            change_str = f"{change:+.2f}%"
        elif isinstance(change, (int, float)) and entry.get("change_1w_bps") is not None:
            change_str = f"{change:+.0f}bps"
        else:
            change_str = "—"
        rows.append([label, val_str, change_str, entry.get("date", "—")])
    return rows


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
    """Build a template context dict from live provider data for one ticker.

    Priority: SEC iXBRL fundamentals > Tiingo consensus > empty ("—").
    Returns formatted strings ready for template variable substitution.
    """
    price_data = providers.price.quote(ticker) if providers else None
    consensus = providers.consensus.estimates(ticker) if providers else None
    recs = providers.consensus.recommendations(ticker) if providers else None
    consensus = dict(consensus or {})
    if recs:
        consensus.update(recs)
    if price_data:
        consensus.setdefault("price", price_data.get("price"))
        consensus.setdefault("market_cap", price_data.get("market_cap"))

    # SEC iXBRL fundamentals — primary source for financial data
    funds = {}
    if providers and cik:
        try:
            funds = providers.fundamentals.latest_fundamentals(cik, ticker) or {}
        except (KeyError, TypeError, ValueError) as e:
            log.warning("SEC iXBRL fundamentals parse failed for %s: %s", ticker, e)
        except httpx.HTTPError as e:
            log.warning("SEC iXBRL fetch failed for %s: %s", ticker, e)

    p = price_data or {}
    c = consensus
    f = funds

    def _get(*keys: str, fmt_fn=None):
        for k in keys:
            v = f.get(k) or c.get(k)
            if v is not None:
                return fmt_fn(v) if fmt_fn else v
        return "—"

    return {
        "event_id": event_id,
        "primary_ticker": ticker,
        "company_name": f.get("company_name") or c.get("company", ticker),
        "price": fmt_price(p.get("price")),
        "market_cap": fmt_b(p.get("market_cap")),
        "sector": p.get("sector") or "Technology",
        "ceo_name": p.get("ceo_name") or "—",
        "employees": str(p.get("employees") or "—"),
        "consensus_rating": str(c.get("consensus_rating", _get("consensus_rating") or "—")),
        "mean_target": fmt_price(c.get("mean_target")),
        "upside_pct": "—",
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
        "pe_ttm": fmt_x(c.get("pe_ttm")),
        "forward_pe": fmt_x(c.get("forward_pe")),
        "ev_ebitda": fmt_x(c.get("ev_ebitda")),
        "price_sales": fmt_x(c.get("price_sales")),
        "price_book": fmt_x(c.get("price_book")),
        "cash": _get("cash", fmt_fn=fmt_b),
        "total_debt": _get("long_term_debt", fmt_fn=fmt_b),
        "net_debt": _get("net_debt", fmt_fn=fmt_b),
        "debt_equity": _get("debt_equity", fmt_fn=fmt_x),
        "current_ratio": _get("current_ratio", fmt_fn=lambda v: f"{v:.2f}"),
        "buy": str(c.get("buy", "—")),
        "hold": str(c.get("hold", "—")),
        "sell": str(c.get("sell", "—")),
        "buy_pct": str(c.get("buy_pct", "—")),
        "hold_pct": str(c.get("hold_pct", "—")),
        "sell_pct": str(c.get("sell_pct", "—")),
        "num_analysts": str(c.get("num_analysts", "—")),
        "change_1w": f"{p.get('change_1w', 0):+.1f}%" if p.get('change_1w') is not None else "—",
        "change_1m": f"{p.get('change_1m', 0):+.1f}%" if p.get('change_1m') is not None else "—",
        "change_ytd": f"{p.get('ytd_pct', 0):+.1f}%" if p.get('ytd_pct') is not None else "—",
        "range_52w": f"{fmt_price(p.get('52w_low'))} — {fmt_price(p.get('52w_high'))}",
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
    template_name: str,
    context: dict[str, Any],
    data_tables: dict[str, Any] | None,
    llm_call: Any,
    providers: Any,
    *,
    analysis: EventAnalysis | None = None,
) -> EventAnalysis:
    """Instantiate a template, run the TemplateEngine, return the EventAnalysis."""
    from calorch.templates import TemplateEngine, load_template

    tpl = load_template(template_name)
    engine = TemplateEngine(tpl, llm_client=llm_call)
    a = engine.build(
        context=context,
        data_tables=data_tables or {},
        data_sources=data_sources(providers),
    )
    if analysis:
        a.role_focus = analysis.role_focus
    return a
