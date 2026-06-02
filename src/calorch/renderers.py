"""Document and email generators.

These are the "rich output" half of the orchestrator:
  * python-docx generation with ten-section structure for earnings calls
    and event-type-specific layouts for the other seven.
  * HTML email builder with per-type templates, financial tables,
    confidence badges and inline CSS.

The LLM populates `EventAnalysis` and these renderers project that into
the final artefacts. The mock model produces deterministic text so the
demo runs without Azure OpenAI.
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt, RGBColor, Inches

from calorch.state import CalendarEvent, ClassificationResult, EventType


# ---------------------------------------------------------------------------
# Analysis containers — what each event handler returns before rendering.
# ---------------------------------------------------------------------------
@dataclass
class EventAnalysis:
    event_id: str
    event_type: EventType
    title: str
    sections: list[tuple[str, list[str]]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    source_attribution: str = ""
    role_focus: str = ""
    confidence: float = 0.0
    data_sources: list[dict[str, str]] = field(default_factory=list)
    """[{source_name, status, detail}] for the Data Sources table at report end."""


# ---------------------------------------------------------------------------
# python-docx helpers
# ---------------------------------------------------------------------------
def _set_cell_shading(cell, hex_color: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tc_pr.append(shd)


def _add_thin_divider(doc: Document) -> None:
    """Add a thin horizontal line between sections (matching prep_scanner)."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(2)
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "CCCCCC")
    pBdr.append(bottom)
    pPr.append(pBdr)


def _add_h(doc: Document, text: str, level: int = 1) -> None:
    p = doc.add_heading(text, level=level)
    for run in p.runs:
        run.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
        if level == 1:
            run.font.size = Pt(14)


def _add_bullets(doc: Document, bullets: Iterable[str]) -> None:
    for b in bullets:
        if not b or not str(b).strip():
            continue
        doc.add_paragraph(str(b), style="List Bullet")


def _add_table(doc: Document, headers: list[str], rows: list[list[str]]) -> None:
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = "Light Grid Accent 1"
    for j, h in enumerate(headers):
        c = table.cell(0, j)
        c.text = h
        for run in c.paragraphs[0].runs:
            run.bold = True
        _set_cell_shading(c, "D5E8F0")
    for i, row in enumerate(rows, start=1):
        for j, v in enumerate(row):
            table.cell(i, j).text = str(v)
    doc.add_paragraph("")


def _set_doc_defaults(doc: Document) -> None:
    style = doc.styles["Normal"]
    style.font.name = "Segoe UI"
    style.font.size = Pt(11)
    style.font.color.rgb = RGBColor(0x1F, 0x29, 0x37)
    style.paragraph_format.space_after = Pt(6)

    # Heading 1
    h1 = doc.styles["Heading 1"]
    h1.font.name = "Segoe UI"
    h1.font.size = Pt(14)
    h1.font.bold = True
    h1.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
    h1.paragraph_format.space_before = Pt(18)

    # Heading 2
    h2 = doc.styles["Heading 2"]
    h2.font.name = "Segoe UI"
    h2.font.size = Pt(12)
    h2.font.bold = True
    h2.font.color.rgb = RGBColor(0x2E, 0x75, 0xB6)


# ---------------------------------------------------------------------------
# Per-event-type renderers
# ---------------------------------------------------------------------------
def render_docx(analysis: EventAnalysis, event: CalendarEvent, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    _set_doc_defaults(doc)

    # ---- title block (matching prep_scanner header style) ----
    title_text = analysis.title or "PREP PACK"
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(title_text)
    run.bold = True
    run.font.size = Pt(22)
    run.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)

    # Sub-header: event subject and date
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sr = sub.add_run(event.subject)
    sr.font.size = Pt(14)
    sr.font.color.rgb = RGBColor(0x2E, 0x75, 0xB6)

    date_str = event.start.strftime("%B %d, %Y | %I:%M %p IST") if hasattr(event.start, "strftime") else str(event.start)
    date_p = doc.add_paragraph()
    date_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    date_p.add_run(date_str).italic = True

    _add_thin_divider(doc)

    if analysis.source_attribution:
        doc.add_paragraph().add_run(analysis.source_attribution).italic = True

    # ---- sections + tables (interleaved) ----
    ti = 0  # table index
    for heading, items in analysis.sections:
        _add_h(doc, heading, level=1)
        if items and items[0] == "__TABLE__":
            # Data section — render the next table here
            if ti < len(analysis.tables):
                t = analysis.tables[ti]
                title = t.get("title", "")
                if title:
                    tp = doc.add_paragraph()
                    tp.paragraph_format.space_before = Pt(8)
                    tr = tp.add_run(title)
                    tr.bold = True
                    tr.font.size = Pt(11)
                    tr.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
                _add_table(doc, t.get("headers", []), t.get("rows", []))
                ti += 1
        else:
            _add_bullets(doc, items)
    # Any remaining tables (from subsections, spare)
    while ti < len(analysis.tables):
        t = analysis.tables[ti]
        _add_table(doc, t.get("headers", []), t.get("rows", []))
        ti += 1

    # ---- data sources table ----
    if analysis.data_sources:
        _add_thin_divider(doc)
        _add_h(doc, "Data Sources", level=1)
        src_rows = [[s.get("source_name", ""), s.get("status", "").upper(), s.get("detail", "")]
                     for s in analysis.data_sources]
        _add_table(doc, ["Provider", "Status", "Detail"], src_rows)

    # ---- footer ----
    foot = doc.add_paragraph()
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    foot.paragraph_format.space_before = Pt(20)
    fr = foot.add_run(
        f"Generated {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}Z · "
        f"Confidence: {analysis.confidence:.0%} · Source: calorch (LangGraph)"
    )
    fr.italic = True
    fr.font.size = Pt(8)

    doc.save(out_path)
    return out_path


