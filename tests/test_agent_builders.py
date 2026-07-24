"""Characterization tests for the per-event-type analysis builders.

These pin the *current* observable output of every agent's analysis
builder (title, section headings, table count, role focus) so that the
ongoing migration of builder bodies out of ``renderers.py`` into their
agent modules is provably a pure relocation, not a behaviour change.

If a builder is intentionally changed later, update the expected snapshot
in the same commit — that's the signal that behaviour moved on purpose.
"""
from __future__ import annotations

from datetime import datetime, UTC

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
            # With no providers wired, every SEC-derived table is dash-only
            # and the engine's dash-row suppression omits those sections
            # entirely (including the old fabricated ESG section, now
            # deleted outright); only the LLM-fallback snapshot survives.
            "section_headings": [
                "Executive Snapshot",
            ],
            "n_tables": 0,
            "role_focus": "",
        },
    ),
    EventType.MANAGEMENT_MEETING: (
        "1:1 with CFO of AAPL",
        {
            "title": "MANAGEMENT MEETING BRIEFING",
            # "What Changed This Quarter" self-omits here (no providers ->
            # no trend data to ground it in). "Follow-Up Template" is a
            # fillable note-taking grid that renders unconditionally (it
            # doesn't depend on data), now that it's a "data"-source
            # section instead of the dead "static" one that never rendered.
            "section_headings": [
                "Company Overview",
                "Recent Developments",
                "Follow-Up Template",
            ],
            "n_tables": 1,
            "role_focus": "CFO",
        },
    ),
    EventType.CONFERENCE: (
        "Tech Conference AAPL MSFT NVDA",
        {
            "title": "CONFERENCE PREP PACK",
            # Same self-omission for "What Changed This Quarter"; the
            # "Note-Taking Template" grid now renders unconditionally.
            "section_headings": [
                "Company Overview",
                "Recent Developments",
                "Note-Taking Template",
            ],
            "n_tables": 1,
            "role_focus": "",
        },
    ),
    EventType.KOL_MEETING: (
        "KOL call on AAPL supply chain",
        {
            "title": "KOL MEETING PREP",
            # Expert identity comes from the event payload (organizer /
            # attendees) now, not a fabricated "Dr. Sarah Chen" persona;
            # with no organizer/attendees on the test event it degrades to
            # "—", and the discussion guide is industry-generic.
            # "Hypothesis Tracker" / "Note-Taking Template" are fillable
            # grids that render unconditionally, now that they're
            # "data"-source sections instead of the dead "static" ones
            # that never rendered.
            "section_headings": [
                "Meeting Context",
                "Pre-Call Research Notes",
                "Industry Landscape",
                "Competitive Dynamics",
                "Commercial Outlook",
                "Regulatory & Policy Environment",
                "Hypothesis Tracker",
                "Note-Taking Template",
            ],
            "n_tables": 3,
            "role_focus": "",
        },
    ),
    EventType.CHANNEL_CHECK: (
        "Channel check AAPL distributor",
        {
            "title": "AAPL — Channel Check Preparation",
            # "Section 1" is now real: an LLM revenue-overview bullet
            # section (formerly dead nested-subsection JSON that the engine
            # never rendered). "Channel Finding Tracker" / "Contact Log"
            # are fillable grids that render unconditionally, now that
            # they're "data"-source sections instead of the dead "static"
            # ones that never rendered.
            "section_headings": [
                "Revenue Overview",
                "Section 2: Key Metrics to Validate",
                "Section 3: Standardized Questionnaire",
                "Channel Finding Tracker",
                "Contact Log",
            ],
            "n_tables": 3,
            "role_focus": "",
        },
    ),
    EventType.PORTFOLIO_MEETING: (
        "Portfolio review Q2",
        {
            "title": "WEEKLY PORTFOLIO REVIEW",
            # Rewritten builder: no providers/cik_lookup -> the watchlist
            # loop never runs, so every data-driven section is honestly
            # omitted. "Action Items" is a fillable grid that renders
            # unconditionally (it doesn't depend on the watchlist), now
            # that it's a "data"-source section instead of the dead
            # "static" one that never rendered.
            "section_headings": ["Action Items"],
            "n_tables": 1,
            "role_focus": "",
        },
    ),
    EventType.INTERNAL_REVIEW: (
        "Internal coverage retro",
        {
            "title": "INTERNAL REVIEW",
            # Rewritten builder: real pipeline_activity/watchlist_coverage
            # tables replace the fabricated "47 names / 12 initiations"
            # coverage-universe stats; with no ops repo wired in this call
            # (providers=None) both tables are honestly omitted.
            # "Outstanding Items" is a fillable grid that renders
            # unconditionally, now that it's a "data"-source section
            # instead of the dead "static" one that never rendered.
            "section_headings": [
                "Executive Summary",
                "Performance Review",
                "Key Questions",
                "Risk Factors to Monitor",
                "Outstanding Items",
            ],
            "n_tables": 1,
            "role_focus": "",
        },
    ),
    EventType.ANALYST_MEETING: (
        "Analyst meeting AAPL Morgan Stanley",
        {
            "title": "ANALYST MEETING BRIEFING",
            # analyst_profile/quoted_view (fabricated "Morgan Stanley"
            # persona) are deleted; debate_points is now an LLM section
            # (empty fallback -> omitted with no LLM wired). "Note-Taking
            # Template" is a fillable grid that renders unconditionally,
            # now that it's a "data"-source section instead of the dead
            # "static" one that never rendered.
            "section_headings": [
                "Executive Summary",
                "Key Questions to Probe",
                "Risk Factors to Monitor",
                "Note-Taking Template",
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
        start=datetime(2026, 6, 10, 10, tzinfo=UTC),
        end=datetime(2026, 6, 10, 11, tzinfo=UTC),
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


# ---------------------------------------------------------------------------
# Fully-populated shape — a stub ProviderBundle with a shared canned
# 5-quarter history fixture (+ canned narrative/filings/sentiment/ops) pins
# the *real-data* shape, complementing the degraded (no-provider) snapshots
# above. This is the regression net for the analyst-grade report redesign.
# ---------------------------------------------------------------------------
QUARTERLY_HISTORY = [
    {"label": "Q2 FY2026", "revenue": 100e9, "gross_profit": 46e9, "operating_income": 30e9,
     "net_income": 25e9, "eps_diluted": 1.60, "gross_margin": 46.0, "operating_margin": 30.0,
     "net_margin": 25.0, "ocf": 32e9, "capex": 3e9, "buybacks": 20e9, "dividends_paid": 4e9,
     "fcf": 29e9, "fcf_margin": 29.0, "current_assets": 140e9, "current_liabilities": 120e9,
     "current_ratio": 1.17},
    {"label": "Q1 FY2026", "revenue": 95e9, "gross_profit": 43e9, "operating_income": 27e9,
     "net_income": 22e9, "eps_diluted": 1.40, "gross_margin": 45.3, "operating_margin": 28.4,
     "net_margin": 23.2, "ocf": 30e9, "capex": 2.8e9, "buybacks": 18e9, "dividends_paid": 3.8e9,
     "fcf": 27.2e9, "fcf_margin": 28.6, "current_assets": 135e9, "current_liabilities": 118e9,
     "current_ratio": 1.14},
    {"label": "Q4 FY2025", "revenue": 90e9, "gross_profit": 40e9, "operating_income": 24e9,
     "net_income": 20e9, "eps_diluted": 1.25, "gross_margin": 44.4, "operating_margin": 26.7,
     "net_margin": 22.2, "ocf": 28e9, "capex": 2.5e9, "buybacks": 16e9, "dividends_paid": 3.6e9,
     "fcf": 25.5e9, "fcf_margin": 28.3, "current_assets": 130e9, "current_liabilities": 115e9,
     "current_ratio": 1.13},
    {"label": "Q3 FY2025", "revenue": 88e9, "gross_profit": 39e9, "operating_income": 23e9,
     "net_income": 19e9, "eps_diluted": 1.20, "gross_margin": 44.3, "operating_margin": 26.1,
     "net_margin": 21.6, "ocf": 26e9, "capex": 2.4e9, "buybacks": 15e9, "dividends_paid": 3.5e9,
     "fcf": 23.6e9, "fcf_margin": 26.8, "current_assets": 128e9, "current_liabilities": 112e9,
     "current_ratio": 1.14},
    {"label": "Q2 FY2025", "revenue": 85e9, "gross_profit": 38e9, "operating_income": 22e9,
     "net_income": 18e9, "eps_diluted": 1.10, "gross_margin": 44.7, "operating_margin": 25.9,
     "net_margin": 21.2, "ocf": 25e9, "capex": 2.3e9, "buybacks": 14e9, "dividends_paid": 3.4e9,
     "fcf": 22.7e9, "fcf_margin": 26.7, "current_assets": 125e9, "current_liabilities": 110e9,
     "current_ratio": 1.14},
]


class _FullStubFundamentals:
    def latest_fundamentals(self, cik, ticker):
        d = dict(QUARTERLY_HISTORY[0])
        d.update({
            "company_name": f"{ticker} Inc.",
            "accounts_payable": 55e9,
            "receivables": 40e9,
            "inventory": 8e9,
            "rd_expense": 9e9,
            "cost_of_revenue": 54e9,
            "revenue_form": "10-Q",
            "revenue_period": "2026-03-28",
        })
        return d

    def fundamentals_history(self, cik, ticker, *, quarters=5):
        return {"quarterly": QUARTERLY_HISTORY[:quarters]}


class _FullStubSegments:
    def latest_segments(self, cik, ticker, *, axis="product"):
        if axis == "product":
            return [{"segment_label": "iPhone", "value": 50e9, "period_end": "2026-03-28"},
                    {"segment_label": "Services", "value": 25e9, "period_end": "2026-03-28"}]
        return [{"segment_label": "Americas", "value": 40e9, "period_end": "2026-03-28"},
                {"segment_label": "Europe", "value": 20e9, "period_end": "2026-03-28"}]


class _FullStubFilings:
    def guidance_hits(self, cik, ticker, *, limit=5):
        return [{"file_date": "2026-05-01", "form": "8-K", "snippet": "We expect continued demand."}]


class _FullStubNarrative:
    def guidance_hits(self, cik, ticker, *, limit=5):
        return [{"title": f"{ticker} 8-K press release", "date": "2026-05-01", "type": "8-K",
                  "snippet": "Management expects fiscal 2026 revenue growth in the high single digits."}]


class _FullStubTranscripts:
    def transcript_hits(self, ticker, *, limit=5):
        return []


class _FullStubSentiment:
    def sentiment(self, ticker):
        return {"mean_sentiment": 0.32, "label": "positive", "sample": 4, "source": "sec-lexicon"}


class _FullStubRepo:
    """Stub ``ops`` repository — delivery records for internal_review."""

    def all(self):
        now = datetime.now(UTC).isoformat()
        return [
            {"event_type": "earnings_call", "email_status": "sent", "confidence": 0.9, "updated_at": now},
            {"event_type": "earnings_call", "email_status": "draft", "confidence": 0.7, "updated_at": now},
            {"event_type": "channel_check", "email_status": "sent", "confidence": 0.8, "updated_at": now},
        ]


class _FullStubProviders:
    def __init__(self):
        self.fundamentals = _FullStubFundamentals()
        self.segments = _FullStubSegments()
        self.filings = _FullStubFilings()
        self.narrative = _FullStubNarrative()
        self.transcripts = _FullStubTranscripts()
        self.sentiment = _FullStubSentiment()
        self.sources: list = []
        self.ops = _FullStubRepo()


def _full_cik_lookup(ticker: str) -> str:
    return "0000320193"


def _run_full(event_type: EventType, subject: str, **event_kwargs):
    ev = CalendarEvent(
        id=f"ev-full-{event_type.value}",
        subject=subject,
        start=datetime(2026, 6, 10, 10, tzinfo=UTC),
        end=datetime(2026, 6, 10, 11, tzinfo=UTC),
        body_preview="preview text",
        **event_kwargs,
    )
    cls = ClassificationResult(event_id=ev.id, final_label=event_type, confidence=0.8)
    return build_analysis(
        event_type, ev, cls, _ENTERPRISE_DATA, llm_call=None,
        providers=_FullStubProviders(), cik_lookup=_full_cik_lookup,
    )


@pytest.fixture
def watchlist_env(monkeypatch: pytest.MonkeyPatch):
    """Portfolio/internal-review builders read ``settings.sec_watchlist``
    directly via ``get_settings()`` — point it at a small deterministic
    watchlist for the duration of the test.
    """
    from calorch.config import get_settings

    monkeypatch.setenv("SEC_WATCHLIST", "AAPL,MSFT")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_earnings_call_full_shape():
    a = _run_full(EventType.EARNINGS_CALL, "AAPL Q2 FY2026 Earnings Call")
    assert [h for h, _ in a.sections] == [
        "Executive Snapshot",
        "Last Quarter Performance (Q2 FY2026)",
        "5-Quarter Trend",
        "Cash Flow & Capital Returns",
        # New analyst-synthesis section: grounded in the real 5-quarter
        # trend data from the stub, so NoOpEnricher's data-driven fallback
        # (no LLM wired in this test) produces non-empty bullets.
        "What Changed This Quarter",
        "Balance Sheet & Liquidity",
        "Revenue Segmentation",
        "By Geography",
        "Guidance & Management Commentary",
        "Recent Filings Mentioning Guidance / Outlook",
        "Sentiment",
    ]
    assert len(a.tables) == 9


def test_management_meeting_full_shape():
    a = _run_full(EventType.MANAGEMENT_MEETING, "1:1 with CFO of AAPL")
    assert [h for h, _ in a.sections] == [
        "Company Overview",
        "Last Quarter (Q2 FY2026)",
        "5-Quarter Trend",
        "What Changed This Quarter",
        "Recent Developments",
        "Guidance & Management Commentary",
        "Sentiment",
        "Follow-Up Template",
        "Financial Summary",
    ]
    assert len(a.tables) == 7
    assert a.role_focus == "CFO"


def test_channel_check_full_shape():
    a = _run_full(
        EventType.CHANNEL_CHECK, "Channel check AAPL distributor",
        organizer="Distributor Contact", location="Zoom Call",
    )
    assert [h for h, _ in a.sections] == [
        "Revenue Overview",
        "Revenue by Segment (Q2 FY2026)",
        "Revenue by Geography (Q2 FY2026)",
        "Margin Profile",
        "Key Operating Metrics",
        "Section 2: Key Metrics to Validate",
        "Sentiment",
        "Section 3: Standardized Questionnaire",
        "Channel Finding Tracker",
        "Contact Log",
    ]
    assert len(a.tables) == 9


def test_analyst_meeting_full_shape():
    a = _run_full(
        EventType.ANALYST_MEETING, "Analyst meeting AAPL Morgan Stanley",
        organizer="John Smith", attendees=["john.smith@wellsfargo.com"],
    )
    assert [h for h, _ in a.sections] == [
        "Executive Summary",
        "Fundamentals Snapshot (Q2 FY2026)",
        "5-Quarter Trend",
        "Key Questions to Probe",
        "Risk Factors to Monitor",
        "Sentiment",
        "Note-Taking Template",
    ]
    assert len(a.tables) == 5
    # No fabricated "Morgan Stanley" analyst-firm persona — the firm is
    # derived from the attendee's email domain.
    assert a.tables[0]["rows"][:2] == [["Counterpart", "John Smith"], ["Firm", "Wellsfargo"]]


def test_kol_meeting_full_shape():
    a = _run_full(
        EventType.KOL_MEETING, "KOL call on AAPL supply chain",
        organizer="Dr. Jane Doe", attendees=["jane.doe@acmeresearch.com"],
    )
    assert [h for h, _ in a.sections] == [
        "Meeting Context",
        "Company Context (AAPL)",
        "Pre-Call Research Notes",
        "Industry Landscape",
        "Competitive Dynamics",
        "Commercial Outlook",
        "Regulatory & Policy Environment",
        "Hypothesis Tracker",
        "Note-Taking Template",
    ]
    assert len(a.tables) == 4
    assert a.tables[0]["rows"][0] == ["Expert", "Dr. Jane Doe"]


def test_portfolio_meeting_full_shape(watchlist_env):
    a = _run_full(EventType.PORTFOLIO_MEETING, "Portfolio review Q2")
    assert [h for h, _ in a.sections] == [
        "Watchlist Snapshot",
        "Sentiment Overview",
        "Upcoming Catalysts",
        "Action Items",
    ]
    assert len(a.tables) == 4


def test_internal_review_full_shape(watchlist_env):
    a = _run_full(EventType.INTERNAL_REVIEW, "Internal coverage retro")
    assert [h for h, _ in a.sections] == [
        "Executive Summary",
        "Pipeline Activity (Last 90 Days)",
        "Watchlist Coverage",
        "Performance Review",
        "Key Questions",
        "Risk Factors to Monitor",
        "Outstanding Items",
    ]
    assert len(a.tables) == 3


# ---------------------------------------------------------------------------
# Part B5 — narrative_docs_table snippet_source provenance footnote.
# ---------------------------------------------------------------------------
def test_narrative_docs_table_snippet_source_provenance():
    from calorch.analysis import narrative_docs_table

    base_hit = {"date": "2026-05-01", "type": "8-K", "snippet": "Guidance raised."}
    llm_hits = [{**base_hit, "snippet_source": "llm"}]
    heuristic_hits = [{**base_hit, "snippet_source": "heuristic"}]
    mixed_hits = [{**base_hit, "snippet_source": "llm"}, {**base_hit, "snippet_source": "heuristic"}]
    untagged_hits = [dict(base_hit)]

    assert narrative_docs_table(llm_hits)["source_note"] == (
        "Source: SEC 8-K press release / MD&A (excerpts LLM-selected)"
    )
    assert narrative_docs_table(heuristic_hits)["source_note"] == (
        "Source: SEC 8-K press release / MD&A (keyword-selected)"
    )
    assert narrative_docs_table(mixed_hits)["source_note"] == (
        "Source: SEC 8-K press release / MD&A (mixed)"
    )
    # No snippet_source tag on any hit (e.g. old/ad-hoc data) -> no suffix.
    assert narrative_docs_table(untagged_hits)["source_note"] == "Source: SEC 8-K press release / MD&A"


# ---------------------------------------------------------------------------
# Part B6 — ticker_trends()/ticker_context() no longer double-fetch
# fundamentals_history for builders that need both.
# ---------------------------------------------------------------------------
class _CountingFundamentals(_FullStubFundamentals):
    def __init__(self):
        self.history_calls = 0

    def fundamentals_history(self, cik, ticker, *, quarters=5):
        self.history_calls += 1
        return super().fundamentals_history(cik, ticker, quarters=quarters)


@pytest.mark.parametrize("event_type,subject", [
    (EventType.EARNINGS_CALL, "AAPL Q2 FY2026 Earnings Call"),
    (EventType.MANAGEMENT_MEETING, "1:1 with CFO of AAPL"),
    (EventType.CONFERENCE, "Tech Conference AAPL"),
    (EventType.ANALYST_MEETING, "Analyst meeting AAPL Morgan Stanley"),
    (EventType.CHANNEL_CHECK, "Channel check AAPL distributor"),
], ids=lambda v: v.value if isinstance(v, EventType) else v)
def test_builders_fetch_fundamentals_history_once(event_type: EventType, subject: str):
    providers = _FullStubProviders()
    counting = _CountingFundamentals()
    providers.fundamentals = counting

    ev = CalendarEvent(
        id=f"ev-count-{event_type.value}",
        subject=subject,
        start=datetime(2026, 6, 10, 10, tzinfo=UTC),
        end=datetime(2026, 6, 10, 11, tzinfo=UTC),
        body_preview="preview text",
    )
    cls = ClassificationResult(event_id=ev.id, final_label=event_type, confidence=0.8)
    build_analysis(
        event_type, ev, cls, _ENTERPRISE_DATA, llm_call=None,
        providers=providers, cik_lookup=_full_cik_lookup,
    )
    assert counting.history_calls == 1
