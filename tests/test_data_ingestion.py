"""Tests for `IngestionPipeline.ingest_qualitative` — backend-selectable
narrative + sentiment ingestion (SEC filings + local lexicon, or AlphaSense).

No network: the SEC path is driven through a monkeypatched
`calorch.sec_narrative.SecNarrativeClient`; the AlphaSense path through a
lightweight stub client.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from calorch.blob_store import LocalBlobStore
from calorch.data_ingestion import IngestionPipeline


class _StubSecNarrativeClient:
    def __init__(self, user_agent: str, cache_dir: Path | None = None) -> None:
        self.user_agent = user_agent
        self.cache_dir = cache_dir

    def narrative_docs(self, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        return [{
            "title": f"{ticker} 8-K press release", "date": "2026-04-30", "type": "8-K",
            "company": ticker, "ticker": ticker, "sentiment": None, "source": "sec-filings",
            "doc_id": "0000320193-26-000011", "snippet": "We expect continued growth.",
        }]

    def full_texts(self, ticker: str, *, limit: int = 3) -> list[dict[str, Any]]:
        return [{"title": f"{ticker} 8-K", "date": "2026-04-30", "type": "8-K",
                 "text": "Revenue growth was strong and margins improved."}]


class _StubAlphaSenseClient:
    def __init__(self) -> None:
        self.guidance_calls: list[str] = []
        self.transcript_calls: list[str] = []
        self.sentiment_calls: list[str] = []

    def guidance_hits(self, ticker: str, *, limit: int = 10) -> list[dict[str, Any]]:
        self.guidance_calls.append(ticker)
        return [{"title": "AlphaSense doc", "date": "2026-05-01", "type": "TRANSCRIPT",
                 "company": ticker, "ticker": ticker, "sentiment": 0.3, "source": "alphasense",
                 "doc_id": "as-1"}]

    def transcript_hits(self, ticker: str, *, limit: int = 10) -> list[dict[str, Any]]:
        self.transcript_calls.append(ticker)
        return [{"title": "Earnings call", "date": "2026-05-01", "type": "TRANSCRIPT",
                 "company": ticker, "ticker": ticker, "sentiment": 0.2, "source": "alphasense",
                 "doc_id": "as-2"}]

    def sentiment(self, ticker: str) -> dict[str, Any]:
        self.sentiment_calls.append(ticker)
        return {"ticker": ticker, "mean_sentiment": 0.25, "label": "positive", "sample": 3, "source": "alphasense"}


def _pipeline(tmp_path: Path, **settings_overrides: Any) -> IngestionPipeline:
    blob = LocalBlobStore(tmp_path / "blobs")
    pipeline = IngestionPipeline(blob_store=blob, date="20260711")
    pipeline._s = dataclasses.replace(pipeline._s, sec_cache_dir=tmp_path / "sec", **settings_overrides)
    return pipeline


# ---------------------------------------------------------------------------
# SEC path — narrative=sec, sentiment=lexicon
# ---------------------------------------------------------------------------
def test_sec_path_writes_narrative_and_sentiment_no_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.sec_narrative as sec_narrative_mod

    monkeypatch.setattr(sec_narrative_mod, "SecNarrativeClient", _StubSecNarrativeClient)
    pipeline = _pipeline(tmp_path, narrative_backend="sec", sentiment_backend="lexicon", alphasense_api_key=None)

    result = pipeline.ingest_qualitative("AAPL")

    assert result["status"] == "ok"
    assert result["narrative_backend"] == "sec"
    assert result["sentiment_backend"] == "lexicon"

    narrative = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/narrative/AAPL/20260711.json")
    assert narrative[0]["source"] == "sec-filings"

    sentiment = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/sentiment/AAPL/20260711.json")
    assert sentiment["source"] == "sec-lexicon"
    assert sentiment["ticker"] == "AAPL"
    assert sentiment["mean_sentiment"] is not None

    assert not pipeline._blob.exists(pipeline._blob.input_container, "inputs/transcripts/AAPL/20260711.json")


def test_auto_backend_resolves_to_sec_when_alphasense_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.sec_narrative as sec_narrative_mod

    monkeypatch.setattr(sec_narrative_mod, "SecNarrativeClient", _StubSecNarrativeClient)
    pipeline = _pipeline(tmp_path, narrative_backend="auto", sentiment_backend="auto", alphasense_api_key=None)

    result = pipeline.ingest_qualitative("AAPL")

    assert result["narrative_backend"] == "sec"
    assert result["sentiment_backend"] == "lexicon"
    assert not pipeline._blob.exists(pipeline._blob.input_container, "inputs/transcripts/AAPL/20260711.json")


# ---------------------------------------------------------------------------
# AlphaSense path — narrative=alphasense, sentiment=alphasense (unchanged
# ingest_alphasense behavior, including the transcripts blob)
# ---------------------------------------------------------------------------
def test_alphasense_path_delegates_to_ingest_alphasense(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, narrative_backend="alphasense", sentiment_backend="alphasense")
    stub = _StubAlphaSenseClient()

    result = pipeline.ingest_qualitative("AAPL", alphasense_client=stub)

    assert result["status"] == "ok"
    assert stub.guidance_calls == ["AAPL"]
    assert stub.transcript_calls == ["AAPL"]
    assert stub.sentiment_calls == ["AAPL"]

    narrative = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/narrative/AAPL/20260711.json")
    assert narrative[0]["source"] == "alphasense"
    transcripts = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/transcripts/AAPL/20260711.json")
    assert transcripts[0]["source"] == "alphasense"
    sentiment = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/sentiment/AAPL/20260711.json")
    assert sentiment["source"] == "alphasense"


# ---------------------------------------------------------------------------
# Mixed backends — narrative=sec, sentiment=alphasense
# ---------------------------------------------------------------------------
def test_mixed_backends_narrative_sec_sentiment_alphasense(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.sec_narrative as sec_narrative_mod

    monkeypatch.setattr(sec_narrative_mod, "SecNarrativeClient", _StubSecNarrativeClient)
    pipeline = _pipeline(tmp_path, narrative_backend="sec", sentiment_backend="alphasense")
    stub = _StubAlphaSenseClient()

    result = pipeline.ingest_qualitative("AAPL", alphasense_client=stub)

    assert result["status"] == "ok"
    assert stub.sentiment_calls == ["AAPL"]
    assert stub.guidance_calls == []  # narrative came from the SEC backend, not AlphaSense
    assert stub.transcript_calls == []  # not the pure AlphaSense path -> no transcripts blob

    narrative = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/narrative/AAPL/20260711.json")
    assert narrative[0]["source"] == "sec-filings"
    sentiment = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/sentiment/AAPL/20260711.json")
    assert sentiment["source"] == "alphasense"
    assert not pipeline._blob.exists(pipeline._blob.input_container, "inputs/transcripts/AAPL/20260711.json")


# ---------------------------------------------------------------------------
# Degraded shapes
# ---------------------------------------------------------------------------
def test_sec_narrative_client_import_error_degrades_to_empty_narrative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.sec_narrative as sec_narrative_mod

    class ExplodingSecNarrativeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError("edgartools not installed")

    monkeypatch.setattr(sec_narrative_mod, "SecNarrativeClient", ExplodingSecNarrativeClient)
    pipeline = _pipeline(tmp_path, narrative_backend="sec", sentiment_backend="lexicon")

    result = pipeline.ingest_qualitative("AAPL")

    assert result["status"] == "ok"
    narrative = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/narrative/AAPL/20260711.json")
    assert narrative == []
    sentiment = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/sentiment/AAPL/20260711.json")
    assert sentiment["mean_sentiment"] is None


def test_ingest_qualitative_degrades_on_narrative_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.sec_narrative as sec_narrative_mod

    class BoomSecNarrativeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def narrative_docs(self, ticker: str, *, limit: int = 5) -> Any:
            raise RuntimeError("boom")

        def full_texts(self, ticker: str, *, limit: int = 3) -> Any:
            return []

    monkeypatch.setattr(sec_narrative_mod, "SecNarrativeClient", BoomSecNarrativeClient)
    pipeline = _pipeline(tmp_path, narrative_backend="sec", sentiment_backend="lexicon")

    result = pipeline.ingest_qualitative("AAPL")

    assert result["status"] == "error"
    assert result["ticker"] == "AAPL"
    assert "error" in result


# ---------------------------------------------------------------------------
# LLM-based guidance-snippet refinement (SEC narrative path only)
# ---------------------------------------------------------------------------
_GUIDANCE_FULL_TEXT = (
    "Apple today announced financial results. "
    "We expect continued strong growth in fiscal 2026. "
    "The Board of Directors declared a dividend."
)


class _StubSecNarrativeClientForGuidance:
    def __init__(self, user_agent: str, cache_dir: Path | None = None) -> None:
        self.user_agent = user_agent
        self.cache_dir = cache_dir

    def narrative_docs(self, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        return [{
            "title": f"{ticker} 8-K press release", "date": "2026-04-30", "type": "8-K",
            "company": ticker, "ticker": ticker, "sentiment": None, "source": "sec-filings",
            "doc_id": "0000320193-26-000011", "snippet": "heuristic fallback snippet",
        }]

    def full_texts(self, ticker: str, *, limit: int = 3) -> list[dict[str, Any]]:
        return [{"title": f"{ticker} 8-K", "date": "2026-04-30", "type": "8-K", "text": _GUIDANCE_FULL_TEXT}]


class _FakeAIMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChatModel:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.prompts: list[str] = []

    def invoke(self, prompt: Any, *args: Any, **kwargs: Any) -> _FakeAIMessage:
        self.prompts.append(prompt)
        return _FakeAIMessage(self._reply)


def test_llm_guidance_extraction_replaces_snippet_and_tags_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.llm as llm_mod
    import calorch.sec_narrative as sec_narrative_mod

    monkeypatch.setattr(sec_narrative_mod, "SecNarrativeClient", _StubSecNarrativeClientForGuidance)
    verbatim_reply = "We expect continued strong growth in fiscal 2026."
    fake_model = _FakeChatModel(verbatim_reply)
    monkeypatch.setattr(llm_mod, "get_chat_model", lambda settings: fake_model)

    pipeline = _pipeline(tmp_path, narrative_backend="sec", sentiment_backend="lexicon", guidance_extractor="llm")

    result = pipeline.ingest_qualitative("AAPL")

    assert result["status"] == "ok"
    narrative = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/narrative/AAPL/20260711.json")
    assert narrative[0]["snippet"] == verbatim_reply
    assert narrative[0]["snippet_source"] == "llm"
    assert fake_model.prompts  # the chat model was actually invoked


def test_llm_guidance_extraction_guard_failure_keeps_heuristic_snippet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.llm as llm_mod
    import calorch.sec_narrative as sec_narrative_mod

    monkeypatch.setattr(sec_narrative_mod, "SecNarrativeClient", _StubSecNarrativeClientForGuidance)
    hallucinated_reply = "Totally fabricated commentary that appears nowhere in the filing."
    fake_model = _FakeChatModel(hallucinated_reply)
    monkeypatch.setattr(llm_mod, "get_chat_model", lambda settings: fake_model)

    pipeline = _pipeline(tmp_path, narrative_backend="sec", sentiment_backend="lexicon", guidance_extractor="llm")

    result = pipeline.ingest_qualitative("AAPL")

    assert result["status"] == "ok"
    narrative = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/narrative/AAPL/20260711.json")
    assert narrative[0]["snippet"] == "heuristic fallback snippet"
    assert narrative[0]["snippet_source"] == "heuristic"


def test_heuristic_extractor_never_builds_chat_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import calorch.llm as llm_mod
    import calorch.sec_narrative as sec_narrative_mod

    monkeypatch.setattr(sec_narrative_mod, "SecNarrativeClient", _StubSecNarrativeClientForGuidance)

    def _boom(settings: Any) -> Any:
        raise AssertionError("get_chat_model should not be called when guidance_extractor=heuristic")

    monkeypatch.setattr(llm_mod, "get_chat_model", _boom)

    pipeline = _pipeline(
        tmp_path, narrative_backend="sec", sentiment_backend="lexicon", guidance_extractor="heuristic"
    )

    result = pipeline.ingest_qualitative("AAPL")

    assert result["status"] == "ok"
    narrative = pipeline._blob.download_json(pipeline._blob.input_container, "inputs/narrative/AAPL/20260711.json")
    assert narrative[0]["snippet"] == "heuristic fallback snippet"
    assert narrative[0]["snippet_source"] == "heuristic"
