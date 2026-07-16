"""Internal-review agent — retro / sprint-review preparation.

Real pipeline stats from the delivery repository (``providers.ops``,
wired in ``calorch.tools.make_providers``) — the old fabricated "47
names / 12 initiations" coverage stats are gone. An unavailable/empty
repository or watchlist degrades to an honest omission of the affected
sections, never a fabricated table.
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC
from typing import Any

import httpx

from calorch.agents.base import AgentSpec, register
from calorch.analysis import EventAnalysis, build_with_template, event_datetime_ctx, latest_filing_str
from calorch.state import EventType

_DASH = "—"
_WINDOW_DAYS = 90
_MAX_WATCHLIST = 8
_DEGRADE = (httpx.HTTPError, ConnectionError, TimeoutError, KeyError, TypeError, ValueError)


def _parse_updated_at(row: dict[str, Any]) -> datetime | None:
    raw = row.get("updated_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _pipeline_activity_table(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        et = r.get("event_type") or "unknown"
        by_type.setdefault(et, []).append(r)
    if not by_type:
        return None
    table_rows = []
    for et in sorted(by_type):
        recs = by_type[et]
        count = len(recs)
        drafted = sum(1 for r in recs if r.get("email_status") in {"draft", "sent", "prepared"})
        sent = sum(1 for r in recs if r.get("email_status") == "sent")
        confidences = [r.get("confidence") for r in recs if isinstance(r.get("confidence"), (int, float))]
        avg_conf = sum(confidences) / len(confidences) if confidences else None
        table_rows.append([
            et, str(count), str(drafted), str(sent),
            f"{avg_conf:.2f}" if avg_conf is not None else _DASH,
        ])
    return {
        "headers": ["Event Type", "Count", "Drafted", "Sent", "Avg Confidence"],
        "rows": table_rows,
        "source_note": f"Source: delivery repository, last {_WINDOW_DAYS} days",
    }


def _watchlist_coverage_table(providers: Any, cik_lookup: Any) -> dict[str, Any] | None:
    from calorch.config import get_settings

    try:
        watchlist = list(get_settings().sec_watchlist or [])[:_MAX_WATCHLIST]
    except (OSError, ValueError):
        return None

    rows: list[list[str]] = []
    for ticker in watchlist:
        try:
            cik = cik_lookup(ticker)
        except _DEGRADE:
            continue
        if not cik:
            continue
        try:
            funds = providers.fundamentals.latest_fundamentals(cik, ticker) or {}
        except _DEGRADE:
            continue
        filing = latest_filing_str(funds)
        if filing != _DASH:
            rows.append([ticker, filing])
    if not rows:
        return None
    return {"headers": ["Ticker", "Latest Filing"], "rows": rows,
            "source_note": "Source: SEC EDGAR company facts"}


def build_internal_review(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    edt = event_datetime_ctx(ev)

    ctx: dict[str, Any] = {
        "event_id": ev.id,
        "review_type": "Coverage Retro",
        "event_date": edt["event_date"],
        "event_time": edt["event_time"],
        "confidence": cls.confidence,
    }

    data_tables: dict[str, Any] = {}
    ops = getattr(providers, "ops", None) if providers else None
    if ops is not None:
        try:
            all_rows = ops.all() or []
        except (OSError, ValueError, KeyError):
            all_rows = []
        cutoff = datetime.now(tz=UTC) - timedelta(days=_WINDOW_DAYS)
        recent = [r for r in all_rows if (_parse_updated_at(r) or cutoff) >= cutoff]
        pa = _pipeline_activity_table(recent)
        if pa:
            data_tables["pipeline_activity"] = pa

    if providers and cik_lookup:
        wc = _watchlist_coverage_table(providers, cik_lookup)
        if wc:
            data_tables["watchlist_coverage"] = wc

    return build_with_template("internal_review", ctx, data_tables, llm_call, providers)


register(
    AgentSpec(
        event_type=EventType.INTERNAL_REVIEW,
        analysis_builder=build_internal_review,
        keywords=("internal", "retro", "postmortem", "sprint review", "team meeting"),
    )
)
