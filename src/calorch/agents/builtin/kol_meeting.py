"""KOL-meeting agent — key-opinion-leader / expert call preparation."""
from __future__ import annotations

from typing import Any

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    base_analysis,
    build_with_template,
    counterpart_from_event,
    event_datetime_ctx,
    resolve_primary_ticker_and_cik,
    ticker_context,
)
from calorch.state import EventType


def build_kol_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    a_base = base_analysis(f"KOL Brief — {ev.subject}", ev, cls, ed)
    primary_ticker, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)
    edt = event_datetime_ctx(ev)

    # Expert identity from the calendar payload — never a fabricated persona.
    expert_name, affiliation = counterpart_from_event(ev)

    ctx: dict[str, Any] = {
        "event_id": ev.id,
        "expert_name": expert_name,
        "affiliation": affiliation,
        "meeting_type": "Expert Consultation Call",
        "event_date": edt["event_date"],
        "event_time": edt["event_time"],
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "topic_area": ev.subject,
        "body_preview": ev.body_preview or "",
        "primary_ticker": primary_ticker or "",
        "company_name": primary_ticker or "",
    }

    data_tables: dict[str, Any] = {}
    if primary_ticker and cik:
        tctx = ticker_context(
            ticker=primary_ticker,
            providers=providers,
            event_id=ev.id,
            event_subject=ev.subject,
            event_date=edt["event_date"],
            event_time=edt["event_time"],
            cik=cik,
        )
        ctx["company_name"] = tctx.get("company_name", primary_ticker)
        data_tables["company_context"] = {
            "headers": ["Metric", "Value"],
            "rows": [
                ["Revenue", tctx["rev_actual"]],
                ["Gross Margin", tctx["gross_margin"]],
                ["Operating Margin", tctx["operating_margin"]],
                ["FCF Margin", tctx["fcf_margin"]],
            ],
        }

    return build_with_template("kol_meeting", ctx, data_tables, llm_call, providers)


register(
    AgentSpec(
        event_type=EventType.KOL_MEETING,
        analysis_builder=build_kol_meeting,
        keywords=("kol", "expert", "consultant", "thought leader", "kolsight"),
    )
)
