"""Tests for the LLM enrichment layer."""
from __future__ import annotations

import pytest

from calorch.llm import MockChatModel
from calorch.llm_enrich import LlmEnricher, NoOpEnricher


def test_noop_enricher_returns_empty():
    e = NoOpEnricher()
    assert e.enrich_headline(ticker="AAPL") == []
    assert e.enrich_guidance(ticker="AAPL") == []
    assert e.enrich_margin_walk(ticker="AAPL") == []
    assert e.enrich_risk_factors(ticker="AAPL") == []
    assert e.enrich_key_questions(ticker="AAPL") == []


def test_llm_enricher_with_mock_model():
    """MockChatModel returns JSON classification; enricher should parse it into bullets."""
    mock = MockChatModel()
    enricher = LlmEnricher(mock)
    bullets = enricher.enrich_headline(ticker="AAPL", context={"price": 248.80})
    # Mock model returns JSON with 'rationale' which becomes a bullet
    assert isinstance(bullets, list)


def test_llm_enricher_graceful_on_none_llm():
    e = LlmEnricher(None)
    # Falls back to placeholder when LLM is unavailable
    assert e.enrich_headline(ticker="AAPL") == ["AAPL earnings — see data tables above."]


def test_llm_enricher_skips_mock_json():
    """MockChatModel returns JSON classification; enricher should skip it."""
    mock = MockChatModel()
    enricher = LlmEnricher(mock)
    bullets = enricher.enrich_headline(ticker="AAPL", context={"price": 248.80})
    # Should detect JSON and return fallback, not raw JSON
    assert any("AAPL earnings" in b for b in bullets)
    assert not any("final_label" in b for b in bullets)


def test_to_bullets_parses_markdown():
    e = LlmEnricher(MockChatModel())
    raw = """- First point
* Second point
• Third point
1. Numbered
Normal line"""
    bullets = e._to_bullets(raw)
    assert bullets == ["First point", "Second point", "Third point", "Numbered", "Normal line"]
