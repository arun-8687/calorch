"""Analyst-meeting agent — sell-side / buy-side meeting preparation."""
from __future__ import annotations

from typing import Any

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    add_sentiment_table_to,
    base_analysis,
    build_with_template,
    counterpart_from_event,
    enrich_sentiment,
    event_datetime_ctx,
    latest_filing_str,
    resolve_primary_ticker_and_cik,
    ticker_context,
    ticker_trends,
)
from calorch.state import EventType


def build_analyst_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    a_base = base_analysis(f"Analyst Meeting — {ev.subject}", ev, cls, ed)
    primary_ticker, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)
    edt = event_datetime_ctx(ev)

    sentiment = enrich_sentiment(providers, primary_ticker)
    funds: dict[str, Any] = {}
    if providers and cik and primary_ticker:
        funds = providers.fundamentals.latest_fundamentals(cik, primary_ticker) or {}
    trends = (
        ticker_trends(primary_ticker, providers, cik)
        if (providers and cik and primary_ticker)
        else {"tables": {}}
    )

    ctx = ticker_context(
        ticker=primary_ticker or "",
        providers=providers,
        event_id=ev.id,
        event_subject=ev.subject,
        event_date=edt["event_date"],
        event_time=edt["event_time"],
        cik=cik or "",
        trends=trends,
    )
    name, firm = counterpart_from_event(ev)
    ctx.update({
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "counterpart_name": name,
        "counterpart_firm": firm,
        "latest_filing": latest_filing_str(funds),
    })

    data_tables: dict[str, Any] = {}
    add_sentiment_table_to(data_tables, sentiment)
    data_tables.update(trends["tables"])
    fs_rows = [
        [label, ctx[key]] for label, key in (
            ("Revenue", "rev_actual"), ("EPS (Diluted)", "eps_actual"),
            ("Gross Margin", "gross_margin"), ("Operating Margin", "operating_margin"),
            ("Net Margin", "net_margin"), ("ROE", "roe"), ("ROA", "roa"),
            ("Cash", "cash"), ("Net Debt", "net_debt"), ("FCF Margin", "fcf_margin"),
        )
    ]
    data_tables["fundamentals_snapshot"] = {
        "headers": ["Metric", "Value"], "rows": fs_rows,
        "source_note": "Source: SEC company facts (XBRL), latest reported period",
    }

    return build_with_template("analyst_meeting", ctx, data_tables, llm_call, providers)


register(
    AgentSpec(
        event_type=EventType.ANALYST_MEETING,
        analysis_builder=build_analyst_meeting,
        keywords=("analyst", "broker", "sell-side", "buy-side"),
    )
)
