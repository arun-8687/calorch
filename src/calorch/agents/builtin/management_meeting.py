"""Management-meeting agent — executive 1:1 / town-hall preparation."""
from __future__ import annotations

from typing import Any

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    add_sentiment_table_to,
    base_analysis,
    build_with_template,
    enrich_segments,
    enrich_sentiment,
    resolve_primary_ticker_and_cik,
    segment_table_rows,
    ticker_context,
)
from calorch.state import EventType


def build_management_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    role = "CEO"
    for r in ("CEO", "CFO", "CRO", "CTO"):
        if r in ev.subject.upper():
            role = r
            break

    a_base = base_analysis(f"Management Meeting — {role}", ev, cls, ed)
    primary_ticker, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)
    seg = enrich_segments(providers, cik, primary_ticker)

    data_tables: dict[str, Any] = {}
    add_sentiment_table_to(data_tables, enrich_sentiment(providers, primary_ticker))
    if seg:
        data_tables["product_segments"] = {
            "headers": [f"Segment ({primary_ticker})", "Revenue", "Period end"],
            "rows": segment_table_rows(seg),
        }

    ctx = ticker_context(
        ticker=primary_ticker or "",
        providers=providers,
        event_id=ev.id,
        event_subject=ev.subject,
        event_date=str(ev.start)[:10] if hasattr(ev, "start") else "",
        cik=cik or "",
    )
    ctx.update({
        "role": role,
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "event_time": "3:00 PM IST",
        "buy_buy": ctx.get("buy", "—"),
        "hold_hold": ctx.get("hold", "—"),
        "sell_sell": ctx.get("sell", "—"),
        "rev_growth": "—",
        "segment_growth": "—",
        "key_metric_1": "—",
        "key_metric_2": "—",
    })

    a_base.role_focus = role
    return build_with_template(
        "management_meeting", ctx, data_tables, llm_call, providers, analysis=a_base,
    )


register(
    AgentSpec(
        event_type=EventType.MANAGEMENT_MEETING,
        analysis_builder=build_management_meeting,
        keywords=("ceo", "cfo", "cro", "cto", "1on1", "1:1", "town hall", "mgmt"),
    )
)
