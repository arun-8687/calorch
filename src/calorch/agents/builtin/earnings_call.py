"""Earnings-call agent — quarterly results preparation.

SEC iXBRL supplies the financial tables (revenue, EPS, margins, balance
sheet, product/geographic segments); AlphaSense supplies sentiment and the
qualitative guidance/transcript context. There is no price, consensus or
macro data — those market-data fields render as "—".
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
    enrich_geo,
    enrich_segments,
    enrich_sentiment,
    fmt_b,
    fmt_pct,
    fmt_price,
    fmt_x,
    resolve_primary_ticker_and_cik,
)
from calorch.state import EventType

_DASH = "—"


def build_earnings_call(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch._earnings_helpers import _build_geo_table_pct, _build_segment_table_pct

    a_base = base_analysis(f"Earnings Filing Brief — {ev.subject}", ev, cls, ed)
    primary_ticker, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)

    # ---- SEC iXBRL fundamentals + segments ----
    funds: dict[str, Any] = {}
    if providers and cik and primary_ticker:
        funds = providers.fundamentals.latest_fundamentals(cik, primary_ticker) or {}
    seg = enrich_segments(providers, cik, primary_ticker)
    geo = enrich_geo(providers, cik, primary_ticker)
    sentiment = enrich_sentiment(providers, primary_ticker)

    # ---- data tables (SEC financials + segments + AlphaSense sentiment) ----
    data_tables: dict[str, Any] = {}
    data_tables["financials"] = {
        "headers": ["Metric", "Value"],
        "rows": [
            ["Revenue", fmt_b(funds.get("revenue"))],
            ["Net income", fmt_b(funds.get("net_income"))],
            ["EPS (diluted)", fmt_price(funds.get("eps_diluted"))],
            ["Gross margin", fmt_pct(funds.get("gross_margin"))],
            ["Operating margin", fmt_pct(funds.get("operating_margin"))],
            ["Net margin", fmt_pct(funds.get("net_margin"))],
            ["ROE", fmt_pct(funds.get("roe"))],
            ["ROA", fmt_pct(funds.get("roa"))],
        ],
    }
    data_tables["balance_sheet"] = {
        "headers": ["Metric", "Value"],
        "rows": [
            ["Cash & equivalents", fmt_b(funds.get("cash"))],
            ["Long-term debt", fmt_b(funds.get("long_term_debt"))],
            ["Net debt", fmt_b(funds.get("net_debt"))],
            ["Debt / equity", fmt_x(funds.get("debt_equity"))],
        ],
    }
    sp = _build_segment_table_pct(seg)
    if sp:
        data_tables["segments"] = sp
    gp = _build_geo_table_pct(geo)
    if gp:
        data_tables["geo"] = gp
    add_sentiment_table_to(data_tables, sentiment)

    # ---- context (all template keys present; market data → "—") ----
    ctx = {
        "event_id": ev.id,
        "company_name": funds.get("company_name") or funds.get("company") or ed.get("company", primary_ticker or ""),
        "primary_ticker": primary_ticker or "",
        "quarter": ed.get("quarter", "Q2 FY2026"),
        "event_date": ev.start.dateTime[:10] if hasattr(ev.start, "dateTime") else str(ev.start),
        "event_time": "8:00 PM IST",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "last_quarter_label": "Q1 FY2026",
        "next_quarter_label": "Q2 FY2026",
        "prev_quarter_label": "Q4 FY2025",
        "prior_year_quarter_label": "Q1 FY2025",
        # ---- SEC iXBRL fundamentals ----
        "rev_actual": fmt_b(funds.get("revenue")),
        "eps_actual": fmt_price(funds.get("eps_diluted")),
        "net_income": fmt_b(funds.get("net_income")),
        "gross_margin": fmt_pct(funds.get("gross_margin")),
        "operating_margin": fmt_pct(funds.get("operating_margin")),
        "net_margin": fmt_pct(funds.get("net_margin")),
        "roe": fmt_pct(funds.get("roe")),
        "roa": fmt_pct(funds.get("roa")),
        "cash": fmt_b(funds.get("cash")),
        "total_debt": fmt_b(funds.get("long_term_debt")),
        "net_debt": fmt_b(funds.get("net_debt")),
        "debt_equity": fmt_x(funds.get("debt_equity")),
        "current_ratio": fmt_x(funds.get("current_ratio")),
        # ---- AlphaSense sentiment ----
        "sentiment_label": (sentiment or {}).get("label", _DASH) if sentiment else _DASH,
        "sentiment_score": f"{sentiment['mean_sentiment']:+.2f}" if sentiment else _DASH,
        # ---- market data: no SEC/AlphaSense source ----
        "eps_estimate": _DASH, "eps_surprise": _DASH, "rev_estimate": _DASH, "rev_surprise": _DASH,
        "eps_q": _DASH, "eps_range": _DASH, "rev_q": _DASH, "rev_range": _DASH, "num_analysts": _DASH,
        "pe_ttm": _DASH, "forward_pe": _DASH, "ev_ebitda": _DASH, "price_sales": _DASH, "price_book": _DASH,
        "consensus_rating": _DASH, "buy": _DASH, "buy_pct": _DASH, "hold": _DASH, "hold_pct": _DASH,
        "sell": _DASH, "sell_pct": _DASH, "mean_target": _DASH, "price": _DASH,
        "perf_1w": _DASH, "perf_1m": _DASH, "perf_ytd": _DASH, "range_52w": _DASH,
        "esg_score": "Low Risk (Top quartile in sector)",
        "esg_env": "Carbon neutral operations; 2030 full supply chain target",
        "esg_social": "Strong privacy positioning; supply chain labor scrutiny",
        "esg_gov": "Dual-class: No. Board independence: High. CEO tenure: strong",
    }

    return build_with_template("earnings_call", ctx, data_tables, llm_call, providers)


register(
    AgentSpec(
        event_type=EventType.EARNINGS_CALL,
        analysis_builder=build_earnings_call,
        keywords=("earnings", "results", "guidance", "q1", "q2", "q3", "q4", "fy"),
    )
)
