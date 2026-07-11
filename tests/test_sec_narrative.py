"""Tests for the free SEC-derived narrative source (`calorch.sec_narrative`).

No network, no real `edgar` import: `SecNarrativeClient` is driven entirely
through its injectable `company_factory` kwarg with lightweight fakes that
mimic just the surface `edgartools` exposes (`.name`, `.get_filings(form=)`
-> `.latest(1)` -> object with `.filing_date` / `.accession_no` /
`.attachments` (each `.document_type` / `.text()`) and `.obj()` (dict-like,
subscriptable by SEC item label).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from calorch.alphasense import _excerpt
from calorch.sec_narrative import SecNarrativeClient, _guidance_snippet


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeAttachment:
    def __init__(self, document_type: str, text: str) -> None:
        self.document_type = document_type
        self._text = text

    def text(self) -> str:
        return self._text


class FakeFiling:
    def __init__(
        self,
        *,
        filing_date: str = "2026-04-30",
        accession_no: str = "0000320193-26-000011",
        attachments: list[FakeAttachment] | None = None,
        obj_map: dict[str, str] | None = None,
    ) -> None:
        self.filing_date = filing_date
        self.accession_no = accession_no
        self.attachments = attachments or []
        self._obj_map = obj_map or {}

    def obj(self) -> dict[str, str]:
        return self._obj_map


class FakeFilingsResult:
    def __init__(self, filing: FakeFiling | None) -> None:
        self._filing = filing

    def latest(self, n: int = 1) -> FakeFiling | None:
        return self._filing


class FakeCompany:
    def __init__(self, *, name: str = "Apple Inc.", filings_by_form: dict[str, FakeFilingsResult] | None = None) -> None:
        self.name = name
        self._filings_by_form = filings_by_form or {}
        self.get_filings_calls: list[str] = []

    def get_filings(self, form: str) -> FakeFilingsResult:
        self.get_filings_calls.append(form)
        return self._filings_by_form.get(form, FakeFilingsResult(None))


_PRESS_TEXT = (
    "Apple today announced financial results. Revenue was $100 billion, up 10% year over year. "
    "We expect continued strong growth in the coming fiscal 2026 quarters. "
    "The Board of Directors declared a dividend."
)
_MDA_TEXT = (
    "This section discusses our results of operations. Overall demand remained solid across regions. "
    "Management anticipates margins will improve as supply constraints ease. "
    "We are not aware of any material pending litigation."
)


def _company_with_8k_and_10q() -> FakeCompany:
    press = FakeFiling(
        filing_date="2026-04-30",
        accession_no="0000320193-26-000011",
        attachments=[
            FakeAttachment("8-K", "<html>cover page</html>"),
            FakeAttachment("EX-99.1", _PRESS_TEXT),
            FakeAttachment("EX-101.SCH", "<xsd/>"),
        ],
    )
    mda = FakeFiling(
        filing_date="2026-05-01",
        accession_no="0000320193-26-000013",
        obj_map={"Item 2": _MDA_TEXT},
    )
    return FakeCompany(filings_by_form={
        "8-K": FakeFilingsResult(press),
        "10-Q": FakeFilingsResult(mda),
    })


# ---------------------------------------------------------------------------
# full_texts / narrative_docs — happy path
# ---------------------------------------------------------------------------
def test_full_texts_returns_press_release_and_mda() -> None:
    company = _company_with_8k_and_10q()
    client = SecNarrativeClient("test agent test@example.com", company_factory=lambda _t: company)

    texts = client.full_texts("AAPL")

    assert len(texts) == 2
    assert {t["type"] for t in texts} == {"8-K", "10-Q"}
    press = next(t for t in texts if t["type"] == "8-K")
    assert press["text"] == _PRESS_TEXT
    assert press["date"] == "2026-04-30"
    mda = next(t for t in texts if t["type"] == "10-Q")
    assert mda["text"] == _MDA_TEXT


def test_narrative_docs_hit_shape_matches_excerpt_keys_plus_snippet() -> None:
    company = _company_with_8k_and_10q()
    client = SecNarrativeClient("test agent test@example.com", company_factory=lambda _t: company)

    hits = client.narrative_docs("AAPL")

    assert len(hits) == 2
    excerpt_keys = set(_excerpt({}).keys())
    for hit in hits:
        assert excerpt_keys <= set(hit.keys())
        assert "snippet" in hit
        assert hit["source"] == "sec-filings"
        assert hit["sentiment"] is None
        assert hit["ticker"] == "AAPL"
        assert hit["company"] == "Apple Inc."
        assert hit["doc_id"]
        assert hit["snippet"]


def test_narrative_docs_respects_limit() -> None:
    company = _company_with_8k_and_10q()
    client = SecNarrativeClient("test agent test@example.com", company_factory=lambda _t: company)

    assert len(client.narrative_docs("AAPL", limit=1)) == 1
    assert len(client.full_texts("AAPL", limit=1)) == 1


# ---------------------------------------------------------------------------
# 8-K exhibit selection
# ---------------------------------------------------------------------------
def test_press_release_only_considers_ex99_attachments() -> None:
    press = FakeFiling(attachments=[
        FakeAttachment("8-K", "cover"),
        FakeAttachment("EX-101.SCH", "xsd"),
    ])
    company = FakeCompany(filings_by_form={"8-K": FakeFilingsResult(press), "10-Q": FakeFilingsResult(None)})
    client = SecNarrativeClient("test agent test@example.com", company_factory=lambda _t: company)

    texts = client.full_texts("AAPL")
    assert not any(t["type"] == "8-K" for t in texts)


# ---------------------------------------------------------------------------
# 10-K fallback when no 10-Q
# ---------------------------------------------------------------------------
def test_10k_fallback_when_no_10q_text() -> None:
    tenk = FakeFiling(
        filing_date="2025-10-31",
        accession_no="0000320193-25-000079",
        obj_map={"Item 7": _MDA_TEXT},
    )
    company = FakeCompany(filings_by_form={
        "8-K": FakeFilingsResult(None),
        "10-Q": FakeFilingsResult(None),
        "10-K": FakeFilingsResult(tenk),
    })
    client = SecNarrativeClient("test agent test@example.com", company_factory=lambda _t: company)

    texts = client.full_texts("AAPL")
    assert len(texts) == 1
    assert texts[0]["type"] == "10-K"
    assert texts[0]["text"] == _MDA_TEXT
    assert texts[0]["date"] == "2025-10-31"


def test_10k_fallback_also_used_when_10q_item2_empty() -> None:
    tenq = FakeFiling(obj_map={"Item 2": "   "})  # present filing, blank MD&A text
    tenk = FakeFiling(accession_no="0000320193-25-000079", obj_map={"Item 7": _MDA_TEXT})
    company = FakeCompany(filings_by_form={
        "8-K": FakeFilingsResult(None),
        "10-Q": FakeFilingsResult(tenq),
        "10-K": FakeFilingsResult(tenk),
    })
    client = SecNarrativeClient("test agent test@example.com", company_factory=lambda _t: company)

    texts = client.full_texts("AAPL")
    assert len(texts) == 1
    assert texts[0]["type"] == "10-K"


# ---------------------------------------------------------------------------
# Degraded / empty shapes
# ---------------------------------------------------------------------------
def test_degrades_to_empty_on_factory_exception() -> None:
    def _boom(_t: Any) -> Any:
        raise RuntimeError("no such company")

    client = SecNarrativeClient("test agent test@example.com", company_factory=_boom)
    assert client.full_texts("NOPE") == []
    assert client.narrative_docs("NOPE") == []


def test_degrades_to_empty_when_no_filings_at_all() -> None:
    company = FakeCompany(filings_by_form={})
    client = SecNarrativeClient("test agent test@example.com", company_factory=lambda _t: company)
    assert client.full_texts("AAPL") == []
    assert client.narrative_docs("AAPL") == []


def test_press_release_fetch_exception_does_not_block_mda() -> None:
    class ExplodingFilingsResult:
        def latest(self, n: int = 1) -> Any:
            raise RuntimeError("boom")

    mda = FakeFiling(obj_map={"Item 2": _MDA_TEXT})
    company = FakeCompany(filings_by_form={
        "8-K": ExplodingFilingsResult(),
        "10-Q": FakeFilingsResult(mda),
    })
    client = SecNarrativeClient("test agent test@example.com", company_factory=lambda _t: company)

    texts = client.full_texts("AAPL")
    assert len(texts) == 1
    assert texts[0]["type"] == "10-Q"


# ---------------------------------------------------------------------------
# Caching — filings fetched once per ticker
# ---------------------------------------------------------------------------
def test_full_texts_caches_per_ticker() -> None:
    company = _company_with_8k_and_10q()
    factory_calls: list[str] = []

    def factory(ticker: str) -> FakeCompany:
        factory_calls.append(ticker)
        return company

    client = SecNarrativeClient("test agent test@example.com", company_factory=factory)

    client.full_texts("AAPL")
    client.full_texts("AAPL")
    client.narrative_docs("AAPL")

    assert factory_calls == ["AAPL"]
    assert company.get_filings_calls.count("8-K") == 1
    assert company.get_filings_calls.count("10-Q") == 1


# ---------------------------------------------------------------------------
# Guidance-sentence heuristic
# ---------------------------------------------------------------------------
def test_guidance_snippet_keeps_only_matching_sentences() -> None:
    text = (
        "The weather was pleasant during the quarter. "
        "We expect revenue to grow double digits next year. "
        "The office moved to a new building. "
        "Management provided guidance for fiscal 2027 of continued expansion."
    )
    snippet = _guidance_snippet(text)
    assert "weather" not in snippet
    assert "office moved" not in snippet
    assert "expect revenue to grow" in snippet
    assert "guidance for fiscal 2027" in snippet


def test_guidance_snippet_falls_back_to_first_chars_when_no_match() -> None:
    text = "Nothing here relates to forward statements. " * 20
    snippet = _guidance_snippet(text)
    assert snippet == text.strip()[:300]


def test_guidance_snippet_capped_length() -> None:
    sentence = "We expect strong outlook and continued growth this quarter. "
    text = sentence * 20
    snippet = _guidance_snippet(text)
    assert len(snippet) <= 500


def test_guidance_snippet_empty_text() -> None:
    assert _guidance_snippet("") == ""


# ---------------------------------------------------------------------------
# company_factory / cache_dir plumbing sanity (no real edgar import)
# ---------------------------------------------------------------------------
def test_init_with_company_factory_skips_edgar_import(tmp_path: Path) -> None:
    company = _company_with_8k_and_10q()
    client = SecNarrativeClient(
        "test agent test@example.com", cache_dir=tmp_path, company_factory=lambda _t: company
    )
    assert client._company_factory("AAPL") is company
