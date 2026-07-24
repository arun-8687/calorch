"""Earnings-call agent — quarterly results preparation.

SEC iXBRL supplies the financial tables (revenue, EPS, margins, balance
sheet, cash flow, product/geographic segments, multi-quarter trend);
SEC EFTS/narrative supplies guidance excerpts; the sentiment provider
(AlphaSense when configured, else the free SEC-lexicon backend) supplies
tone. There is no price, consensus, valuation or ESG data source, so
those sections are omitted rather than rendered as fabricated "—" rows.
"""
from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    add_sentiment_table_to,
    base_analysis,
    build_with_template,
    enrich_filings,
    enrich_geo,
    enrich_guidance,
    enrich_segments,
    enrich_sentiment,
    event_datetime_ctx,
    fmt_b,
    fmt_pct,
    fmt_price,
    fmt_x,
    guidance_excerpts_str,
    guidance_filings_table,
    narrative_docs_table,
    recent_doc_titles_str,
    resolve_primary_ticker_and_cik,
    ticker_trends,
)
from calorch import fin_metrics as fm
from calorch.state import EventType

_DASH = "—"


def _yoy_str(history: dict[str, Any], key: str, value_str: str) -> str:
    if value_str == _DASH:
        return _DASH
    y = fm.yoy(history, key)
    return f"{value_str} ({y:+.1f}% YoY)" if y is not None else value_str


def _delta_str(history: dict[str, Any], key: str, value_str: str) -> str:
    if value_str == _DASH:
        return _DASH
    d = fm.margin_delta(history, key)
    return f"{value_str} (Δ {d:+.1f}pts vs PY)" if d is not None else value_str


def _ratio(v: Any) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else _DASH


def build_earnings_call(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch._earnings_helpers import _build_geo_table_pct, _build_segment_table_pct

    a_base = base_analysis(f"Earnings Filing Brief — {ev.subject}", ev, cls, ed)
    primary_ticker, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)
    edt = event_datetime_ctx(ev)

    # ---- SEC iXBRL fundamentals + multi-quarter trend + segments ----
    funds: dict[str, Any] = {}
    if providers and cik and primary_ticker:
        funds = providers.fundamentals.latest_fundamentals(cik, primary_ticker) or {}
    trends = (
        ticker_trends(primary_ticker, providers, cik)
        if (providers and cik and primary_ticker)
        else {"history": {}, "tables": {}, "ctx": {}, "strings": {}}
    )
    history = trends["history"]
    tctx = trends["ctx"]
    strings = trends["strings"]

    seg = enrich_segments(providers, cik, primary_ticker)
    geo = enrich_geo(providers, cik, primary_ticker)
    sentiment = enrich_sentiment(providers, primary_ticker)
    narrative_hits = enrich_guidance(providers, cik, primary_ticker)
    filings_hits = enrich_filings(providers, cik, primary_ticker)

    # ---- data tables ----
    data_tables: dict[str, Any] = {}
    sp = _build_segment_table_pct(seg)
    if sp:
        data_tables["segments"] = sp
    gp = _build_geo_table_pct(geo)
    if gp:
        data_tables["geo"] = gp
    add_sentiment_table_to(data_tables, sentiment)
    data_tables.update(trends["tables"])
    nd = narrative_docs_table(narrative_hits)
    if nd:
        data_tables["narrative_docs"] = nd
    gf = guidance_filings_table(filings_hits)
    if gf:
        data_tables["guidance_filings"] = gf

    current_assets = funds.get("current_assets")
    current_liabilities = funds.get("current_liabilities")
    working_capital = (
        fmt_b(current_assets - current_liabilities)
        if isinstance(current_assets, (int, float)) and isinstance(current_liabilities, (int, float))
        else _DASH
    )

    # ---- context ----
    ctx = {
        "event_id": ev.id,
        "company_name": funds.get("company_name") or funds.get("company") or ed.get("company", primary_ticker or ""),
        "primary_ticker": primary_ticker or "",
        "quarter": tctx.get("last_quarter_label", ed.get("quarter", "latest quarter")),
        "event_date": edt["event_date"],
        "event_time": edt["event_time"],
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "last_quarter_label": tctx.get("last_quarter_label", "latest quarter"),
        # ---- Last Quarter Performance: real, with YoY / Δ vs prior year ----
        "rev_actual_display": _yoy_str(history, "revenue", fmt_b(funds.get("revenue"))),
        "eps_actual_display": _yoy_str(history, "eps_diluted", fmt_price(funds.get("eps_diluted"))),
        "gross_margin_display": _delta_str(history, "gross_margin", fmt_pct(funds.get("gross_margin"))),
        "operating_margin_display": _delta_str(history, "operating_margin", fmt_pct(funds.get("operating_margin"))),
        "net_margin_display": _delta_str(history, "net_margin", fmt_pct(funds.get("net_margin"))),
        # ---- return ratios ----
        "roe": fmt_pct(funds.get("roe")),
        "roa": fmt_pct(funds.get("roa")),
        # ---- balance sheet + liquidity ----
        "cash": fmt_b(funds.get("cash")),
        "total_debt": fmt_b(funds.get("long_term_debt")),
        "net_debt": fmt_b(funds.get("net_debt")),
        "debt_equity": fmt_x(funds.get("debt_equity")),
        "current_ratio": _ratio(funds.get("current_ratio")),
        "working_capital": working_capital,
        # ---- sentiment ----
        "sentiment_label": (sentiment or {}).get("label", _DASH) if sentiment else _DASH,
        "sentiment_score": f"{sentiment['mean_sentiment']:+.2f}" if sentiment else _DASH,
        # ---- LLM context: multi-quarter trends + guidance excerpts ----
        "revenue_trend": strings.get("revenue_trend", _DASH),
        "margin_trend": strings.get("margin_trend", _DASH),
        "fcf_trend": strings.get("fcf_trend", _DASH),
        "fcf_conversion_trend": strings.get("fcf_conversion_trend", _DASH),
        "guidance_excerpts": guidance_excerpts_str(narrative_hits),
        "recent_doc_titles": recent_doc_titles_str(narrative_hits),
    }

    return build_with_template("earnings_call", ctx, data_tables, llm_call, providers)


register(
    AgentSpec(
        event_type=EventType.EARNINGS_CALL,
        analysis_builder=build_earnings_call,
        keywords=("earnings", "results", "guidance", "q1", "q2", "q3", "q4", "fy"),
    )
)
