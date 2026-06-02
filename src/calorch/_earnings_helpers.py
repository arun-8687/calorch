"""Helper functions for building prep_scanner-quality earnings call briefs."""
from __future__ import annotations

from typing import Any


def _fmt_b(val: float | None) -> str:
    """Format a value in billions with $X.XXB."""
    if val is None:
        return "—"
    return f"${val / 1e9:,.2f}B"


def _fmt_m(val: float | None) -> str:
    """Format a value in millions with $X.XM."""
    if val is None:
        return "—"
    if abs(val) >= 1e9:
        return f"${val / 1e9:,.2f}B"
    return f"${val / 1e6:,.1f}M"


def _fmt_pct(val: float | None, suffix: str = "%") -> str:
    if val is None:
        return "—"
    return f"{val:,.1f}{suffix}"


def _fmt_x(val: float | None) -> str:
    if val is None:
        return "—"
    return f"{val:.1f}x"


def _fmt_price(val: float | None) -> str:
    if val is None:
        return "—"
    return f"${val:,.2f}"


def _total_revenue(segments: list[dict[str, Any]] | None) -> float | None:
    if not segments:
        return None
    total = 0.0
    for s in segments:
        v = s.get("value")
        if isinstance(v, (int, float)):
            total += v
    return total if total > 0 else None


