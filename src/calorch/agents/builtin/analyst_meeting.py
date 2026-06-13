"""Analyst-meeting agent — sell-side / buy-side meeting preparation."""
from __future__ import annotations

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    base_analysis,
    build_with_template,
    resolve_primary_ticker_and_cik,
    ticker_context,
)
from calorch.state import EventType


def build_analyst_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    a_base = base_analysis(f"Analyst Meeting — {ev.subject}", ev, cls, ed)
    primary_ticker, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)

    ctx = ticker_context(
        ticker=primary_ticker or "",
        providers=providers,
        event_id=ev.id,
        event_subject=ev.subject,
        event_date=str(ev.start)[:10] if hasattr(ev, "start") else "",
        cik=cik or "",
    )
    ctx.update({
        "event_time": "7:00 PM IST",
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "analyst_name": "Senior Analyst",
        "analyst_firm": "Morgan Stanley",
        "coverage_years": "10",
        "analyst_rating": "Overweight",
        "analyst_target": ctx.get("mean_target", "—"),
    })
    return build_with_template("analyst_meeting", ctx, {}, llm_call, providers)


register(
    AgentSpec(
        event_type=EventType.ANALYST_MEETING,
        analysis_builder=build_analyst_meeting,
        keywords=("analyst", "broker", "sell-side", "buy-side"),
    )
)
