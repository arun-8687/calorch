"""Conference agent — investor day / summit preparation."""
from __future__ import annotations

from typing import Any

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    add_macro_table_to,
    base_analysis,
    build_with_template,
    enrich_macro,
    resolve_primary_ticker_and_cik,
    ticker_context,
)
from calorch.state import EventType


def build_conference(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    a_base = base_analysis(f"Conference Brief — {ev.subject}", ev, cls, ed)
    tickers = a_base.tickers or ["AAPL", "MSFT", "NVDA"]
    primary_ticker = tickers[0]
    _, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)
    macro = enrich_macro(providers)

    data_tables: dict[str, Any] = {}
    add_macro_table_to(data_tables, macro)

    ctx = ticker_context(
        ticker=primary_ticker,
        providers=providers,
        event_id=ev.id,
        event_subject=ev.subject,
        event_date=str(ev.start)[:10] if hasattr(ev, "start") else "",
        cik=cik or "",
    )
    ctx.update({
        "conference_name": ev.subject,
        "confidence": cls.confidence,
        "tickers": tickers,
        "event_time": "11:00 AM IST",
    })
    return build_with_template("conference", ctx, data_tables, llm_call, providers)


register(
    AgentSpec(
        event_type=EventType.CONFERENCE,
        analysis_builder=build_conference,
        keywords=("conference", "summit", "expo", "investor day", "cmd", "capital markets day"),
    )
)
