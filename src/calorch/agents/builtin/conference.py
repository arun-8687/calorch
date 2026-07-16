"""Conference agent — investor day / summit preparation."""
from __future__ import annotations

from typing import Any

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    add_sentiment_table_to,
    base_analysis,
    build_with_template,
    enrich_guidance,
    enrich_sentiment,
    event_datetime_ctx,
    guidance_excerpts_str,
    latest_filing_str,
    narrative_docs_table,
    recent_doc_titles_str,
    resolve_primary_ticker_and_cik,
    ticker_context,
    ticker_trends,
)
from calorch.state import EventType


def build_conference(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    a_base = base_analysis(f"Conference Brief — {ev.subject}", ev, cls, ed)
    tickers = a_base.tickers or ["AAPL", "MSFT", "NVDA"]
    primary_ticker = tickers[0]
    _, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)
    edt = event_datetime_ctx(ev)

    sentiment = enrich_sentiment(providers, primary_ticker)
    narrative_hits = enrich_guidance(providers, cik, primary_ticker)

    funds: dict[str, Any] = {}
    if providers and cik and primary_ticker:
        funds = providers.fundamentals.latest_fundamentals(cik, primary_ticker) or {}
    trends = (
        ticker_trends(primary_ticker, providers, cik)
        if (providers and cik and primary_ticker)
        else {"tables": {}}
    )

    data_tables: dict[str, Any] = {}
    add_sentiment_table_to(data_tables, sentiment)
    data_tables.update(trends["tables"])
    nd = narrative_docs_table(narrative_hits)
    if nd:
        data_tables["narrative_docs"] = nd

    ctx = ticker_context(
        ticker=primary_ticker,
        providers=providers,
        event_id=ev.id,
        event_subject=ev.subject,
        event_date=edt["event_date"],
        event_time=edt["event_time"],
        cik=cik or "",
    )
    ctx.update({
        "conference_name": ev.subject,
        "confidence": cls.confidence,
        "tickers": tickers,
        "latest_filing": latest_filing_str(funds),
        "guidance_excerpts": guidance_excerpts_str(narrative_hits),
        "recent_doc_titles": recent_doc_titles_str(narrative_hits),
    })
    return build_with_template("conference", ctx, data_tables, llm_call, providers)


register(
    AgentSpec(
        event_type=EventType.CONFERENCE,
        analysis_builder=build_conference,
        keywords=("conference", "summit", "expo", "investor day", "cmd", "capital markets day"),
    )
)