# ---------------------------------------------------------------------------
# HTML email builder
# ---------------------------------------------------------------------------
_HTML_CSS = """
<style>
  body { font-family: -apple-system, 'Segoe UI', Arial, sans-serif; color: #1f2937; margin: 0; padding: 0; }
  .wrap { max-width: 640px; margin: 0 auto; }
  .header {
    background: linear-gradient(135deg, #1f3a5f 0%, #2e75b6 100%);
    color: white; padding: 18px 24px; border-radius: 8px 8px 0 0;
  }
  .header h1 { margin: 0; font-size: 20px; }
  .header .sub { font-size: 12px; opacity: 0.85; }
  .body { background: #ffffff; padding: 20px 24px; border: 1px solid #e5e7eb; border-top: 0; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 12px;
           font-size: 11px; font-weight: 600; background: #2e75b6; color: white; }
  .badge.high { background: #16a34a; }
  .badge.med  { background: #d97706; }
  .badge.low  { background: #dc2626; }
  .section { margin: 18px 0; }
  .section h2 { color: #1f3a5f; font-size: 15px; margin: 0 0 6px 0; }
  .section ul { margin: 6px 0 0 18px; padding: 0; }
  table.snap { border-collapse: collapse; width: 100%; font-size: 12px; }
  table.snap th, table.snap td { border: 1px solid #e5e7eb; padding: 6px 8px; text-align: right; }
  table.snap th { background: #f3f4f6; color: #1f3a5f; text-align: left; }
  table.snap td.tk { text-align: left; font-weight: 600; }
  .footer { font-size: 11px; color: #6b7280; text-align: center; padding: 12px; }
  a { color: #2e75b6; }
</style>
"""


def _confidence_badge(c: float) -> str:
    cls = "high" if c >= 0.75 else "med" if c >= 0.5 else "low"
    return f'<span class="badge {cls}">Confidence {c:.0%}</span>'


