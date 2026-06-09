"""Characterization tests for the per-event-type analysis builders.

These pin the *current* observable output of every agent's analysis
builder (title, section headings, table count, role focus) so that the
ongoing migration of builder bodies out of ``renderers.py`` into their
agent modules is provably a pure relocation, not a behaviour change.

If a builder is intentionally changed later, update the expected snapshot
in the same commit — that's the signal that behaviour moved on purpose.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from calorch.analysis import build_analysis
from calorch.state import CalendarEvent, ClassificationResult, EventType

_ENTERPRISE_DATA = {
    "source": "mock",
    "as_of": "2026-06-09",
    "snapshots": {"AAPL": {"price": 200}},
    "company": "Apple",
}

# event_type -> (subject, expected snapshot)
_CASES = {
    EventType.EARNINGS_CALL: (
        "AAPL Q2 FY2026 Earnings Call",
        {
            "title": "EARNINGS PREP PACK",
            "section_headings": [
                "Executive Snapshot",
                "Last Quarter Performance (Q1 FY2026)",
                "Q2 FY2026 Consensus Estimates",
                "Key Financial Metrics",
                "Valuation Multiples",
                "Balance Sheet Highlights",
                "Analyst Sentiment & Fund Activity",
                "ESG Snapshot",
                "Recent Price Performance",
            ],
            "n_tables": 8,
            "role_focus": "",
        },
    ),
    EventType.MANAGEMENT_MEETING: (
        "1:1 with CFO of AAPL",
        {
            "title": "MANAGEMENT MEETING BRIEFING",
            "section_headings": [
                "Company Overview",
                "Last Quarter (Q1 FY2026)",
                "Recent Developments",
                "Financial Summary",
            ],
            "n_tables": 3,
            "role_focus": "CFO",
        },
    ),
    EventType.CONFERENCE: (
        "Tech Conference AAPL MSFT NVDA",
        {
            "title": "CONFERENCE PREP PACK",
            "section_headings": [
                "Company Overview",
                "Last Quarter (Q1 FY2026)",
                "Recent Developments",
            ],
            "n_tables": 2,
            "role_focus": "",
        },
    ),
    EventType.KOL_MEETING: (
        "KOL call on AAPL supply chain",
        {
            "title": "KOL MEETING PREP",
            "section_headings": [
                "Meeting Context",
                "Pre-Call Research Notes",
                "Clinical Landscape",
                "Competitive Dynamics",
                "Commercial Outlook",
                "Regulatory Environment",
            ],
            "n_tables": 1,
            "role_focus": "",
        },
    ),
    EventType.CHANNEL_CHECK: (
        "Channel check AAPL distributor",
        {
            "title": "AAPL — Channel Check Preparation",
            "section_headings": [
                "Section 2: Key Metrics to Validate",
                "Section 3: Standardized Questionnaire",
            ],
            "n_tables": 1,
            "role_focus": "",
        },
    ),
    EventType.PORTFOLIO_MEETING: (
        "Portfolio review Q2",
        {
            "title": "WEEKLY PORTFOLIO REVIEW",
            "section_headings": [
                "Market Context",
                "Sector Performance",
                "Portfolio Holdings Snapshot",
                "Key Movers This Week",
                "Upcoming Catalysts",
            ],
            "n_tables": 4,
            "role_focus": "",
        },
    ),
    EventType.INTERNAL_REVIEW: (
        "Internal coverage retro",
        {
            "title": "INTERNAL REVIEW",
            "section_headings": [
                "Executive Summary",
                "Coverage Universe",
                "Research Activity",
                "Performance Review",
                "Key Questions",
                "Risk Factors to Monitor",
            ],
            "n_tables": 2,
            "role_focus": "",
        },
    ),
    EventType.ANALYST_MEETING: (
        "Analyst meeting AAPL Morgan Stanley",
        {
            "title": "ANALYST MEETING BRIEFING",
            "section_headings": [
                "Executive Summary",
                "Analyst Profile",
                "Debate Points",
                "Key Questions to Probe",
                "Risk Factors to Monitor",
                "Quoted View",
            ],
            "n_tables": 1,
            "role_focus": "",
        },
    ),
    EventType.UNKNOWN: (
        "Lunch with team",
        {
            "title": "Calendar Brief — Lunch with team",
            "section_headings": ["Summary"],
            "n_tables": 0,
            "role_focus": "",
        },
    ),
}


def _run(event_type: EventType, subject: str):
    ev = CalendarEvent(
        id=f"ev-{event_type.value}",
        subject=subject,
        start=datetime(2026, 6, 10, 10, tzinfo=timezone.utc),
        end=datetime(2026, 6, 10, 11, tzinfo=timezone.utc),
        body_preview="preview text",
    )
    cls = ClassificationResult(event_id=ev.id, final_label=event_type, confidence=0.8)
    return build_analysis(event_type, ev, cls, _ENTERPRISE_DATA, llm_call=None)


@pytest.mark.parametrize("event_type", list(_CASES), ids=lambda e: e.value)
def test_builder_output_unchanged(event_type: EventType):
    subject, expected = _CASES[event_type]
    a = _run(event_type, subject)

    assert a.event_type == event_type
    assert a.title == expected["title"]
    assert [h for h, _ in a.sections] == expected["section_headings"]
    assert len(a.tables) == expected["n_tables"]
    assert a.role_focus == expected["role_focus"]
