"""Tests for the LLM enrichment layer."""
from __future__ import annotations


from calorch.llm import MockChatModel
from calorch.llm_enrich import LlmEnricher, NoOpEnricher


def test_noop_enricher_returns_empty():
    e = NoOpEnricher()
    assert e.enrich_headline(ticker="AAPL") == []
    assert e.enrich_guidance(ticker="AAPL") == []
    assert e.enrich_margin_walk(ticker="AAPL") == []
    assert e.enrich_risk_factors(ticker="AAPL") == []
    assert e.enrich_key_questions(ticker="AAPL") == []
    assert e.enrich_whats_changed(ticker="AAPL") == []


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


def test_to_bullets_keeps_snippet_quoting_output():
    """_THINKING_PHRASES must not eat a bullet that quotes a guidance
    excerpt or trend string — the analyst-grade redesign feeds ctx keys
    like ``revenue_trend``/``guidance_excerpts`` into the LLM prompt and
    expects the model's bullets to reference that data verbatim.
    """
    e = LlmEnricher(MockChatModel())
    raw = (
        "- Revenue grew to $100.0B in Q2 FY2026 (+17.6% YoY), continuing the "
        "trend from Q1 FY2026's $95.0B print.\n"
        "- Management said: \"We expect continued strong demand next quarter\" "
        "in the latest 8-K press release.\n"
        "- Gross margin expanded to 46.0%, up from 44.7% a year ago.\n"
    )
    bullets = e._to_bullets(raw)
    assert len(bullets) == 3
    assert any("100.0B" in b and "17.6%" in b for b in bullets)
    assert any("We expect continued strong demand" in b for b in bullets)


# ---------------------------------------------------------------------------
# Part A — enrich_whats_changed: analyst synthesis over the trend series.
# ---------------------------------------------------------------------------
_GROUNDED_CTX = {
    "revenue_trend": "Q2 FY2026: $100.0B (+17.6% YoY) | Q1 FY2026: $95.0B (+11.8% YoY)",
    "margin_trend": (
        "Q2 FY2026: GM 46.0% / OM 30.0% / NM 25.0% | "
        "Q1 FY2026: GM 45.3% / OM 28.4% / NM 23.2%"
    ),
    "fcf_trend": "Q2 FY2026: $29.0B | Q1 FY2026: $27.2B",
    "fcf_conversion_trend": "Q2 FY2026: FCF/NI 116% | Q1 FY2026: FCF/NI 124%",
}


class _ExplodingLLM:
    """Fails the test if the enricher ever calls the model — used to prove
    the grounding check short-circuits *before* any LLM call is attempted.
    """

    def invoke(self, *args, **kwargs):  # noqa: D401 - test double
        raise AssertionError("LLM should not be called without grounding data")


class _FakeBulletLLM:
    """Deterministic stand-in that returns real analyst-style bullet text
    (unlike MockChatModel, which always returns classification JSON), so we
    can verify the raw-LLM-output path -- not just the data-driven fallback.
    """

    def __init__(self, content: str) -> None:
        self._content = content

    def invoke(self, messages, **kwargs):
        class _Resp:
            def __init__(self, content: str) -> None:
                self.content = content

        return _Resp(self._content)


def test_enrich_whats_changed_self_omits_without_grounding():
    """No trend data at all, or trend keys present but all dashes -> the
    section has nothing to analyze, so this returns [] without ever
    invoking the model (the exploding LLM would fail the test if called).
    """
    e = LlmEnricher(_ExplodingLLM())
    assert e.enrich_whats_changed(ticker="AAPL", context={}) == []
    assert e.enrich_whats_changed(
        ticker="AAPL",
        context={"revenue_trend": "—", "margin_trend": "—", "fcf_trend": "—"},
    ) == []


def test_enrich_whats_changed_mock_model_uses_grounded_fallback():
    """MockChatModel's classification JSON is discarded (same as every other
    enrich_* method), so with grounding data present this exercises the
    method's own data-driven fallback -- it must still be non-empty and
    must not leak the mock's raw JSON.
    """
    e = LlmEnricher(MockChatModel())
    bullets = e.enrich_whats_changed(ticker="AAPL", context=_GROUNDED_CTX)
    assert bullets
    assert any("Revenue trajectory" in b for b in bullets)
    assert not any("final_label" in b for b in bullets)


