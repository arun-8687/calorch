"""Tests for the DOCX and HTML renderers."""
from datetime import datetime, UTC
from pathlib import Path

import pytest

from calorch.analysis import EventAnalysis, build_analysis
from calorch.renderers import render_docx, render_html_email
from calorch.state import CalendarEvent, ClassificationResult, EventType


@pytest.fixture
def sample_event() -> CalendarEvent:
    return CalendarEvent(
        id="ev-test-001",
        subject="AAPL Q1 FY26 Earnings Call",
        body_preview="Apple Q1 results discussion",
        start=datetime(2026, 3, 3, 21, 0, tzinfo=UTC),
        end=datetime(2026, 3, 3, 22, 0, tzinfo=UTC),
        organizer="ir@apple.com",
        attendees=["me@firm.example"],
        location="Webcast",
        is_online=True,
        web_link="",
    )


@pytest.fixture
def sample_classification() -> ClassificationResult:
    return ClassificationResult(
        event_id="ev-test-001",
        pass1_label=EventType.EARNINGS_CALL,
        pass1_keyword_hits=3,
        final_label=EventType.EARNINGS_CALL,
        confidence=0.92,
        rationale="earnings/q1/guidance hits",
        routed_node="handle_earnings_call",
    )


def test_earnings_call_docx_renders(tmp_path: Path, sample_event, sample_classification):
    analysis = EventAnalysis(
        event_id=sample_event.id,
        event_type=EventType.EARNINGS_CALL,
        title="Earnings Call Brief",
        sections=[
            ("Headline", ["In-line with consensus."]),
            ("Q&A Highlights", ["Buyback pace reaffirmed."]),
        ],
        tickers=["AAPL"],
        confidence=0.92,
    )
    out = tmp_path / "test.docx"
    render_docx(analysis, sample_event, out)
    assert out.exists()
    assert out.stat().st_size > 5_000  # real DOCX, not empty


class _StubFundamentals:
    def latest_fundamentals(self, cik, ticker):
        return {
            "company_name": "Apple Inc.",
            "revenue": 90_000_000_000,
            "net_income": 20_000_000_000,
            "eps_diluted": 2.10,
            "gross_margin": 45.2,
            "operating_margin": 30.1,
            "net_margin": 22.5,
            "roe": 28.0,
            "roa": 12.0,
            "cash": 30_000_000_000,
            "long_term_debt": 95_000_000_000,
            "net_debt": 65_000_000_000,
            "debt_equity": 1.2,
            "current_ratio": 1.05,
        }

    def fundamentals_history(self, cik, ticker, *, quarters=5):
        return {"quarterly": []}


class _StubFilings:
    def guidance_hits(self, cik, ticker, limit=5):
        return []


class _StubTranscripts:
    def transcript_hits(self, ticker, limit=5):
        return []


class _StubSegments:
    def latest_segments(self, cik, ticker, axis="product"):
        if axis == "product":
            return [
                {"segment_label": "iPhone", "value": 45_000_000_000, "period_end": "2026-03-31"},
                {"segment_label": "Services", "value": 20_000_000_000, "period_end": "2026-03-31"},
            ]
        return [
            {"segment_label": "Americas", "value": 40_000_000_000, "period_end": "2026-03-31"},
            {"segment_label": "Europe", "value": 25_000_000_000, "period_end": "2026-03-31"},
        ]


class _StubSentiment:
    def sentiment(self, ticker):
        return None


class _StubNarrative:
    def guidance_hits(self, cik, ticker, limit=5):
        return []


class _StubProviders:
    def __init__(self):
        self.fundamentals = _StubFundamentals()
        self.segments = _StubSegments()
        self.sentiment = _StubSentiment()
        self.narrative = _StubNarrative()
        self.filings = _StubFilings()
        self.transcripts = _StubTranscripts()
        self.sources = []