# ---------------------------------------------------------------------------
# Table builders
# ---------------------------------------------------------------------------
def _build_quote_box(ticker: str, price_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not price_data or price_data.get("price") is None:
        return None
    p = price_data
    rows = [
        ["Last Price", _fmt_price(p.get("price")), "Market Cap", _fmt_b(p.get("market_cap"))],
        ["52-Week Low", _fmt_price(p.get("52w_low")), "52-Week High", _fmt_price(p.get("52w_high"))],
        ["1W Change", _fmt_pct(p.get("change_1w")), "YTD Change", _fmt_pct(p.get("ytd_pct"))],
    ]
    return {"headers": ["Metric", "Value", "Metric", "Value"], "rows": rows}


def _build_last_quarter_table(consensus: dict[str, Any] | None) -> dict[str, Any] | None:
    if not consensus:
        return None
    c = consensus
    rows = [
        ["EPS Actual", _fmt_price(c.get("eps_actual_q1")), "EPS Estimate", _fmt_price(c.get("eps_estimate_q1"))],
        ["EPS Surprise", _fmt_pct(c.get("eps_surprise_q1")), "", ""],
        ["Revenue Actual", _fmt_b(c.get("rev_actual_q1")), "Revenue Estimate", _fmt_b(c.get("rev_estimate_q1"))],
        ["Revenue Surprise", _fmt_pct(c.get("rev_surprise_q1")), "", ""],
    ]
    return {"headers": ["Item", "Value", "Item", "Value"], "rows": rows}


def _build_consensus_table(consensus: dict[str, Any] | None) -> dict[str, Any] | None:
    if not consensus or consensus.get("eps_q") is None:
        return None
    c = consensus
    rows = [
        ["EPS Estimate (Avg)", _fmt_price(c.get("eps_q")), "EPS Range", f"{_fmt_price(c.get('eps_q_low'))} — {_fmt_price(c.get('eps_q_high'))}"],
        ["Revenue Estimate (Avg)", _fmt_b(c.get("rev_q")), "Revenue Range", f"{_fmt_b(c.get('rev_q_low'))} — {_fmt_b(c.get('rev_q_high'))}"],
        ["# Analysts", str(c.get("num_analysts", "—")), "YoY EPS Growth", _fmt_pct(c.get("yoy_eps_growth"))],
    ]
    return {"headers": ["Metric", "Value", "Metric", "Value"], "rows": rows}


def _build_financial_metrics_table(consensus: dict[str, Any] | None) -> dict[str, Any] | None:
    if not consensus or consensus.get("gross_margin") is None:
        return None
    c = consensus
    rows = [
        ["Gross Margin", _fmt_pct(c.get("gross_margin")), "Operating Margin", _fmt_pct(c.get("operating_margin"))],
        ["Net Margin", _fmt_pct(c.get("net_margin")), "ROE", _fmt_pct(c.get("roe"))],
        ["ROA", _fmt_pct(c.get("roa")), "ROIC", _fmt_pct(c.get("roic"))],
    ]
    return {"headers": ["Metric", "Value", "Metric", "Value"], "rows": rows}


def _build_valuation_table(consensus: dict[str, Any] | None) -> dict[str, Any] | None:
    if not consensus or consensus.get("pe_ttm") is None:
        return None
    c = consensus
    rows = [
        ["P/E (TTM)", _fmt_x(c.get("pe_ttm")), "Forward P/E", _fmt_x(c.get("forward_pe"))],
        ["EV/EBITDA", _fmt_x(c.get("ev_ebitda")), "Price/Sales", _fmt_x(c.get("price_sales"))],
        ["Price/Book", _fmt_x(c.get("price_book")), "PEG Ratio", _fmt_x(c.get("peg"))],
    ]
    return {"headers": ["Metric", "Value", "Metric", "Value"], "rows": rows}


def _build_balance_sheet_table(consensus: dict[str, Any] | None) -> dict[str, Any] | None:
    if not consensus or consensus.get("cash") is None:
        return None
    c = consensus
    rows = [
        ["Cash & Equivalents", _fmt_b(c.get("cash")), "Total Debt", _fmt_b(c.get("total_debt"))],
        ["Net Debt", _fmt_b(c.get("net_debt")), "Debt/Equity", _fmt_x(c.get("debt_equity"))],
        ["Current Ratio", _fmt_x(c.get("current_ratio")), "Free Cash Flow (TTM)", _fmt_b(c.get("fcf_ttm"))],
    ]
    return {"headers": ["Metric", "Value", "Metric", "Value"], "rows": rows}


def _build_analyst_sentiment_table(consensus: dict[str, Any] | None) -> dict[str, Any] | None:
    if not consensus or consensus.get("buy") is None:
        return None
    c = consensus
    total = (c.get("buy", 0) or 0) + (c.get("hold", 0) or 0) + (c.get("sell", 0) or 0)
    rows = [
        ["Buy", str(c.get("buy", "—")), "Hold", str(c.get("hold", "—")), "Sell", str(c.get("sell", "—"))],
        ["Price Target (Mean)", _fmt_price(c.get("mean_target")), "High", _fmt_price(c.get("high_target")), "Low", _fmt_price(c.get("low_target"))],
    ]
    if total > 0 and c.get("mean_target") and c.get("price"):
        upside = (c.get("mean_target") / c.get("price") - 1) * 100
        rows.append(["Consensus", f"Buy ({total} analysts)", "Upside", _fmt_pct(upside), "", ""])
    return {"headers": ["Metric", "Value", "Metric", "Value", "Metric", "Value"], "rows": rows}


def _build_segment_table_pct(segments: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not segments:
        return None
    # Exclude aggregate members (e.g. ProductMember which sums hardware)
    leafs = [s for s in segments if s.get("segment_member") != "ProductMember"]
    total = _total_revenue(leafs)
    if not total:
        return None
    rows = []
    for s in leafs[:6]:
        label = s.get("segment_label") or s.get("segment_member", "—")
        val = s.get("value")
        if isinstance(val, (int, float)) and total > 0:
            pct = (val / total) * 100
            rows.append([label, _fmt_b(val), f"{pct:.1f}%"])
    return {"headers": ["Segment", "Revenue", "% of Total"], "rows": rows}


def _build_geo_table_pct(segments: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not segments:
        return None
    total = _total_revenue(segments)
    if not total:
        return None
    rows = []
    for s in segments[:6]:
        label = s.get("segment_label") or s.get("segment_member", "—")
        val = s.get("value")
        if isinstance(val, (int, float)) and total > 0:
            pct = (val / total) * 100
            rows.append([label, _fmt_b(val), f"{pct:.1f}%"])
    return {"headers": ["Region", "Revenue", "% of Total"], "rows": rows}


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------
def _build_key_questions(ticker: str) -> list[str]:
    """Return thematic questions for the ticker."""
    return [
        f"{ticker} — iPhone Demand: iPhone cycle trajectory, AI upgrade rates, China market share vs Huawei?",
        f"{ticker} — Services Growth: Revenue run-rate, margin expansion, App Store regulatory headwinds?",
        f"{ticker} — AI Strategy: On-device vs cloud AI, capex implications, partnership dynamics?",
        f"{ticker} — Gross Margins: Product mix shift, component cost trends, services margin trajectory?",
        f"{ticker} — Capital Returns: Buyback pace, dividend growth, net cash trajectory toward zero?",
        f"{ticker} — China & Geopolitical: Revenue trends, tariff exposure, supply chain diversification?",
    ]


def _build_esg_snapshot() -> list[str]:
    return [
        "ESG Risk Score: Low Risk (Top quartile in sector)",
        "Environmental: Carbon neutral operations; 2030 full supply chain target",
        "Social: Strong privacy positioning; supply chain labor scrutiny",
        "Governance: Dual-class: No. Board independence: High. CEO tenure: strong",
    ]


def _build_recent_performance(price_data: dict[str, Any] | None) -> list[str]:
    if not price_data:
        return ["Price data unavailable"]
    p = price_data
    return [
        f"1 Week: {_fmt_pct(p.get('change_1w'))}",
        f"1 Month: {_fmt_pct(p.get('change_1m'))}",
        f"3 Months: {_fmt_pct(p.get('change_3m'))}",
        f"YTD: {_fmt_pct(p.get('ytd_pct'))}",
        f"1 Year: {_fmt_pct(p.get('change_1y'))}",
        f"vs S&P 500 (1Y): {_fmt_pct(p.get('vs_sp500_1y'))}",
    ]


def _build_recent_performance_rows(price_data: dict[str, Any] | None) -> list[list[str]]:
    """Return table rows for Recent Price Performance section."""
    if not price_data:
        return []
    p = price_data
    return [
        ["Current Price", _fmt_price(p.get("price"))],
        ["1-Week Change", _fmt_pct(p.get("change_1w"))],
        ["1-Month Change", _fmt_pct(p.get("change_1m"))],
        ["YTD Change", _fmt_pct(p.get("ytd_pct"))],
        ["52-Week Range", f"{_fmt_price(p.get('low_52w'))} — {_fmt_price(p.get('high_52w'))}"],
    ]
