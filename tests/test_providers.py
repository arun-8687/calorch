"""Tests for the provider dispatcher — SEC EDGAR + AlphaSense only."""
from __future__ import annotations

from pathlib import Path

import pytest

from calorch.config import Settings
from calorch.providers import (
    AlphaSenseSentimentProvider,
    EftsFilingsProvider,
    IxbrlFundamentalsProvider,
    IxbrlSegmentProvider,
    ProviderBundle,
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
        alphasense_api_key=None,
        alphasense_client_id=None,
        alphasense_client_secret=None,
        alphasense_username=None,
        alphasense_password=None,
        alphasense_base_url="https://api.alpha-sense.com",
        use_alphasense=True,
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


def test_provider_bundle_has_sources(base_settings: Settings) -> None:
    bundle = build_providers(base_settings)
    names = {s["source_name"] for s in bundle.sources}
    assert {"SEC iXBRL", "SEC EFTS", "AlphaSense"} <= names
    for src in bundle.sources:
        assert {"source_name", "status", "detail"} <= set(src)


def test_alphasense_missing_without_key(base_settings: Settings) -> None:
    bundle = build_providers(base_settings)
    # No ALPHASENSE_API_KEY → narrative/transcripts/sentiment degrade to empty.
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


def test_alphasense_sentiment_provider_none_client() -> None:
    s = AlphaSenseSentimentProvider(client=None).sentiment("AAPL")
    assert s["mean_sentiment"] is None and s["source"] == "none"
