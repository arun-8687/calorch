"""Management-meeting agent — executive 1:1 / town-hall preparation."""
from __future__ import annotations

from typing import Any

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    add_sentiment_table_to,
    base_analysis,
    build_with_template,
    enrich_guidance,
    enrich_segments,
    enrich_sentiment,
    event_datetime_ctx,
    fmt_pct,
    guidance_excerpts_str,
    latest_filing_str,
    narrative_docs_table,
    recent_doc_titles_str,
    resolve_primary_ticker_and_cik,
    segment_table_rows,
    ticker_context,
    ticker_trends,
)
from calorch import fin_metrics as fm
from calorch.state import EventType

_DASH = "—"


def build_management_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    role = "CEO"
    for r in ("CEO", "CFO", "CRO", "CTO"):
        if r in ev.subject.upper():
            role = r
            break

    a_base = base_analysis(f"Management Meeting — {role}", ev, cls, ed)
    primary_ticker, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)
    edt = event_datetime_ctx(ev)

    seg = enrich_segments(providers, cik, primary_ticker)
    sentiment = enrich_sentiment(providers, primary_ticker)
    narrative_hits = enrich_guidance(providers, cik, primary_ticker)

    funds: dict[str, Any] = {}
    if providers and cik and primary_ticker:
        funds = providers.fundamentals.latest_fundamentals(cik, primary_ticker) or {}
    trends = (
        ticker_trends(primary_ticker, providers, cik)
        if (providers and cik and primary_ticker)
        else {"history": {}, "tables": {}}
    )
    history = trends["history"]

    data_tables: dict[str, Any] = {}
    add_sentiment_table_to(data_tables, sentiment)
    if seg:
        data_tables["product_segments"] = {
            "headers": [f"Segment ({primary_ticker})", "Revenue", "Period end"],
            "rows": segment_table_rows(seg),
        }
    data_tables.update(trends["tables"])
    nd = narrative_docs_table(narrative_hits)
    if nd:
        data_tables["narrative_docs"] = nd

    ctx = ticker_context(
        ticker=primary_ticker or "",
        providers=providers,
        event_id=ev.id,
        event_subject=ev.subject,
        event_date=edt["event_date"],
        event_time=edt["event_time"],
        cik=cik or "",
    )

    rev_yoy = fm.yoy(history, "revenue")
    op_margin_delta = fm.margin_delta(history, "operating_margin")
    rd = funds.get("rd_expense")
    rev = funds.get("revenue")
    rd_pct = rd / rev * 100 if isinstance(rd, (int, float)) and isinstance(rev, (int, float)) and rev else None

    ctx.update({
        "role": role,
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "latest_filing": latest_filing_str(funds),
        "rev_growth": f"{rev_yoy:+.1f}%" if rev_yoy is not None else _DASH,
        "op_margin_delta": f"{op_margin_delta:+.1f}pts" if op_margin_delta is not None else _DASH,
        "rd_pct_revenue": fmt_pct(rd_pct),
        "guidance_excerpts": guidance_excerpts_str(narrative_hits),
        "recent_doc_titles": recent_doc_titles_str(narrative_hits),
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