def test_enrich_whats_changed_survives_thinking_phrase_filter():
    """A plausible, well-formed analyst response (three grounded bullets,
    each citing a specific number) must pass _to_bullets/_THINKING_PHRASES
    intact -- confirming the new method's own hardcoded prompt strings
    don't accidentally poison the ratio, and that grounded analyst prose
    survives the filter.
    """
    content = (
        "- Operating margin inflected higher to 30.0% from 28.4% QoQ, a 160bps "
        "sequential expansion driven by opex leverage.\n"
        "- FCF grew to $29.0B while net income growth lagged, signalling "
        "improving cash conversion (FCF/NI 116% vs 124% last quarter).\n"
        "- Revenue growth held at +17.6% YoY, still accelerating from Q1's "
        "+11.8% YoY print -- no deceleration signal yet.\n"
    )
    e = LlmEnricher(_FakeBulletLLM(content))
    bullets = e.enrich_whats_changed(ticker="AAPL", context=_GROUNDED_CTX)
    assert len(bullets) == 3
    assert any("Operating margin inflected" in b for b in bullets)
    assert any("FCF grew to $29.0B" in b for b in bullets)


def test_enrich_whats_changed_llm_failure_falls_back_to_data_driven_bullets():
    """When the LLM call fails (llm=None -> _call returns ""), grounded
    context still produces a non-empty, purely data-driven fallback.
    """
    e = LlmEnricher(None)
    bullets = e.enrich_whats_changed(ticker="AAPL", context=_GROUNDED_CTX)
    assert bullets == [
        "Revenue trajectory (AAPL): Q2 FY2026: $100.0B (+17.6% YoY) | Q1 FY2026: $95.0B (+11.8% YoY)",
        "Margin trajectory: Q2 FY2026: GM 46.0% / OM 30.0% / NM 25.0% | Q1 FY2026: GM 45.3% / OM 28.4% / NM 23.2%",
        "Free cash flow trajectory: Q2 FY2026: $29.0B | Q1 FY2026: $27.2B",
        "FCF-to-net-income conversion: Q2 FY2026: FCF/NI 116% | Q1 FY2026: FCF/NI 124%",
    ]


def test_noop_enricher_whats_changed_grounded_vs_empty():
    """NoOpEnricher has no model to call, but this section's fallback is
    data-only anyway, so it should still produce grounded bullets when
    trend data is present -- and still self-omit when it isn't.
    """
    e = NoOpEnricher()
    assert e.enrich_whats_changed(ticker="AAPL", context={}) == []
    partial_ctx = {"revenue_trend": "Q2 FY2026: $100.0B (+17.6% YoY)", "margin_trend": "—", "fcf_trend": "—"}
    bullets = e.enrich_whats_changed(ticker="AAPL", context=partial_ctx)
    assert bullets == ["Revenue trajectory (AAPL): Q2 FY2026: $100.0B (+17.6% YoY)"]


def test_ctx_prompt_truncates_trend_keys_to_240_not_80():
    """Trend/excerpt ctx values get a longer truncation budget than plain
    scalar fields — a 5-quarter trend string easily exceeds 80 chars.
    """
    e = LlmEnricher(MockChatModel())
    long_trend = " | ".join(f"Q{i} FY2026: ${90 + i}.0B (+{i}.0% YoY)" for i in range(1, 6))
    assert len(long_trend) > 80
    prompt = e._ctx_prompt("AAPL", "Apple", "earnings_call", {"revenue_trend": long_trend}, "task")
    assert long_trend[:100] in prompt  # not truncated at 80 chars
    # A same-length plain (non-trend/excerpt) key is still capped at 80.
    prompt_plain = e._ctx_prompt("AAPL", "Apple", "earnings_call", {"some_field": long_trend}, "task")
    assert long_trend[:100] not in prompt_plain
