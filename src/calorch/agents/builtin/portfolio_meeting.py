"""Portfolio-meeting agent — investment-committee / holdings review preparation."""
from __future__ import annotations

from typing import Any

from calorch.agents.base import AgentSpec, register
from calorch.analysis import EventAnalysis, base_analysis, build_with_template
from calorch.state import EventType


def build_portfolio_meeting(ev, cls, ed, llm_call, *, providers=None, cik_lookup=None) -> EventAnalysis:
    a_base = base_analysis(f"Portfolio Filing Brief — {ev.subject}", ev, cls, ed)

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
