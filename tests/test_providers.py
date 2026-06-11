"""Tests for the provider dispatcher + Protocol-based wiring."""
from __future__ import annotations

from pathlib import Path

import pytest

from calorch.config import Settings
from calorch.providers import (
    IxbrlSegmentProvider,
    ProviderBundle,
    TiingoConsensusProvider,
    TiingoPriceProvider,
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
        factset_api_key=None,
        bloomberg_blpapi_host=None,
        lseg_client_id=None,
        spglobal_api_key=None,
        tiingo_api_key=None,
        opencode_go_api_key=None,
        opencode_go_model="glm-5.1",
        fred_api_key=None,
        use_fred=True,
        use_ixbrl_segments=True,
        use_sec_efts=True,
        use_fed_h15=True,
        sec_user_agent="Test/test@example.com",
        sec_cache_dir=Path(".cache/sec"),
        sec_watchlist=["AAPL"],
        sec_forms=None,
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
    assert hasattr(bundle, "price")
    assert hasattr(bundle, "consensus")
    assert hasattr(bundle, "macro")
    assert hasattr(bundle, "segments")
    assert hasattr(bundle, "narrative")
    assert hasattr(bundle, "sources")


def test_provider_bundle_has_sources(base_settings: Settings) -> None:
    bundle = build_providers(base_settings)
    assert len(bundle.sources) >= 3  # SEC iXBRL, SEC EFTS, FOMC H.15 at minimum
    # Every source must have required fields
    for src in bundle.sources:
        assert "source_name" in src
        assert "status" in src
        assert "detail" in src


def test_price_returns_missing_when_no_key(base_settings: Settings) -> None:
    bundle = build_providers(base_settings)
    q = bundle.price.quote("AAPL")
    assert isinstance(q, dict)
    assert q.get("source") == "none" or q.get("note") is not None
    assert q.get("price") is None  # No TIINGO_API_KEY set


def test_consensus_returns_missing_when_no_key(base_settings: Settings) -> None:
    bundle = build_providers(base_settings)
    e = bundle.consensus.estimates("AAPL")
    assert isinstance(e, dict)
    assert e.get("source") == "none" or e.get("note") is not None


def test_segments_returns_empty_list_when_disabled() -> None:
    # IxbrlSegmentProvider with ixbrl=None returns empty
    seg = IxbrlSegmentProvider(ixbrl=None)
    result = seg.latest_segments("0000320193", "AAPL")
    assert result == []


def test_price_shows_note_when_no_tiingo() -> None:
    p = TiingoPriceProvider(tiingo=None)
    q = p.quote("AAPL")
    assert q.get("note") == "TIINGO_API_KEY not set"


def test_consensus_shows_note_when_no_tiingo() -> None:
    c = TiingoConsensusProvider(tiingo=None)
    e = c.estimates("AAPL")
    assert e.get("note") == "TIINGO_API_KEY not set"
