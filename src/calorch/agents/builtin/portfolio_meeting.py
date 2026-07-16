"""Portfolio-meeting agent — investment-committee / holdings review preparation.

Real data only: a per-ticker snapshot over the configured SEC watchlist
(``settings.sec_watchlist``), each ticker's sentiment score, and recent
EFTS filing hits as "catalysts". There is no price/market-data source, so
the old market_context/sector_performance/holdings tables (all fabricated)
are gone — an unavailable or empty watchlist degrades to an honest
omission of the affected sections, never a fake table.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    base_analysis,
    build_with_template,
    event_datetime_ctx,
    fmt_b,
    fmt_pct,
    truncate_text,
)
from calorch import fin_metrics as fm
from calorch.state import EventType

log = logging.getLogger("calorch.agents.portfolio_meeting")

_DASH = "—"
_MAX_WATCHLIST = 8
_DEGRADE = (httpx.HTTPError, ConnectionError, TimeoutError, KeyError, TypeError, ValueError)


def build_portfolio_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch.config import get_settings

    a_base = base_analysis(f"Portfolio Filing Brief — {ev.subject}", ev, cls, ed)
    edt = event_datetime_ctx(ev)

    watchlist: list[str] = []
    try:
        watchlist = list(get_settings().sec_watchlist or [])[:_MAX_WATCHLIST]
    except (OSError, ValueError) as e:
        log.warning("could not load sec_watchlist: %s", e)

    watchlist_rows: list[list[str]] = []
    sentiment_rows: list[list[str]] = []
    catalyst_rows: list[list[str]] = []
    trend_strings: list[str] = []

    if providers and cik_lookup and watchlist:
        for ticker in watchlist:
            try:
                cik = cik_lookup(ticker)
            except (KeyError, ValueError, httpx.HTTPError) as e:
                log.debug("cik lookup miss for %s: %s", ticker, e)
                continue
            if not cik:
                continue

            try:
                funds = providers.fundamentals.latest_fundamentals(cik, ticker) or {}
                history = providers.fundamentals.fundamentals_history(cik, ticker, quarters=5) or {}
                rev_yoy = fm.yoy(history, "revenue")
                row = [
                    ticker,
                    fmt_b(funds.get("revenue")),
                    f"{rev_yoy:+.1f}%" if rev_yoy is not None else _DASH,
                    fmt_pct(funds.get("operating_margin")),
                    fmt_pct(funds.get("fcf_margin")),
                ]
                if any(c != _DASH for c in row[1:]):
                    watchlist_rows.append(row)
                strings = fm.trend_summary_strings(history)
                if strings.get("revenue_trend") != _DASH:
                    trend_strings.append(f"{ticker}: {strings['revenue_trend']}")
            except _DEGRADE as e:
                log.warning("watchlist fundamentals failed for %s: %s", ticker, e)

            try:
                snap = providers.sentiment.sentiment(ticker)
                if snap and snap.get("mean_sentiment") is not None:
                    sentiment_rows.append(
                        [ticker, f"{snap['mean_sentiment']:+.2f}", str(snap.get("label", "—")).title()]
                    )
            except _DEGRADE as e:
                log.warning("watchlist sentiment failed for %s: %s", ticker, e)

            try:
                hits = providers.filings.guidance_hits(cik, ticker, limit=3) or []
                for h in hits[:2]:
                    catalyst_rows.append(
                        [h.get("file_date") or _DASH, ticker,
                         f"{h.get('form') or '—'}: {truncate_text(h.get('snippet'), 120)}"]
                    )
            except _DEGRADE as e:
                log.warning("watchlist filings failed for %s: %s", ticker, e)

    catalyst_rows.sort(key=lambda r: r[0], reverse=True)
    catalyst_rows = catalyst_rows[:10]

    data_tables: dict[str, Any] = {}
    if watchlist_rows:
        data_tables["watchlist_snapshot"] = {
            "headers": ["Ticker", "Revenue (Latest Q)", "Rev YoY", "Op Margin", "FCF Margin"],
            "rows": watchlist_rows,
            "source_note": "Source: SEC quarterly filings (XBRL)",
        }
    if sentiment_rows:
        data_tables["sentiment_overview"] = {
            "headers": ["Ticker", "Score", "Label"],
            "rows": sentiment_rows,
        }
    if catalyst_rows:
        data_tables["catalysts"] = {
            "headers": ["Date", "Ticker", "Filing"],
            "rows": catalyst_rows,
            "source_note": "Source: SEC EDGAR full-text search (EFTS)",
        }

    ctx = {
        "event_id": ev.id,
        "event_date": edt["event_date"],
        "event_time": edt["event_time"],
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "watchlist_trend": " | ".join(trend_strings) if trend_strings else _DASH,
    }

    return build_with_template(
        "portfolio_meeting", ctx, data_tables, llm_call, providers,
    )


register(
    AgentSpec(
        event_type=EventType.PORTFOLIO_MEETING,
        analysis_builder=build_portfolio_meeting,
        keywords=("portfolio", "ic ", "investment committee", "holdings"),
    )
)
