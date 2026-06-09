"""Earnings-call agent — quarterly results preparation.

Everything specific to the ``earnings_call`` event type lives here:
classification keywords and the analysis builder (10-section earnings
brief with XBRL financials, consensus, segments and guidance).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    add_macro_table_to,
    base_analysis,
    build_with_template,
    enrich_geo,
    enrich_macro,
    enrich_segments,
    fmt_b,
    fmt_pct,
    fmt_price,
    fmt_x,
    resolve_primary_ticker_and_cik,
)
from calorch.state import EventType


def build_earnings_call(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch._earnings_helpers import (
        _build_quote_box, _build_last_quarter_table, _build_consensus_table,
        _build_financial_metrics_table, _build_valuation_table, _build_balance_sheet_table,
        _build_analyst_sentiment_table, _build_segment_table_pct, _build_geo_table_pct,
        _build_recent_performance_rows,
    )

    a_base = base_analysis(f"Earnings Filing Brief — {ev.subject}", ev, cls, ed)
    primary_ticker, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)

    # ---- fetch all data ----
    price_data = providers.price.quote(primary_ticker) if providers and primary_ticker else None
    consensus_est = providers.consensus.estimates(primary_ticker) if providers and primary_ticker else None
    recs = providers.consensus.recommendations(primary_ticker) if providers and primary_ticker else None
    macro = enrich_macro(providers)
    seg = enrich_segments(providers, cik, primary_ticker)
    geo = enrich_geo(providers, cik, primary_ticker)

    consensus = dict(consensus_est or {})
    if recs:
        consensus.update(recs)
    if price_data:
        consensus["price"] = price_data.get("price")

    # ---- build data tables ----
    data_tables: dict[str, Any] = {}
    qb = _build_quote_box(primary_ticker, price_data)
    if qb:
        data_tables["quote_box"] = qb
    lq = _build_last_quarter_table(consensus)
    if lq:
        data_tables["last_quarter"] = lq
    ct = _build_consensus_table(consensus)
    if ct:
        data_tables["consensus"] = ct
    fm = _build_financial_metrics_table(consensus)
    if fm:
        data_tables["financial_metrics"] = fm
    vt = _build_valuation_table(consensus)
    if vt:
        data_tables["valuation"] = vt
    bs = _build_balance_sheet_table(consensus)
    if bs:
        data_tables["balance_sheet"] = bs
    sp = _build_segment_table_pct(seg)
    if sp:
        data_tables["segments"] = sp
    gp = _build_geo_table_pct(geo)
    if gp:
        data_tables["geo"] = gp
    ast = _build_analyst_sentiment_table(consensus)
    if ast:
        data_tables["analyst_sentiment"] = ast
    add_macro_table_to(data_tables, macro)
    data_tables["esg"] = {
        "headers": ["Metric", "Value"],
        "rows": [["ESG Risk Score", "{esg_score}"], ["Environmental", "{esg_env}"], ["Social", "{esg_social}"], ["Governance", "{esg_gov}"]],
    }
    data_tables["price_performance"] = {
        "headers": ["Metric", "Value"],
        "rows": _build_recent_performance_rows(price_data),
    }

    # ---- build context ----
    ctx = {
        "event_id": ev.id,
        "company_name": ed.get("company", primary_ticker or ""),
        "primary_ticker": primary_ticker or "",
        "quarter": ed.get("quarter", "Q2 FY2026"),
        "event_date": ev.start.dateTime[:10] if hasattr(ev.start, "dateTime") else str(ev.start),
        "event_time": "8:00 PM IST",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "last_quarter_label": "Q1 FY2026",
        "next_quarter_label": "Q2 FY2026",
        "prev_quarter_label": "Q4 FY2025",
        "prior_year_quarter_label": "Q1 FY2025",
        "eps_actual": fmt_price(consensus.get("eps_actual_q1")),
        "eps_estimate": fmt_price(consensus.get("eps_est_q1")),
        "eps_surprise": f"{consensus.get('eps_surprise', 0):+.2f}%" if consensus.get('eps_surprise') else "—",
        "rev_actual": fmt_b(consensus.get("rev_actual_q1")),
        "rev_estimate": fmt_b(consensus.get("rev_est_q1")),
        "rev_surprise": f"{consensus.get('rev_surprise', 0):+.2f}%" if consensus.get('rev_surprise') else "—",
        "eps_q": fmt_price(consensus.get("eps_q")),
        "eps_range": f"{fmt_price(consensus.get('eps_low'))} — {fmt_price(consensus.get('eps_high'))}",
        "rev_q": fmt_b(consensus.get("rev_q")),
        "rev_range": f"{fmt_b(consensus.get('rev_low'))} — {fmt_b(consensus.get('rev_high'))}",
        "num_analysts": str(consensus.get("num_analysts", "—")),
        "gross_margin": fmt_pct(consensus.get("gross_margin")),
        "operating_margin": fmt_pct(consensus.get("operating_margin")),
        "net_margin": fmt_pct(consensus.get("net_margin")),
        "roe": fmt_pct(consensus.get("roe")),
        "roa": fmt_pct(consensus.get("roa")),
        "pe_ttm": fmt_x(consensus.get("pe_ttm")),
        "forward_pe": fmt_x(consensus.get("forward_pe")),
        "ev_ebitda": fmt_x(consensus.get("ev_ebitda")),
        "price_sales": fmt_x(consensus.get("price_sales")),
        "price_book": fmt_x(consensus.get("price_book")),
        "cash": fmt_b(consensus.get("cash")),
        "total_debt": fmt_b(consensus.get("total_debt")),
        "net_debt": fmt_b(consensus.get("net_debt")),
        "debt_equity": fmt_x(consensus.get("debt_equity")),
        "current_ratio": f"{consensus.get('current_ratio', 0):.2f}" if consensus.get('current_ratio') else "—",
        "consensus_rating": consensus.get("consensus_rating", "Buy"),
        "buy": str(consensus.get("buy", "—")),
        "buy_pct": str(consensus.get("buy_pct", "—")),
        "hold": str(consensus.get("hold", "—")),
        "hold_pct": str(consensus.get("hold_pct", "—")),
        "sell": str(consensus.get("sell", "—")),
        "sell_pct": str(consensus.get("sell_pct", "—")),
        "mean_target": fmt_price(consensus.get("mean_target")),
        "price": fmt_price(consensus.get("price")),
        "perf_1w": f"{price_data.get('change_1w', 0):+.1f}%" if price_data else "—",
        "perf_1m": f"{price_data.get('change_1m', 0):+.1f}%" if price_data else "—",
        "perf_ytd": f"{price_data.get('change_ytd', 0):+.1f}%" if price_data else "—",
        "range_52w": f"{fmt_price(price_data.get('low_52w'))} — {fmt_price(price_data.get('high_52w'))}" if price_data else "—",
        "esg_score": "Low Risk (Top quartile in sector)",
        "esg_env": "Carbon neutral operations; 2030 full supply chain target",
        "esg_social": "Strong privacy positioning; supply chain labor scrutiny",
        "esg_gov": "Dual-class: No. Board independence: High. CEO tenure: strong",
    }

    return build_with_template(
        "earnings_call", ctx, data_tables, llm_call, providers,
    )


register(
    AgentSpec(
        event_type=EventType.EARNINGS_CALL,
        analysis_builder=build_earnings_call,
        keywords=("earnings", "results", "guidance", "q1", "q2", "q3", "q4", "fy"),
    )
)
