"""Tests for `calorch.guidance_extract` — optional LLM-based guidance-snippet
extraction (ingestion-time only) and its `resolve_extractor` policy.

No network, no real LLM: `llm_guidance_snippet` is driven entirely through a
fake `invoke: str -> str` callable.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from calorch.config import Settings
from calorch.guidance_extract import llm_guidance_snippet, resolve_extractor


def _settings(**overrides: Any) -> Settings:
    base = Settings(
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
    return dataclasses.replace(base, **overrides)


_SOURCE = (
    "Apple today announced financial results. Revenue was $100 billion, up 10% "
    "year over year. We expect continued strong growth in the coming fiscal "
    "2026 quarters. The Board of Directors declared a dividend."
)


# ---------------------------------------------------------------------------
# llm_guidance_snippet — happy path
# ---------------------------------------------------------------------------
def test_verbatim_reply_accepted() -> None:
    reply = "We expect continued strong growth in the coming fiscal 2026 quarters."

    result = llm_guidance_snippet(_SOURCE, "AAPL", lambda _p: reply)

    assert result == reply


def test_result_capped_at_max_chars() -> None:
    long_sentence = "We expect continued strong growth in the coming fiscal 2026 quarters. " * 20
    # Make the "reply" verbatim by embedding it in a long source too.
    source = long_sentence
    reply = long_sentence

    result = llm_guidance_snippet(source, "AAPL", lambda _p: reply, max_chars=50)

    assert result is not None
    assert len(result) <= 50


def test_none_reply_returns_none() -> None:
    assert llm_guidance_snippet(_SOURCE, "AAPL", lambda _p: "NONE") is None
    assert llm_guidance_snippet(_SOURCE, "AAPL", lambda _p: "  none  ") is None


def test_empty_source_text_returns_none_without_invoking() -> None:
    calls: list[str] = []

    def invoke(p: str) -> str:
        calls.append(p)
        return "whatever"

    assert llm_guidance_snippet("", "AAPL", invoke) is None
    assert llm_guidance_snippet("   ", "AAPL", invoke) is None
    assert calls == []


# ---------------------------------------------------------------------------
# Verbatim guard — hallucination defence
# ---------------------------------------------------------------------------
def test_hallucinated_reply_rejected() -> None:
    reply = "The company will achieve 500% margin expansion via cold fusion synergies."

    result = llm_guidance_snippet(_SOURCE, "AAPL", lambda _p: reply)

    assert result is None


def test_partially_hallucinated_reply_rejected_below_threshold() -> None:
    # Two sentences: one verbatim, one fabricated -> 50% match ratio, below the 60% bar.
    reply = (
        "We expect continued strong growth in the coming fiscal 2026 quarters. "
        "Margins will expand by 900 basis points due to unicorn tears."
    )

    result = llm_guidance_snippet(_SOURCE, "AAPL", lambda _p: reply)

    assert result is None


def test_majority_verbatim_reply_accepted_above_threshold() -> None:
    # Two verbatim sentences out of three (>= 60%) should pass the guard.
    reply = (
        "Revenue was $100 billion, up 10% year over year. "
        "We expect continued strong growth in the coming fiscal 2026 quarters. "
        "The Board of Directors declared a dividend."
    )

    result = llm_guidance_snippet(_SOURCE, "AAPL", lambda _p: reply)

    assert result == reply


# ---------------------------------------------------------------------------
# Defensive invoke handling
# ---------------------------------------------------------------------------
def test_invoke_raising_returns_none() -> None:
    def _boom(_p: str) -> str:
        raise RuntimeError("provider unavailable")

    assert llm_guidance_snippet(_SOURCE, "AAPL", _boom) is None


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------
def test_markdown_fenced_reply_parsed() -> None:
    sentence = "We expect continued strong growth in the coming fiscal 2026 quarters."
    reply = f"```\n{sentence}\n```"

    result = llm_guidance_snippet(_SOURCE, "AAPL", lambda _p: reply)

    assert result == sentence


def test_quoted_reply_parsed() -> None:
    sentence = "We expect continued strong growth in the coming fiscal 2026 quarters."
    reply = f'"{sentence}"'

    result = llm_guidance_snippet(_SOURCE, "AAPL", lambda _p: reply)

    assert result == sentence


# ---------------------------------------------------------------------------
# Input truncation
# ---------------------------------------------------------------------------
def test_long_input_truncated_before_prompting() -> None:
    # Non-repeating tail (a unique marker) so a substring match can't occur by
    # coincidence against the repeated filler earlier in the text.
    long_text = ("We expect continued strong growth this quarter. " * 220) + "UNIQUE_TAIL_MARKER_ZZZ"
    assert len(long_text) > 10_000
    seen_prompts: list[str] = []

    def invoke(prompt: str) -> str:
        seen_prompts.append(prompt)
        return "NONE"

    llm_guidance_snippet(long_text, "AAPL", invoke)

    assert len(seen_prompts) == 1
    prompt = seen_prompts[0]
    # The excerpt embedded in the prompt is capped to ~10k chars of source text.
    assert long_text[:10_000] in prompt
    assert "UNIQUE_TAIL_MARKER_ZZZ" not in prompt


# ---------------------------------------------------------------------------
# resolve_extractor
# ---------------------------------------------------------------------------
def test_resolve_extractor_explicit_llm_passes_through() -> None:
    s = _settings(guidance_extractor="llm", use_mocks=True)
    assert resolve_extractor(s) == "llm"


def test_resolve_extractor_explicit_heuristic_passes_through() -> None:
    s = _settings(guidance_extractor="heuristic", use_mocks=False, azure_openai_api_key="k")
    assert resolve_extractor(s) == "heuristic"


def test_resolve_extractor_auto_with_mocks_is_heuristic() -> None:
    s = _settings(guidance_extractor="auto", use_mocks=True, azure_openai_api_key="k")
    assert resolve_extractor(s) == "heuristic"


def test_resolve_extractor_auto_no_credentials_is_heuristic() -> None:
    s = _settings(guidance_extractor="auto", use_mocks=False, azure_openai_api_key=None, opencode_go_api_key=None)
    assert resolve_extractor(s) == "heuristic"


def test_resolve_extractor_auto_with_azure_key_and_no_mocks_is_llm() -> None:
    s = _settings(guidance_extractor="auto", use_mocks=False, azure_openai_api_key="k")
    assert resolve_extractor(s) == "llm"


def test_resolve_extractor_auto_with_opencode_key_and_no_mocks_is_llm() -> None:
    s = _settings(guidance_extractor="auto", use_mocks=False, opencode_go_api_key="k")
    assert resolve_extractor(s) == "llm"
