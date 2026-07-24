"""Tests for the provider dispatcher — SEC EDGAR + AlphaSense (+ SEC narrative/lexicon)."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from calorch.config import Settings
from calorch.providers import (
    AlphaSenseSentimentProvider,
    EftsFilingsProvider,
    IxbrlFundamentalsProvider,
    IxbrlSegmentProvider,
    LexiconSentimentProvider,
    ProviderBundle,
    SecNarrativeProvider,
    build_providers,
)


@pytest.fixture
def base_settings() -> Settings:
    return Settings(
        azure_openai_api_key=None,
        azure_openai_endpoint=None,
        azure_openai_deployment="gpt-4o",
        azure_openai_api_version="2024-08-01-preview",
        graph_tenant_id=None,
        graph_client_id=None,
        graph_client_secret=None,
        graph_user_id="me",
        onedrive_drive_id=None,
        repo_backend="json",
        repo_path=Path("./out/repository.json"),
        repo_table_name="calorchdelivery",
        search_endpoint=None,
        search_index="calorch-knowledge",
        search_api_key=None,
        search_semantic_config=None,
        rag_top_k=4,
        knowledge_writeback=True,
        approver_emails=[],
        approval_base_url=None,
        opencode_go_api_key=None,
        opencode_go_model="glm-5.1",
        sec_user_agent="Test/test@example.com",
        sec_cache_dir=Path(".cache/sec"),
        sec_watchlist=["AAPL"],
        sec_forms=None,
        use_ixbrl_segments=True,
        use_sec_efts=True,
        sec_backend="native",
        alphasense_api_key=None,
        alphasense_client_id=None,
        alphasense_client_secret=None,
        alphasense_username=None,
        alphasense_password=None,
        alphasense_base_url="https://api.alpha-sense.com",
        use_alphasense=True,
        narrative_backend="auto",
        sentiment_backend="auto",
        guidance_extractor="auto",
        use_mocks=True,
        output_dir=Path("./out"),
        langsmith_api_key=None,
        langsmith_project="calorch",
        langsmith_tracing=False,
        azure_storage_connection_string=None,
        azure_storage_account_url=None,
        blob_input_container="calorch-inputs",
        blob_output_container="calorch-outputs",
        blob_local_root=None,
        use_blob_providers=False,
    )


def test_build_providers_returns_bundle(base_settings: Settings) -> None:
    bundle = build_providers(base_settings)
    assert isinstance(bundle, ProviderBundle)
    for slot in ("fundamentals", "segments", "filings", "narrative", "transcripts", "sentiment", "sources"):
        assert hasattr(bundle, slot)
    # the removed market-data slots must be gone
    assert not hasattr(bundle, "price")
    assert not hasattr(bundle, "consensus")
    assert not hasattr(bundle, "macro")


def test_provider_bundle_ops_defaults_to_none(base_settings: Settings) -> None:
    """`build_providers` (the SEC/AlphaSense factory) never wires `ops` —
    only `calorch.tools.make_providers` does, so a bare ProviderBundle
    from here degrades cleanly for internal_review (no fabricated stats).
    """
    bundle = build_providers(base_settings)
    assert bundle.ops is None


def test_provider_bundle_has_sources(base_settings: Settings) -> None:
    bundle = build_providers(base_settings)
    names = {s["source_name"] for s in bundle.sources}
    assert {"SEC iXBRL", "SEC EFTS", "AlphaSense"} <= names
    for src in bundle.sources:
        assert {"source_name", "status", "detail"} <= set(src)


def test_alphasense_missing_without_key(base_settings: Settings) -> None:
    # Forcing both slots to "alphasense" preserves the pre-SEC-backend
    # semantics of this test: no ALPHASENSE_API_KEY -> everything AlphaSense
    # degrades to empty. (With the default "auto" backend, a missing key
    # would instead fall back to the free SEC narrative/lexicon backend —
    # covered by test_narrative_backend_auto_falls_back_to_sec below.)
    settings = dataclasses.replace(base_settings, narrative_backend="alphasense", sentiment_backend="alphasense")
    bundle = build_providers(settings)
    assert bundle.narrative.guidance_hits("0000320193", "AAPL") == []
    assert bundle.transcripts.transcript_hits("AAPL") == []
    sent = bundle.sentiment.sentiment("AAPL")
    assert sent["mean_sentiment"] is None
    assert any(s["source_name"] == "AlphaSense" and s["status"] == "missing" for s in bundle.sources)


def test_sec_providers_return_empty_when_client_absent() -> None:
    assert IxbrlSegmentProvider(ixbrl=None).latest_segments("0000320193", "AAPL") == []
    assert EftsFilingsProvider(efts=None).guidance_hits("0000320193", "AAPL") == []
    funds = IxbrlFundamentalsProvider(ixbrl=None).latest_fundamentals("0000320193", "AAPL")
    assert funds.get("note") is not None


# ---------------------------------------------------------------------------
# fundamentals_history — Protocol degradation
# ---------------------------------------------------------------------------
def test_fundamentals_history_empty_when_client_absent() -> None:
    result = IxbrlFundamentalsProvider(ixbrl=None).fundamentals_history("0000320193", "AAPL")
    assert result == {"source": "sec-ixbrl", "ticker": "AAPL", "quarterly": []}


def test_fundamentals_history_empty_when_wrapped_client_lacks_method() -> None:
    """The native SecIxbrlClient has no `fundamentals_history` — the provider
    must degrade to an empty quarterly series rather than raising.
    """
    class NativeLikeClient:
        def latest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]:
            return {"source": "sec-ixbrl", "ticker": ticker, "cik": cik}

    result = IxbrlFundamentalsProvider(ixbrl=NativeLikeClient()).fundamentals_history("0000320193", "AAPL")
    assert result == {"source": "sec-ixbrl", "ticker": "AAPL", "quarterly": []}


def test_fundamentals_history_delegates_when_client_has_method() -> None:
    class EdgarLikeClient:
        def latest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]:
            return {}

        def fundamentals_history(self, cik: str, ticker: str, *, quarters: int = 5) -> dict[str, Any]:
            return {"source": "sec-edgartools", "ticker": ticker, "cik": cik, "quarterly": [{"label": "Q2 2026"}],
                    "quarters_requested": quarters}

    result = IxbrlFundamentalsProvider(ixbrl=EdgarLikeClient()).fundamentals_history("0000320193", "AAPL", quarters=3)
    assert result["source"] == "sec-edgartools"
    assert result["quarterly"] == [{"label": "Q2 2026"}]
    assert result["quarters_requested"] == 3


def test_fundamentals_history_degrades_on_exception() -> None:
    class BoomClient:
        def latest_fundamentals(self, cik: str, ticker: str) -> dict[str, Any]:
            return {}

        def fundamentals_history(self, cik: str, ticker: str, *, quarters: int = 5) -> dict[str, Any]:
            raise ValueError("boom")

    result = IxbrlFundamentalsProvider(ixbrl=BoomClient()).fundamentals_history("0000320193", "AAPL")
    assert result["source"] == "sec-ixbrl"
    assert result["quarterly"] == []
    assert "note" in result


def test_alphasense_sentiment_provider_none_client() -> None:
    s = AlphaSenseSentimentProvider(client=None).sentiment("AAPL")
    assert s["mean_sentiment"] is None and s["source"] == "none"


# ---------------------------------------------------------------------------
# SEC narrative / lexicon provider degraded shapes
# ---------------------------------------------------------------------------
def test_sec_narrative_provider_none_client() -> None:
    assert SecNarrativeProvider(client=None).guidance_hits("0000320193", "AAPL") == []


def test_sec_narrative_provider_degrades_on_exception() -> None:
    class Boom:
        def narrative_docs(self, ticker: str, *, limit: int = 5) -> Any:
            raise RuntimeError("boom")

    assert SecNarrativeProvider(client=Boom()).guidance_hits("0000320193", "AAPL") == []


def test_lexicon_sentiment_provider_none_client() -> None:
    s = LexiconSentimentProvider(client=None).sentiment("AAPL")
    assert s["mean_sentiment"] is None and s["sample"] == 0 and s["source"] == "none"


def test_lexicon_sentiment_provider_degrades_on_exception() -> None:
    class Boom:
        def full_texts(self, ticker: str, *, limit: int = 3) -> Any:
            raise RuntimeError("boom")

    s = LexiconSentimentProvider(client=Boom()).sentiment("AAPL")
    assert s["mean_sentiment"] is None and s["source"] == "sec-lexicon" and "note" in s


class _StubSecNarrativeClient:
    """Fake SecNarrativeClient — no edgartools, no network."""

    def __init__(self, user_agent: str, cache_dir: Path | None = None) -> None:
        self.user_agent = user_agent
        self.cache_dir = cache_dir

    def narrative_docs(self, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        return [{
            "title": f"{ticker} 8-K press release", "date": "2026-04-30", "type": "8-K",
            "company": ticker, "ticker": ticker, "sentiment": None, "source": "sec-filings",
            "doc_id": "0000320193-26-000011", "snippet": "We expect strong growth next quarter.",
        }][:limit]

    def full_texts(self, ticker: str, *, limit: int = 3) -> list[dict[str, Any]]:
        return [{"title": f"{ticker} 8-K", "date": "2026-04-30", "type": "8-K",
                 "text": "Revenue growth was strong and margins improved."}][:limit]


# ---------------------------------------------------------------------------
# Backend resolution — providers.build_providers
# ---------------------------------------------------------------------------
def test_narrative_backend_auto_falls_back_to_sec(
    base_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.sec_narrative as sec_narrative_mod

    monkeypatch.setattr(sec_narrative_mod, "SecNarrativeClient", _StubSecNarrativeClient)

    bundle = build_providers(base_settings)  # narrative/sentiment_backend="auto", no AlphaSense key
    hits = bundle.narrative.guidance_hits("0000320193", "AAPL")
    assert len(hits) == 1
    assert hits[0]["source"] == "sec-filings"
    sent = bundle.sentiment.sentiment("AAPL")
    assert sent["source"] == "sec-lexicon"
    assert sent["mean_sentiment"] is not None
    assert any(s["source_name"] == "narrative" and s["detail"] == "backend=sec" for s in bundle.sources)
    assert any(s["source_name"] == "sentiment" and s["detail"] == "backend=lexicon" for s in bundle.sources)


def test_narrative_sentiment_backend_explicit_mixed(
    base_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """narrative=sec, sentiment=alphasense (mixed) — both slots resolve independently."""
    import calorch.sec_narrative as sec_narrative_mod

    monkeypatch.setattr(sec_narrative_mod, "SecNarrativeClient", _StubSecNarrativeClient)
    settings = dataclasses.replace(base_settings, narrative_backend="sec", sentiment_backend="alphasense")

    bundle = build_providers(settings)
    hits = bundle.narrative.guidance_hits("0000320193", "AAPL")
    assert hits[0]["source"] == "sec-filings"
    # AlphaSense not configured -> sentiment degrades to the AlphaSense "none" shape.
    sent = bundle.sentiment.sentiment("AAPL")
    assert sent["source"] == "none"


def test_narrative_backend_sec_import_error_degrades(
    base_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.sec_narrative as sec_narrative_mod

    class ExplodingSecNarrativeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError("edgartools not installed")

    monkeypatch.setattr(sec_narrative_mod, "SecNarrativeClient", ExplodingSecNarrativeClient)
    settings = dataclasses.replace(base_settings, narrative_backend="sec", sentiment_backend="lexicon")

    bundle = build_providers(settings)
    assert bundle.narrative.guidance_hits("0000320193", "AAPL") == []
    sent = bundle.sentiment.sentiment("AAPL")
    assert sent["mean_sentiment"] is None
    assert any(s["source_name"] == "SEC narrative" and s["status"] == "error" for s in bundle.sources)
    # The slot itself must not claim "active" when no client backs it —
    # the Data Sources section would otherwise overstate coverage.
    narrative_slot = next(s for s in bundle.sources if s["source_name"] == "narrative")
    sentiment_slot = next(s for s in bundle.sources if s["source_name"] == "sentiment")
    assert narrative_slot["status"] == "unavailable"
    assert sentiment_slot["status"] == "unavailable"


def test_narrative_backend_alphasense_without_credentials_reported_unavailable(
    base_settings: Settings,
) -> None:
    """Explicitly selecting alphasense with no credentials must not read as active."""
    settings = dataclasses.replace(base_settings, narrative_backend="alphasense")

    bundle = build_providers(settings)

    narrative_slot = next(s for s in bundle.sources if s["source_name"] == "narrative")
    assert narrative_slot["status"] == "unavailable"
    assert "backend=alphasense" in narrative_slot["detail"]


# ---------------------------------------------------------------------------
# BlobFundamentalsProvider.fundamentals_history — round-trip + latest-date
# fallback, via LocalBlobStore (no Azure, no network).
# ---------------------------------------------------------------------------
def test_blob_fundamentals_history_round_trip(tmp_path: Path) -> None:
    from calorch.blob_reader import BlobFundamentalsProvider
    from calorch.blob_store import LocalBlobStore

    blob = LocalBlobStore(tmp_path / "blobs")
    history = {"source": "sec-edgartools", "ticker": "AAPL", "cik": "0000320193",
               "quarterly": [{"label": "Q2 2026", "revenue": 111.2e9}]}
    blob.upload_json(blob.input_container, "inputs/fundamentals_history/0000320193/AAPL/20260716.json", history)

    provider = BlobFundamentalsProvider(blob, date="20260716")
    result = provider.fundamentals_history("0000320193", "AAPL")

    assert result == history


def test_blob_fundamentals_history_falls_back_to_latest_available_date(tmp_path: Path) -> None:
    """Same-day blob is missing -> fall back to the most recent prior date
    for this cik/ticker, discovered via `list_blobs`.
    """
    from calorch.blob_reader import BlobFundamentalsProvider
    from calorch.blob_store import LocalBlobStore

    blob = LocalBlobStore(tmp_path / "blobs")
    older = {"quarterly": [{"label": "Q1 2026", "revenue": 143.8e9}]}
    newer = {"quarterly": [{"label": "Q2 2026", "revenue": 111.2e9}]}
    blob.upload_json(blob.input_container, "inputs/fundamentals_history/0000320193/AAPL/20260601.json", older)
    blob.upload_json(blob.input_container, "inputs/fundamentals_history/0000320193/AAPL/20260701.json", newer)

    # Requested date (today) has no blob -> should fall back to 20260701 (latest of the two).
    provider = BlobFundamentalsProvider(blob, date="20260716")
    result = provider.fundamentals_history("0000320193", "AAPL")

    assert result == newer


def test_blob_fundamentals_history_missing_entirely_returns_empty_quarterly(tmp_path: Path) -> None:
    from calorch.blob_reader import BlobFundamentalsProvider
    from calorch.blob_store import LocalBlobStore

    blob = LocalBlobStore(tmp_path / "blobs")
    provider = BlobFundamentalsProvider(blob, date="20260716")

    result = provider.fundamentals_history("0000320193", "AAPL")

    assert result["quarterly"] == []
    assert "note" in result


def test_blob_fundamentals_history_ignores_other_tickers(tmp_path: Path) -> None:
    """The prefix scan must not leak another ticker's history into the fallback."""
    from calorch.blob_reader import BlobFundamentalsProvider
    from calorch.blob_store import LocalBlobStore

    blob = LocalBlobStore(tmp_path / "blobs")
    blob.upload_json(
        blob.input_container, "inputs/fundamentals_history/0000789019/MSFT/20260601.json",
        {"quarterly": [{"label": "MSFT quarter"}]},
    )

    provider = BlobFundamentalsProvider(blob, date="20260716")
    result = provider.fundamentals_history("0000320193", "AAPL")

    assert result["quarterly"] == []