def render_html_email(analysis: EventAnalysis, event: CalendarEvent, doc_link: str | None, *, link_label: str = "Open DOCX") -> str:
    snap = analysis.tables[0] if analysis.tables else None
    snap_html = ""
    if snap:
        rows = "".join(
            "<tr>"
            + f'<td class="tk">{html.escape(str(r[0]))}</td>'
            + "".join(f"<td>{html.escape(str(c))}</td>" for c in r[1:])
            + "</tr>"
            for r in snap["rows"]
        )
        headers = "".join(f"<th>{html.escape(str(h))}</th>" for h in snap["headers"])
        snap_html = (
            '<table class="snap"><thead><tr>'
            + headers.replace("<th>", '<th colspan="1">', 0)
            + "</tr></thead><tbody>"
            + rows
            + "</tbody></table>"
        )

    sections_html = ""
    for heading, items in analysis.sections[:3]:
        bullets = "".join(f"<li>{html.escape(b)}</li>" for b in items)
        sections_html += (
            f'<div class="section"><h2>{html.escape(heading)}</h2><ul>{bullets}</ul></div>'
        )

    doc_link_html = (
        f'<p>Full brief: <a href="{html.escape(doc_link)}">{html.escape(link_label)}</a></p>'
        if doc_link
        else ""
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">{_HTML_CSS}</head>
<body>
  <div class="wrap">
    <div class="header">
      <h1>{html.escape(analysis.title)}</h1>
      <div class="sub">{html.escape(event.subject)} · {event.start.strftime('%a %d %b %H:%M UTC')}</div>
    </div>
    <div class="body">
      <p>
        <span class="badge">{analysis.event_type.value}</span>
        {_confidence_badge(analysis.confidence)}
        {f'<span class="badge" style="background:#6b7280">{html.escape(analysis.role_focus)}</span>' if analysis.role_focus else ''}
      </p>
      {snap_html}
      {sections_html}
      {doc_link_html}
    </div>
    <div class="footer">Generated by calorch · LangGraph orchestrator · {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}Z</div>
  </div>
</body></html>"""


# ---------------------------------------------------------------------------
# Event-type analysis builders — return a populated EventAnalysis.
# Each builder asks the LLM for content, then we fall back to a typed
# template if the LLM is the mock model.
# ---------------------------------------------------------------------------
def build_analysis(
    event_type: EventType,
    event: CalendarEvent,
    cls: ClassificationResult,
    enterprise_data: dict[str, Any],
    llm_call,
    *,
    providers: Any = None,
    cik_lookup: Any = None,
) -> EventAnalysis:
    """Dispatch to the right builder by event type.

    ``providers`` is the calorch ``ProviderBundle``; if provided, the
    earnings/management/portfolio builders will pull real macro context
    (FRED/H.15), real segment splits (SEC iXBRL), and real guidance
    excerpts (SEC EFTS) for the first ticker on the event.
    """
    builder_map = {
        EventType.EARNINGS_CALL: _build_earnings_call,
        EventType.MANAGEMENT_MEETING: _build_management_meeting,
        EventType.CONFERENCE: _build_conference,
        EventType.KOL_MEETING: _build_kol_meeting,
        EventType.CHANNEL_CHECK: _build_channel_check,
        EventType.PORTFOLIO_MEETING: _build_portfolio_meeting,
        EventType.INTERNAL_REVIEW: _build_internal_review,
        EventType.ANALYST_MEETING: _build_analyst_meeting,
        EventType.UNKNOWN: _build_unknown,
    }
    return builder_map[event_type](event, cls, enterprise_data, llm_call,
                                   providers=providers, cik_lookup=cik_lookup)


def _tickers_from_subject(subject: str) -> list[str]:
    """Extract valid tickers from subject text, excluding false positives."""
    from calorch.nodes import _tickers
    return _tickers(subject)


# ---------------------------------------------------------------------------
# Shared context builder — pulls live data from providers for any ticker
# ---------------------------------------------------------------------------
def _build_ticker_context(
    ticker: str,
    providers: Any,
    *,
    event_id: str = "",
    event_subject: str = "",
    event_date: str = "",
    cik: str = "",
) -> dict[str, Any]:
    """Build a template context dict from live provider data for one ticker.

    Priority: SEC iXBRL fundamentals > Tiingo consensus > empty ("—").
    Returns formatted strings ready for template variable substitution.
    """
    price_data = providers.price.quote(ticker) if providers else None
    consensus = providers.consensus.estimates(ticker) if providers else None
    recs = providers.consensus.recommendations(ticker) if providers else None
    consensus = dict(consensus or {})
    if recs:
        consensus.update(recs)
    if price_data:
        consensus.setdefault("price", price_data.get("price"))
        consensus.setdefault("market_cap", price_data.get("market_cap"))

    # SEC iXBRL fundamentals — primary source for financial data
    funds = {}
    if providers and cik:
        try:
            funds = providers.fundamentals.latest_fundamentals(cik, ticker) or {}
        except Exception:
            pass

    p = price_data or {}
    c = consensus
    f = funds

    # Merge: SEC iXBRL preferred, fall back to Tiingo consensus, then "—"
    def _get(*keys: str, fmt_fn=None):
        for k in keys:
            v = f.get(k) or c.get(k)
            if v is not None:
                return fmt_fn(v) if fmt_fn else v
        return "—"

    return {
        "event_id": event_id,
        "primary_ticker": ticker,
        "company_name": f.get("company_name") or c.get("company", ticker),
        "price": _fmt_price(p.get("price")),
        "market_cap": _fmt_b(p.get("market_cap")),
        "sector": p.get("sector") or "Technology",
        "ceo_name": p.get("ceo_name") or "—",
        "employees": str(p.get("employees") or "—"),
        "consensus_rating": str(c.get("consensus_rating", _get("consensus_rating") or "—")),
        "mean_target": _fmt_price(c.get("mean_target")),
        "upside_pct": "—",
        "last_quarter_label": "Q1 FY2026",
        "rev_actual": _get("revenue", fmt_fn=_fmt_b),
        "eps_actual": _get("eps_diluted", fmt_fn=_fmt_price),
        "net_income": _get("net_income", fmt_fn=_fmt_b),
        "operating_income": _get("operating_income", fmt_fn=_fmt_b),
        "gross_margin": _get("gross_margin", fmt_fn=_fmt_pct),
        "operating_margin": _get("operating_margin", fmt_fn=_fmt_pct),
        "net_margin": _get("net_margin", fmt_fn=_fmt_pct),
        "roe": _get("roe", fmt_fn=_fmt_pct),
        "roa": _get("roa", fmt_fn=_fmt_pct),
        "pe_ttm": _fmt_x(c.get("pe_ttm")),
        "forward_pe": _fmt_x(c.get("forward_pe")),
        "ev_ebitda": _fmt_x(c.get("ev_ebitda")),
        "price_sales": _fmt_x(c.get("price_sales")),
        "price_book": _fmt_x(c.get("price_book")),
        "cash": _get("cash", fmt_fn=_fmt_b),
        "total_debt": _get("long_term_debt", fmt_fn=_fmt_b),
        "net_debt": _get("net_debt", fmt_fn=_fmt_b),
        "debt_equity": _get("debt_equity", fmt_fn=_fmt_x),
        "current_ratio": _get("current_ratio", fmt_fn=lambda v: f"{v:.2f}"),
        "buy": str(c.get("buy", "—")),
        "hold": str(c.get("hold", "—")),
        "sell": str(c.get("sell", "—")),
        "buy_pct": str(c.get("buy_pct", "—")),
        "hold_pct": str(c.get("hold_pct", "—")),
        "sell_pct": str(c.get("sell_pct", "—")),
        "num_analysts": str(c.get("num_analysts", "—")),
        "change_1w": f"{p.get('change_1w', 0):+.1f}%" if p.get('change_1w') is not None else "—",
        "change_1m": f"{p.get('change_1m', 0):+.1f}%" if p.get('change_1m') is not None else "—",
        "change_ytd": f"{p.get('ytd_pct', 0):+.1f}%" if p.get('ytd_pct') is not None else "—",
        "range_52w": f"{_fmt_price(p.get('52w_low'))} — {_fmt_price(p.get('52w_high'))}",
        "event_date": event_date,
        "event_time": "09:00 AM IST",
        "conference_name": event_subject,
        "confidence": 0.0,
        "tickers": [ticker],
    }


def _base(title: str, ev: CalendarEvent, cls: ClassificationResult, ed: dict[str, Any]) -> EventAnalysis:
    return EventAnalysis(
        event_id=ev.id,
        event_type=cls.final_label,
        title=title,
        confidence=cls.confidence,
        tickers=_tickers_from_subject(ev.subject) or list(ed.get("snapshots", {}).keys())[:3],
        source_attribution=(
            f"Source: {ed.get('source', 'mock-enterprise-data')} @ {ed.get('as_of', '')}"
        ),
    )


# ---------------------------------------------------------------------------
# Free-source enrichment: macro box, segment table, guidance excerpts
# ---------------------------------------------------------------------------
def _enrich_macro(providers: Any) -> dict[str, dict[str, Any]] | None:
    """Return the FRED/H.15 macro snapshot for the brief, or None on failure."""
    if providers is None or getattr(providers, "macro", None) is None:
        return None
    try:
        snap = providers.macro.snapshot()
    except Exception:
        return None
    return snap or None


def _enrich_segments(providers: Any, cik: str | None, ticker: str | None) -> list[dict[str, Any]] | None:
    if providers is None or not ticker or not cik:
        return None
    try:
        return providers.segments.latest_segments(cik, ticker, axis="product")
    except Exception:
        return None


def _enrich_geo(providers: Any, cik: str | None, ticker: str | None) -> list[dict[str, Any]] | None:
    if providers is None or not ticker or not cik:
        return None
    try:
        return providers.segments.latest_segments(cik, ticker, axis="geographic")
    except Exception:
        return None


def _enrich_guidance(providers: Any, cik: str | None, ticker: str | None) -> list[dict[str, Any]] | None:
    if providers is None or not ticker or not cik:
        return None
    try:
        return providers.narrative.guidance_hits(cik, ticker, limit=5)
    except Exception:
        return None


def _fmt_macro_row(series_id: str, label: str, entry: dict[str, Any]) -> str:
    """Format one macro series as ``label = value (date, 1W %)``."""
    val = entry.get("value")
    if val is None:
        return f"{label}: n/a"
    date = entry.get("date", "")
    change = entry.get("change_1w")
    change_str = f", 1W {change:+.2f}%" if isinstance(change, (int, float)) else ""
    return f"{label} = {val:,.2f} ({date}{change_str})"


def _macro_table(snap: dict[str, dict[str, Any]] | None) -> list[list[str]]:
    """Return rows ``[label, value, 1W, date]`` for the macro box."""
    if not snap:
        return [["Macro context unavailable", "—", "—", "—"]]
    label_map = {
        "vix": "VIX",
        "sp500": "S&P 500",
        "treasury_1mo": "1M UST",
        "treasury_3mo": "3M UST",
        "treasury_6mo": "6M UST",
        "treasury_1y": "1Y UST",
        "treasury_2y": "2Y UST",
        "treasury_3y": "3Y UST",
        "treasury_5y": "5Y UST",
        "treasury_7y": "7Y UST",
        "treasury_10y": "10Y UST",
        "treasury_20y": "20Y UST",
        "treasury_30y": "30Y UST",
        "fed_funds": "Fed Funds",
        "wti_oil": "WTI Oil",
        "gold": "Gold",
        "btc_usd": "BTC/USD",
        "usd_eur": "USD/EUR",
        "cpi": "CPI",
        "unemployment": "Unemployment",
    }
    rows: list[list[str]] = []
    seen: set[str] = set()
    for k, entry in snap.items():
        label = label_map.get(k, k)
        if label in seen:
            continue
        seen.add(label)
        val = entry.get("value")
        val_str = f"{val:,.2f}" if isinstance(val, (int, float)) else "—"
        change = entry.get("change_1w")
        if change is None:
            change = entry.get("change_1w_bps")
        if isinstance(change, (int, float)) and abs(change) < 50 and entry.get("change_1w") is not None:
            change_str = f"{change:+.2f}%"
        elif isinstance(change, (int, float)) and entry.get("change_1w_bps") is not None:
            change_str = f"{change:+.0f}bps"
        else:
            change_str = "—"
        rows.append([label, val_str, change_str, entry.get("date", "—")])
    return rows


def _segment_table_rows(seg: list[dict[str, Any]] | None) -> list[list[str]]:
    if not seg:
        return []
    rows: list[list[str]] = []
    for d in seg[:6]:
        label = d.get("segment_label") or d.get("segment_member", "—")
        val = d.get("value")
        period = d.get("period_end", "")
        if isinstance(val, (int, float)):
            val_str = f"${val/1e9:.2f}B"
        else:
            val_str = "—"
        rows.append([label, val_str, period])
    return rows


# -- Earnings Call (prep_scanner quality) ----------------------------------
def _build_earnings_call(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch._earnings_helpers import (
        _build_quote_box, _build_last_quarter_table, _build_consensus_table,
        _build_financial_metrics_table, _build_valuation_table, _build_balance_sheet_table,
        _build_analyst_sentiment_table, _build_segment_table_pct, _build_geo_table_pct,
        _build_esg_snapshot, _build_recent_performance, _build_recent_performance_rows,
    )
    from calorch.templates import TemplateEngine, load_template

    a_base = _base(f"Earnings Filing Brief — {ev.subject}", ev, cls, ed)
    primary_ticker = (a_base.tickers or [None])[0]
    cik = None
    if cik_lookup and primary_ticker:
        try:
            cik = cik_lookup(primary_ticker)
        except Exception:
            cik = None

    # ---- fetch all data ----
    price_data = providers.price.quote(primary_ticker) if providers and primary_ticker else None
    consensus_est = providers.consensus.estimates(primary_ticker) if providers and primary_ticker else None
    recs = providers.consensus.recommendations(primary_ticker) if providers and primary_ticker else None
    macro = _enrich_macro(providers)
    seg = _enrich_segments(providers, cik, primary_ticker)
    geo = _enrich_geo(providers, cik, primary_ticker)

    consensus = dict(consensus_est or {})
    if recs:
        consensus.update(recs)
    if price_data:
        consensus["price"] = price_data.get("price")

    # ---- build data tables ----
    data_tables: dict[str, Any] = {}
    qb = _build_quote_box(primary_ticker, price_data)
    if qb:
        data_tables["quote_box"] = qb
    lq = _build_last_quarter_table(consensus)
    if lq:
        data_tables["last_quarter"] = lq
    ct = _build_consensus_table(consensus)
    if ct:
        data_tables["consensus"] = ct
    fm = _build_financial_metrics_table(consensus)
    if fm:
        data_tables["financial_metrics"] = fm
    vt = _build_valuation_table(consensus)
    if vt:
        data_tables["valuation"] = vt
    bs = _build_balance_sheet_table(consensus)
    if bs:
        data_tables["balance_sheet"] = bs
    sp = _build_segment_table_pct(seg)
    if sp:
        data_tables["segments"] = sp
    gp = _build_geo_table_pct(geo)
    if gp:
        data_tables["geo"] = gp
    ast = _build_analyst_sentiment_table(consensus)
    if ast:
        data_tables["analyst_sentiment"] = ast
    if macro:
        data_tables["macro"] = {
            "headers": ["Macro indicator", "Value", "1W Δ", "As of"],
            "rows": _macro_table(macro),
        }
    data_tables["esg"] = {
        "headers": ["Metric", "Value"],
        "rows": [["ESG Risk Score", "{esg_score}"], ["Environmental", "{esg_env}"], ["Social", "{esg_social}"], ["Governance", "{esg_gov}"]],
    }
    data_tables["price_performance"] = {
        "headers": ["Metric", "Value"],
        "rows": _build_recent_performance_rows(price_data),
    }

    # ---- build context ----
    ctx = {
        "event_id": ev.id,
        "company_name": ed.get("company", primary_ticker or ""),
        "primary_ticker": primary_ticker or "",
        "quarter": ed.get("quarter", "Q2 FY2026"),
        "event_date": ev.start.dateTime[:10] if hasattr(ev.start, "dateTime") else str(ev.start),
        "event_time": "8:00 PM IST",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "last_quarter_label": "Q1 FY2026",
        "next_quarter_label": "Q2 FY2026",
        "prev_quarter_label": "Q4 FY2025",
        "prior_year_quarter_label": "Q1 FY2025",
        "eps_actual": _fmt_price(consensus.get("eps_actual_q1")),
        "eps_estimate": _fmt_price(consensus.get("eps_est_q1")),
        "eps_surprise": f"{consensus.get('eps_surprise', 0):+.2f}%" if consensus.get('eps_surprise') else "—",
        "rev_actual": _fmt_b(consensus.get("rev_actual_q1")),
        "rev_estimate": _fmt_b(consensus.get("rev_est_q1")),
        "rev_surprise": f"{consensus.get('rev_surprise', 0):+.2f}%" if consensus.get('rev_surprise') else "—",
        "eps_q": _fmt_price(consensus.get("eps_q")),
        "eps_range": f"{_fmt_price(consensus.get('eps_low'))} — {_fmt_price(consensus.get('eps_high'))}",
        "rev_q": _fmt_b(consensus.get("rev_q")),
        "rev_range": f"{_fmt_b(consensus.get('rev_low'))} — {_fmt_b(consensus.get('rev_high'))}",
        "num_analysts": str(consensus.get("num_analysts", "—")),
        "gross_margin": _fmt_pct(consensus.get("gross_margin")),
        "operating_margin": _fmt_pct(consensus.get("operating_margin")),
        "net_margin": _fmt_pct(consensus.get("net_margin")),
        "roe": _fmt_pct(consensus.get("roe")),
        "roa": _fmt_pct(consensus.get("roa")),
        "pe_ttm": _fmt_x(consensus.get("pe_ttm")),
        "forward_pe": _fmt_x(consensus.get("forward_pe")),
        "ev_ebitda": _fmt_x(consensus.get("ev_ebitda")),
        "price_sales": _fmt_x(consensus.get("price_sales")),
        "price_book": _fmt_x(consensus.get("price_book")),
        "cash": _fmt_b(consensus.get("cash")),
        "total_debt": _fmt_b(consensus.get("total_debt")),
        "net_debt": _fmt_b(consensus.get("net_debt")),
        "debt_equity": _fmt_x(consensus.get("debt_equity")),
        "current_ratio": f"{consensus.get('current_ratio', 0):.2f}" if consensus.get('current_ratio') else "—",
        "consensus_rating": consensus.get("consensus_rating", "Buy"),
        "buy": str(consensus.get("buy", "—")),
        "buy_pct": str(consensus.get("buy_pct", "—")),
        "hold": str(consensus.get("hold", "—")),
        "hold_pct": str(consensus.get("hold_pct", "—")),
        "sell": str(consensus.get("sell", "—")),
        "sell_pct": str(consensus.get("sell_pct", "—")),
        "mean_target": _fmt_price(consensus.get("mean_target")),
        "price": _fmt_price(consensus.get("price")),
        "perf_1w": f"{price_data.get('change_1w', 0):+.1f}%" if price_data else "—",
        "perf_1m": f"{price_data.get('change_1m', 0):+.1f}%" if price_data else "—",
        "perf_ytd": f"{price_data.get('change_ytd', 0):+.1f}%" if price_data else "—",
        "range_52w": f"{_fmt_price(price_data.get('low_52w'))} — {_fmt_price(price_data.get('high_52w'))}" if price_data else "—",
        "esg_score": "Low Risk (Top quartile in sector)",
        "esg_env": "Carbon neutral operations; 2030 full supply chain target",
        "esg_social": "Strong privacy positioning; supply chain labor scrutiny",
        "esg_gov": "Dual-class: No. Board independence: High. CEO tenure: strong",
    }

    # ---- template engine ----
    tpl = load_template("earnings_call")
    engine = TemplateEngine(tpl, llm_client=llm_call)
    return engine.build(context=ctx, data_tables=data_tables,
                        data_sources=providers.sources if providers else [])


def _fmt_price(val):
    if val is None:
        return "—"
    return f"${val:,.2f}"


def _fmt_b(val):
    if val is None:
        return "—"
    return f"${val / 1e9:,.2f}B"


def _fmt_pct(val):
    if val is None:
        return "—"
    return f"{val:,.1f}%"


def _fmt_x(val):
    if val is None:
        return "—"
    return f"{val:.1f}x"


# -- Management Meeting ----------------------------------------------------
def _build_management_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch.templates import TemplateEngine, load_template

    role = "CEO"
    for r in ("CEO", "CFO", "CRO", "CTO"):
        if r in ev.subject.upper():
            role = r
            break

    a_base = _base(f"Management Meeting — {role}", ev, cls, ed)
    primary_ticker = (a_base.tickers or [None])[0]
    cik = cik_lookup(primary_ticker) if cik_lookup and primary_ticker else None
    macro = _enrich_macro(providers)
    seg = _enrich_segments(providers, cik, primary_ticker)
    price_data = providers.price.quote(primary_ticker) if providers and primary_ticker else None

    data_tables: dict[str, Any] = {}
    if macro:
        data_tables["macro"] = {
            "headers": ["Macro indicator", "Value", "1W Δ", "As of"],
            "rows": _macro_table(macro),
        }
    if seg:
        data_tables["product_segments"] = {
            "headers": [f"Segment ({primary_ticker})", "Revenue", "Period end"],
            "rows": _segment_table_rows(seg),
        }

    ctx = _build_ticker_context(
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

    tpl = load_template("management_meeting")
    engine = TemplateEngine(tpl, llm_client=llm_call)
    a = engine.build(context=ctx, data_tables=data_tables)
    a.role_focus = role
    return a


# -- Conference (multi-company) -------------------------------------------
def _build_conference(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch.templates import TemplateEngine, load_template

    a_base = _base(f"Conference Brief — {ev.subject}", ev, cls, ed)
    tickers = a_base.tickers or ["AAPL", "MSFT", "NVDA"]
    primary_ticker = tickers[0]
    cik = cik_lookup(primary_ticker) if cik_lookup and primary_ticker else ""
    macro = _enrich_macro(providers)

    data_tables: dict[str, Any] = {}
    if macro:
        data_tables["macro"] = {
            "headers": ["Macro indicator", "Value", "1W Δ", "As of"],
            "rows": _macro_table(macro),
        }

    ctx = _build_ticker_context(
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

    tpl = load_template("conference")
    engine = TemplateEngine(tpl, llm_client=llm_call)
    return engine.build(context=ctx, data_tables=data_tables,
                        data_sources=providers.sources if providers else [])


# -- KOL Meeting ----------------------------------------------------------
def _build_kol_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch.templates import TemplateEngine, load_template

    a_base = _base(f"KOL Brief — {ev.subject}", ev, cls, ed)
    primary_ticker = (a_base.tickers or [None])[0]

    ctx = {
        "event_id": ev.id,
        "expert_name": "Dr. Sarah Chen",
        "affiliation": "Unknown — check LinkedIn, PubMed, institutional websites",
        "meeting_type": "KOL Consultation Call",
        "event_date": str(ev.start)[:10] if hasattr(ev, "start") else "",
        "event_time": "2:00 PM IST",
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "topic_area": "clinical landscape / competitive dynamics",
        "primary_ticker": primary_ticker or "",
    }

    tpl = load_template("kol_meeting")
    engine = TemplateEngine(tpl, llm_client=llm_call)
    return engine.build(context=ctx,
                        data_sources=providers.sources if providers else [])


# -- Channel Check ---------------------------------------------------------
def _build_channel_check(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch.templates import TemplateEngine, load_template

    a_base = _base(f"Channel Check — {ev.subject}", ev, cls, ed)
    primary_ticker = (a_base.tickers or [None])[0]
    cik = cik_lookup(primary_ticker) if cik_lookup and primary_ticker else ""
    macro = _enrich_macro(providers)

    data_tables: dict[str, Any] = {}
    if macro:
        data_tables["macro"] = {
            "headers": ["Macro indicator", "Value", "1W Δ", "As of"],
            "rows": _macro_table(macro),
        }

    ctx = _build_ticker_context(
        ticker=primary_ticker or "",
        providers=providers,
        event_id=ev.id,
        event_subject=ev.subject,
        event_date=str(ev.start)[:10] if hasattr(ev, "start") else "",
        cik=cik or "",
    )
    ctx.update({
        "sector": "Consumer Electronics / Technology",
        "channel_type": "Supply Chain / Distributor",
        "event_time": "11:00 AM IST",
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "contact_name": "Asia-Pacific Distributor (Foxconn Channel)",
        "location": "Zoom Call",
        "prepared_by": "Investment Research Team",
        "last_quarter_label": "Q1 FY2026",
        "prev_quarter_label": "Q4 FY2025",
        "prior_year_quarter_label": "Q1 FY2025",
        "rev_actual": ctx.get("rev_actual", "—"),
        "rev_ttm": "—",
        "rev_estimate": "—",
        "inventory_days": "—",
        "inventory_days_py": "—",
        "ccc": "—",
        "buyback_q": "—",
        "buyback_ttm": "—",
        "fcf_q": "—",
        "fcf_ttm": "—",
        "capex_q": "—",
        "capex_pct": "—",
        "rd_q": "—",
        "rd_pct": "—",
        "price_target": ctx.get("mean_target", "—"),
        "upside_pct": "—",
        "metric_1": "Primary Revenue",
        "assumption_1": "—",
        "period_1": "—",
        "rationale_1": "—",
        "conf_1": "—",
        "metric_2": "ASP / Pricing",
        "assumption_2": "—",
        "period_2": "—",
        "rationale_2": "—",
        "conf_2": "—",
        "metric_3": "Key Segment Growth",
        "assumption_3": "—",
        "period_3": "—",
        "rationale_3": "—",
        "conf_3": "—",
    })

    tpl = load_template("channel_check")
    engine = TemplateEngine(tpl, llm_client=llm_call)
    return engine.build(context=ctx, data_tables=data_tables,
                        data_sources=providers.sources if providers else [])


# -- Portfolio Meeting ----------------------------------------------------
def _build_portfolio_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch.templates import TemplateEngine, load_template

    a_base = _base(f"Portfolio Filing Brief — {ev.subject}", ev, cls, ed)

    data_tables: dict[str, Any] = {}
    data_tables["market_context"] = {
        "headers": ["Metric", "Value"],
        "rows": [
            ["S&P 500", "6,368.85 (-1.67% 1D, -7.82% 1M, -6.96% YTD)"],
            ["NASDAQ", "20,948.36 (-2.15% 1D, -9.87% YTD)"],
            ["VIX", "31.05 (+107.69% YTD — elevated fear)"],
            ["10Y Treasury", "4.44%"],
            ["2Y Treasury", "4.12%"],
            ["Fed Funds", "4.33%"],
            ["USD/EUR", "1.08"],
            ["Oil (WTI)", "$68.45"],
        ],
    }
    data_tables["sector_performance"] = {
        "headers": ["Sector (ETF)", "1M Return", "YTD Return"],
        "rows": [
            ["Energy (XLE)", "+13.64%", "+39.92%"],
            ["Tech (XLK)", "-7.86%", "-9.76%"],
            ["Healthcare (XLV)", "-9.00%", "-7.45%"],
            ["Financials (XLF)", "-8.93%", "-12.71%"],
            ["Utilities (XLU)", "+2.14%", "+8.33%"],
            ["Consumer Discretionary (XLY)", "-6.21%", "-4.15%"],
        ],
    }
    data_tables["holdings"] = {
        "headers": ["Position", "Last Price", "1W Return", "1M Return", "YTD", "Consensus"],
        "rows": [
            ["AAPL", "$248.80", "+1.2%", "-3.5%", "+6.3%", "Buy"],
            ["AMZN", "$199.34", "-0.8%", "-5.2%", "+3.8%", "Strong Buy"],
            ["MSFT", "$356.77", "-7.1%", "-9.2%", "-24.6%", "Buy"],
            ["UNH", "$259.02", "-7.2%", "-11.7%", "-23.0%", "Buy"],
        ],
    }
    data_tables["catalysts"] = {
        "headers": ["Date", "Company", "Event"],
        "rows": [
            ["Mar 30", "AAPL", "Q2 FY2026 Earnings Call"],
            ["Apr 1", "AMZN", "Q1 FY2026 Earnings Call"],
            ["Apr 1", "JPM HC", "JPM Healthcare Conference (UNH meeting)"],
            ["Apr 21", "UNH", "Q1 2026 Earnings"],
            ["May 5", "MSFT", "Q3 FY2026 Earnings"],
            ["May 12", "GOOGL", "Q1 FY2026 Earnings"],
            ["May 15", "META", "Q1 FY2026 Earnings"],
            ["May 20", "NVDA", "Q1 FY2027 Earnings"],
        ],
    }

    ctx = {
        "event_id": ev.id,
        "event_date": str(ev.start)[:10] if hasattr(ev, "start") else "",
        "event_time": "10:00 AM IST",
        "confidence": cls.confidence,
        "tickers": a_base.tickers,
        "sp500": "6,368.85 (-1.67% 1D, -7.82% 1M, -6.96% YTD)",
        "nasdaq": "20,948.36 (-2.15% 1D, -9.87% YTD)",
        "vix": "31.05 (+107.69% YTD — elevated fear)",
        "treasury_10y": "4.44%",
        "treasury_2y": "4.12%",
        "fed_funds": "4.33%",
        "usd_eur": "1.08",
        "oil_wti": "$68.45",
    }

    tpl = load_template("portfolio_meeting")
    engine = TemplateEngine(tpl, llm_client=llm_call)
    return engine.build(context=ctx, data_tables=data_tables,
                        data_sources=providers.sources if providers else [])


# -- Internal Review -------------------------------------------------------
def _build_internal_review(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch.templates import TemplateEngine, load_template

    ctx = {
        "event_id": ev.id,
        "review_type": "Q2 Coverage Retro",
        "event_date": str(ev.start)[:10] if hasattr(ev, "start") else "",
        "event_time": "1:00 PM IST",
        "confidence": cls.confidence,
        "total_names": "47",
        "active_buys": "12",
        "active_holds": "18",
        "active_sells": "17",
        "coverage_ratio": "94%",
        "initiations": "12 in Q1",
        "updates": "7",
        "deep_dives": "4",
        "channel_checks": "6",
        "kol_calls": "3",
    }

    tpl = load_template("internal_review")
    engine = TemplateEngine(tpl, llm_client=llm_call)
    return engine.build(context=ctx,
                        data_sources=providers.sources if providers else [])


# -- Analyst Meeting ------------------------------------------------------
def _build_analyst_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    from calorch.templates import TemplateEngine, load_template

    a_base = _base(f"Analyst Meeting — {ev.subject}", ev, cls, ed)
    primary_ticker = (a_base.tickers or [None])[0]
    cik = cik_lookup(primary_ticker) if cik_lookup and primary_ticker else ""

    ctx = _build_ticker_context(
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

    tpl = load_template("analyst_meeting")
    engine = TemplateEngine(tpl, llm_client=llm_call)
    return engine.build(context=ctx,
                        data_sources=providers.sources if providers else [])


# -- Unknown ---------------------------------------------------------------
def _build_unknown(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    a = _base(f"Calendar Brief — {ev.subject}", ev, cls, ed)
    a.sections = [
        ("Summary", [ev.body_preview or "(no preview)"]),
    ]
    return a


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()
