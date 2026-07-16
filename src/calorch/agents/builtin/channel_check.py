"""Channel-check agent — distributor / reseller diligence preparation."""
from __future__ import annotations

from typing import Any

from calorch.agents.base import AgentSpec, register
from calorch.analysis import (
    EventAnalysis,
    add_sentiment_table_to,
    base_analysis,
    build_with_template,
    enrich_geo,
    enrich_segments,
    enrich_sentiment,
    event_datetime_ctx,
    fmt_b,
    fmt_pct,
    latest_filing_str,
    resolve_primary_ticker_and_cik,
    ticker_context,
    ticker_trends,
)
from calorch import fin_metrics as fm
from calorch.state import EventType

_DASH = "—"


def _ttm_margin(history: dict[str, Any], numerator_key: str) -> float | None:
    num_ttm = fm.ttm(history, numerator_key)
    rev_ttm = fm.ttm(history, "revenue")
    if num_ttm is None or not rev_ttm:
        return None
    return num_ttm / rev_ttm * 100


def _margin_profile_table(history: dict[str, Any]) -> dict[str, Any] | None:
    """Gross/Operating/Net/FCF margin — last 3 quarters + TTM."""
    q = (history.get("quarterly") or [])[:3]
    if not q:
        return None
    headers = ["Metric"] + [r.get("label") or "—" for r in q] + ["TTM"]
    rows = []
    for label, key, num_key in (
        ("Gross Margin", "gross_margin", "gross_profit"),
        ("Operating Margin", "operating_margin", "operating_income"),
        ("Net Margin", "net_margin", "net_income"),
        ("FCF Margin", "fcf_margin", "fcf"),
    ):
        vals = [fmt_pct(r.get(key)) for r in q]
        ttm_val = _ttm_margin(history, num_key)
        rows.append([label, *vals, fmt_pct(ttm_val) if ttm_val is not None else _DASH])
    if all(all(c == _DASH for c in row[1:]) for row in rows):
        return None
    return {"headers": headers, "rows": rows, "source_note": "Source: SEC quarterly filings (XBRL)"}


def _days(v: float | None) -> str:
    return f"{v:.0f} days" if v is not None else _DASH


def _contact_and_location(ev: Any) -> tuple[str, str]:
    organizer = getattr(ev, "organizer", "") or _DASH
    location = getattr(ev, "location", "") or ""
    if not location:
        location = "Online" if getattr(ev, "is_online", False) else _DASH
    return organizer, location


def build_channel_check(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    a_base = base_analysis(f"Channel Check — {ev.subject}", ev, cls, ed)
    primary_ticker, cik = resolve_primary_ticker_and_cik(a_base, cik_lookup)
    edt = event_datetime_ctx(ev)

    seg = enrich_segments(providers, cik, primary_ticker)
    geo = enrich_geo(providers, cik, primary_ticker)
    sentiment = enrich_sentiment(providers, primary_ticker)

    funds: dict[str, Any] = {}
    if providers and cik and primary_ticker:
        funds = providers.fundamentals.latest_fundamentals(cik, primary_ticker) or {}
    trends = (
        ticker_trends(primary_ticker, providers, cik)
        if (providers and cik and primary_ticker)
        else {"history": {}, "ctx": {}, "strings": {}}
    )
    history = trends.get("history", {})

    data_tables: dict[str, Any] = {}
    if seg:
        data_tables["segments"] = {
            "headers": ["Segment", "Revenue", "Period end"],
            "rows": [[s.get("segment_label") or s.get("segment_member", "—"),
                      fmt_b(s.get("value")), s.get("period_end", "—")] for s in seg[:6]],
        }
    if geo:
        data_tables["geo"] = {
            "headers": ["Region", "Revenue", "Period end"],
            "rows": [[g.get("segment_label") or g.get("segment_member", "—"),
                      fmt_b(g.get("value")), g.get("period_end", "—")] for g in geo[:6]],
        }
    mp = _margin_profile_table(history)
    if mp:
        data_tables["margin_profile"] = mp
    add_sentiment_table_to(data_tables, sentiment)

    last_q_label = trends.get("ctx", {}).get("last_quarter_label", "latest quarter")
    dso, dio, dpo, ccc = fm.dso(funds), fm.dio(funds), fm.dpo(funds), fm.ccc(funds)
    rev_yoy = fm.yoy(history, "revenue")
    gm_delta = fm.margin_delta(history, "gross_margin")
    fcf_ttm = fm.ttm(history, "fcf")
    capex_ttm = fm.ttm(history, "capex")
    buyback_ttm = fm.ttm(history, "buybacks")
    rd = funds.get("rd_expense")
    rev = funds.get("revenue")
    rd_pct = rd / rev * 100 if isinstance(rd, (int, float)) and isinstance(rev, (int, float)) and rev else None
    rd_display = (
        f"{fmt_b(rd)} ({fmt_pct(rd_pct)} of revenue)" if isinstance(rd, (int, float)) else _DASH
    )

    data_tables["metrics_to_validate"] = {
        "headers": ["Metric", "Model Assumption", "Period", "Why It Matters"],
        "rows": [
            ["Revenue growth (YoY)", f"{rev_yoy:+.1f}%" if rev_yoy is not None else _DASH,
             last_q_label, "Primary revenue driver"],
            ["Gross margin trend", f"{gm_delta:+.1f}pts vs PY" if gm_delta is not None else _DASH,
             last_q_label, "Margin trajectory signal"],
            ["Inventory days (DIO)", _days(dio), last_q_label, "Channel inventory health"],
        ],
    }

    ctx = ticker_context(
        ticker=primary_ticker or "",
        providers=providers,
        event_id=ev.id,
        event_subject=ev.subject,
        event_date=edt["event_date"],
        event_time=edt["event_time"],
        cik=cik or "",
    )

    organizer, location = _contact_and_location(ev)

    ctx.update({
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "organizer": organizer,
        "location": location,
        "latest_filing": latest_filing_str(funds),
        # ---- real operating metrics ----
        "dso": _days(dso),
        "dio": _days(dio),
        "dpo": _days(dpo),
        "ccc": _days(ccc),
        "fcf_q": fmt_b(funds.get("fcf")),
        "fcf_ttm": fmt_b(fcf_ttm) if fcf_ttm is not None else _DASH,
        "capex_q": fmt_b(funds.get("capex")),
        "capex_ttm": fmt_b(capex_ttm) if capex_ttm is not None else _DASH,
        "rd_display": rd_display,
        "buyback_q": fmt_b(funds.get("buybacks")),
        "buyback_ttm": fmt_b(buyback_ttm) if buyback_ttm is not None else _DASH,
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