def test_earnings_call_uses_ten_sections(tmp_path: Path, sample_event, sample_classification):
    # Dash-row suppression (templates.py) now drops any table whose rows are
    # all "—" — so real fundamentals/segments have to be supplied for
    # tables to render. A stub provider bundle stands in for SEC iXBRL data.
    analysis = build_analysis(
        EventType.EARNINGS_CALL,
        sample_event,
        sample_classification,
        enterprise_data={
            "source": "mock",
            "as_of": "now",
            "snapshots": {"AAPL": {"price": 200, "consensus_eps_q": 2.0, "consensus_rev_q": 1e10, "fy1_pe": 30, "ytd_return": 5}},
            "guidance": "Mgmt guides FY revenue +6-8% YoY",
            "transcript_excerpt": "We remain on track to deliver.",
        },
        llm_call=None,
        providers=_StubProviders(),
        cik_lookup=lambda ticker: "0000320193",
    )
    out = tmp_path / "ec.docx"
    render_docx(analysis, sample_event, out)
    from docx import Document
    doc = Document(out)
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    tables = doc.tables
    # Template-driven output: LLM sections become headings, data sections become tables
    assert len(headings) >= 1, f"expected >=1 heading, got {len(headings)}"
    assert len(tables) >= 5, f"expected >=5 data tables, got {len(tables)}"
    # No hardcoded "1." numbering remains
    assert not any(h.startswith("1.") for h in headings), "old numbered sections should not appear"
    # Dash-only rows/tables (e.g. consensus, valuation — all unresolved
    # market data) are suppressed entirely, not rendered as "—" rows.
    table_texts = [c.text for t in tables for row in t.rows for c in row.cells]
    assert not any(t.strip() in ("—", "-") for t in table_texts)


def test_html_email_contains_confidence_badge(sample_event, sample_classification):
    analysis = EventAnalysis(
        event_id=sample_event.id,
        event_type=EventType.EARNINGS_CALL,
        title="Brief",
        sections=[("Headline", ["A"]), ("Guidance", ["B"]), ("Q&A", ["C"])],
        tables=[{"headers": ["Ticker", "Price"], "rows": [["AAPL", 200]]}],
        confidence=0.92,
    )
    html = render_html_email(analysis, sample_event, doc_link=None)
    assert "earnings_call" in html
    assert "92%" in html
    assert "<table" in html


def test_management_meeting_infers_role(sample_event):
    sample_event.subject = "1:1 with CFO about capital allocation"
    cls = ClassificationResult(
        event_id=sample_event.id,
        final_label=EventType.MANAGEMENT_MEETING,
        confidence=0.7,
        routed_node="handle_management_meeting",
    )
    analysis = build_analysis(
        EventType.MANAGEMENT_MEETING,
        sample_event,
        cls,
        enterprise_data={"source": "mock", "as_of": "now", "snapshots": {}},
        llm_call=None,
    )
    assert analysis.role_focus == "CFO"


def test_html_email_suppresses_unsafe_doc_link_scheme(sample_event, sample_classification):
    """SEC-6: javascript:/data: doc links are not emitted as href."""
    from calorch.analysis import EventAnalysis

    analysis = EventAnalysis(
        event_id=sample_event.id, event_type=EventType.EARNINGS_CALL,
        title="Brief", sections=[("H", ["a"])], confidence=0.9,
    )
    out = render_html_email(analysis, sample_event, doc_link="javascript:alert(1)")
    assert 'href="javascript:' not in out
    safe = render_html_email(analysis, sample_event, doc_link="https://example.com/x.docx")
    assert 'href="https://example.com/x.docx"' in safe


# ---------------------------------------------------------------------------
# source_note provenance footnotes
# ---------------------------------------------------------------------------
_NOTE = "Source: SEC 10-Q filed 2026-01-28, period ended 2025-12-27"


def test_docx_source_note_rendered_italic_gray(tmp_path: Path, sample_event):
    analysis = EventAnalysis(
        event_id=sample_event.id,
        event_type=EventType.EARNINGS_CALL,
        title="Brief",
        sections=[("Financials", ["__TABLE__"])],
        tables=[{
            "title": "",
            "headers": ["Metric", "Value"],
            "rows": [["Revenue", "$1.00B"]],
            "source_note": _NOTE,
        }],
        confidence=0.9,
    )
    out = tmp_path / "note.docx"
    render_docx(analysis, sample_event, out)
    from docx import Document
    from docx.shared import RGBColor

    doc = Document(out)
    note_paragraphs = [p for p in doc.paragraphs if p.text == _NOTE]
    assert len(note_paragraphs) == 1
    run = note_paragraphs[0].runs[0]
    assert run.italic is True
    assert run.font.size.pt == 8
    assert run.font.color.rgb == RGBColor(0x6B, 0x72, 0x80)


