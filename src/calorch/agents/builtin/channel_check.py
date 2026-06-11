"""Channel-check agent — distributor / reseller diligence preparation."""
from __future__ import annotations

from typing import Any

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    add_sentiment_table_to,
    base_analysis,
    build_with_template,
    enrich_sentiment,
    resolve_primary_ticker_and_cik,
    ticker_context,
)
from calorch.state import EventType


def build_channel_check(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    a_base = base_analysis(f"Channel Check — {ev.subject}", ev, cls, ed)
    primary_ticker, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)

    data_tables: dict[str, Any] = {}
    add_sentiment_table_to(data_tables, enrich_sentiment(providers, primary_ticker))

    ctx = ticker_context(
        ticker=primary_ticker or "",
        providers=providers,
        event_id=ev.id,
        event_subject=ev.subject,
        event_date=str(ev.start)[:10] if hasattr(ev, "start") else "",
        cik=cik or "",
    )
    ctx.update({
        "sector": "Consumer Electronics / Technology",
        "channel_type": "Supply Chain / Distributor",
        "event_time": "11:00 AM IST",
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "contact_name": "Asia-Pacific Distributor (Foxconn Channel)",
        "location": "Zoom Call",
        "prepared_by": "Investment Research Team",
        "last_quarter_label": "Q1 FY2026",
        "prev_quarter_label": "Q4 FY2025",
        "prior_year_quarter_label": "Q1 FY2025",
        "rev_actual": ctx.get("rev_actual", "—"),
        "rev_ttm": "—",
        "rev_estimate": "—",
        "inventory_days": "—",
        "inventory_days_py": "—",
        "ccc": "—",
        "buyback_q": "—",
        "buyback_ttm": "—",
        "fcf_q": "—",
        "fcf_ttm": "—",
        "capex_q": "—",
        "capex_pct": "—",
        "rd_q": "—",
        "rd_pct": "—",
        "price_target": ctx.get("mean_target", "—"),
        "upside_pct": "—",
        "metric_1": "Primary Revenue",
        "assumption_1": "—",
        "period_1": "—",
        "rationale_1": "—",
        "conf_1": "—",
        "metric_2": "ASP / Pricing",
        "assumption_2": "—",
        "period_2": "—",
        "rationale_2": "—",
        "conf_2": "—",
        "metric_3": "Key Segment Growth",
        "assumption_3": "—",
        "period_3": "—",
        "rationale_3": "—",
        "conf_3": "—",
    })

    return build_with_template(
        "channel_check", ctx, data_tables, llm_call, providers,
    )


register(
    AgentSpec(
        event_type=EventType.CHANNEL_CHECK,
        analysis_builder=build_channel_check,
        keywords=("channel", "distributor", "reseller", "channel partner", "var"),
    )
)