def test_docx_no_source_note_key_no_footnote_paragraph(tmp_path: Path, sample_event):
    analysis = EventAnalysis(
        event_id=sample_event.id,
        event_type=EventType.EARNINGS_CALL,
        title="Brief",
        sections=[("Financials", ["__TABLE__"])],
        tables=[{"title": "", "headers": ["Metric", "Value"], "rows": [["Revenue", "$1.00B"]]}],
        confidence=0.9,
    )
    out = tmp_path / "nonote.docx"
    render_docx(analysis, sample_event, out)
    from docx import Document

    doc = Document(out)
    assert not any(_NOTE in p.text for p in doc.paragraphs)


def test_html_source_note_rendered(sample_event):
    analysis = EventAnalysis(
        event_id=sample_event.id,
        event_type=EventType.EARNINGS_CALL,
        title="Brief",
        sections=[("Financials", ["__TABLE__"])],
        tables=[{
            "title": "Key Metrics",
            "headers": ["Metric", "Value"],
            "rows": [["Revenue", "$1.00B"]],
            "source_note": _NOTE,
        }],
        confidence=0.9,
    )
    out = render_html_email(analysis, sample_event, doc_link=None)
    assert _NOTE in out
    assert 'class="srcnote"' in out
    assert f'<div class="srcnote">{_NOTE}</div>' in out


# ---------------------------------------------------------------------------
# Full HTML email digest
# ---------------------------------------------------------------------------
def test_html_digest_walks_all_sections_with_tables_interleaved_in_order(sample_event):
    analysis = EventAnalysis(
        event_id=sample_event.id,
        event_type=EventType.EARNINGS_CALL,
        title="Brief",
        sections=[
            ("Headline", ["Bullet A"]),
            ("Financials", ["__TABLE__"]),
            ("Segments", ["__TABLE__"]),
            ("Key Themes", ["Bullet B"]),
        ],
        # Three tables for two __TABLE__ sections: the third is a spare/
        # leftover table that (mirroring the DOCX renderer's trailing
        # while-loop) renders after the section walk, unpaired.
        tables=[
            {"headers": ["M"], "rows": [["TBL0-MARK"]]},
            {"headers": ["M"], "rows": [["TBL1-MARK"]]},
            {"headers": ["M"], "rows": [["TBL2-MARK"]]},
        ],
        confidence=0.9,
    )
    out = render_html_email(analysis, sample_event, doc_link=None)

    for marker in ["Headline", "Financials", "TBL0-MARK", "Segments", "TBL1-MARK", "Key Themes", "TBL2-MARK"]:
        assert marker in out, f"missing {marker}"

    positions = {m: out.index(m) for m in ["Headline", "Financials", "TBL0-MARK", "Segments", "TBL1-MARK", "Key Themes", "TBL2-MARK"]}
    ordered = sorted(positions, key=positions.get)
    assert ordered == ["Headline", "Financials", "TBL0-MARK", "Segments", "TBL1-MARK", "Key Themes", "TBL2-MARK"]
    assert "Full detail in the attached DOCX." not in out


def test_html_digest_bullet_and_table_caps_single_full_detail_line(sample_event):
    many_bullets = [f"Bullet {i}" for i in range(12)]
    table_sections = [(f"Table Section {i}", ["__TABLE__"]) for i in range(12)]
    tables = [{"headers": ["M"], "rows": [[f"row{i}"]]} for i in range(12)]

    analysis = EventAnalysis(
        event_id=sample_event.id,
        event_type=EventType.EARNINGS_CALL,
        title="Brief",
        sections=[("Long List", many_bullets)] + table_sections,
        tables=tables,
        confidence=0.9,
    )
    out = render_html_email(analysis, sample_event, doc_link=None)

    # Bullet cap: 8 real bullets + a truncation marker, not all 12.
    assert "Bullet 7" in out
    assert "Bullet 8" not in out
    assert "<li>…</li>" in out

    # Table cap: only the first 10 tables render.
    assert "row9" in out
    assert "row10" not in out

    # Both caps fired, but the notice appears exactly once.
    assert out.count("Full detail in the attached DOCX.") == 1
