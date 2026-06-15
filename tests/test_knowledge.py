"""Tests for the institutional-knowledge RAG layer (calorch.knowledge)."""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from calorch.knowledge import (
    KnowledgePassage,
    NullKnowledgeStore,
    RagChatModel,
    _flatten_sections,
    make_knowledge_store,
    maybe_wrap_for_rag,
)


class _FakeLLM:
    """Captures the messages it was invoked with."""

    def __init__(self) -> None:
        self.seen: list = []

    def invoke(self, messages, **kwargs):
        self.seen = messages
        return "ok"

    def with_structured_output(self, *a, **k):
        return "structured"


class _FakeRetriever:
    def __init__(self, passages):
        self._passages = passages
        self.queries: list = []

    def search(self, query, *, top_k=4, ticker=None):
        self.queries.append((query, ticker))
        return self._passages


# ---------------------------------------------------------------------------
# Null store + factory
# ---------------------------------------------------------------------------
def test_null_store_is_noop():
    store = NullKnowledgeStore()
    assert store.search("anything") == []
    assert store.index_analysis({"event_id": "x"}, run_id="r") is None


def test_make_store_returns_null_when_unconfigured():
    class S:
        search_endpoint = None
        search_index = "calorch-knowledge"
        use_mocks = False

    assert isinstance(make_knowledge_store(S()), NullKnowledgeStore)


def test_make_store_null_under_mocks_even_if_configured():
    class S:
        search_endpoint = "https://x.search.windows.net"
        search_index = "calorch-knowledge"
        search_api_key = "k"
        search_semantic_config = None
        use_mocks = True

    assert isinstance(make_knowledge_store(S()), NullKnowledgeStore)


# ---------------------------------------------------------------------------
# RAG wrapper
# ---------------------------------------------------------------------------
def test_maybe_wrap_passthrough_for_null_store():
    llm = _FakeLLM()
    assert maybe_wrap_for_rag(llm, NullKnowledgeStore()) is llm
    assert maybe_wrap_for_rag(llm, None) is llm


def test_rag_wrapper_augments_last_human_message():
    llm = _FakeLLM()
    retriever = _FakeRetriever(
        [KnowledgePassage(text="AAPL guided gross margin to 46%", source="AAPL earnings_call (ev-9)", score=2.0)]
    )
    model = RagChatModel(llm, retriever, top_k=3)

    msgs = [SystemMessage(content="sys"), HumanMessage(content="Ticker: AAPL\nTask: Generate guidance.")]
    out = model.invoke(msgs, max_tokens=100)

    assert out == "ok"
    # retrieval used the human prompt as query and extracted the ticker
    assert retriever.queries == [("Ticker: AAPL\nTask: Generate guidance.", "AAPL")]
    # the human message now carries the retrieved passage; system msg untouched
    human = llm.seen[-1]
    assert "<<<DATA" in human.content and "DATA>>>" in human.content  # fenced as untrusted data
    assert "do not contradict" not in human.content                   # SEC-5: phrasing removed
    assert "gross margin to 46%" in human.content
    assert llm.seen[0].content == "sys"


def test_rag_wrapper_caches_retrieval_per_event():
    """Repeated sections for the same ticker/prompt reuse one search (REL-4)."""
    llm = _FakeLLM()
    retriever = _FakeRetriever(
        [KnowledgePassage(text="cached passage", source="AAPL (ev-1)", score=1.0)]
    )
    model = RagChatModel(llm, retriever, top_k=3)
    msg = [HumanMessage(content="Ticker: AAPL\nTask: Generate guidance.")]
    model.invoke(list(msg))
    model.invoke(list(msg))
    assert len(retriever.queries) == 1  # second call served from cache


def test_rag_wrapper_no_passages_leaves_messages_unchanged():
    llm = _FakeLLM()
    model = RagChatModel(llm, _FakeRetriever([]), top_k=3)
    msgs = [HumanMessage(content="Ticker: MSFT\nTask: x")]
    model.invoke(msgs)
    assert llm.seen[0].content == "Ticker: MSFT\nTask: x"


def test_rag_wrapper_retrieval_failure_is_swallowed():
    class Boom:
        def search(self, *a, **k):
            raise RuntimeError("search down")

    llm = _FakeLLM()
    model = RagChatModel(llm, Boom(), top_k=3)
    # must not raise; falls back to the original prompt
    assert model.invoke([HumanMessage(content="Ticker: NVDA")]) == "ok"


def test_rag_wrapper_delegates_unknown_attrs():
    model = RagChatModel(_FakeLLM(), _FakeRetriever([]))
    assert model.with_structured_output(object) == "structured"


# ---------------------------------------------------------------------------
# Section flattening (indexing helper)
# ---------------------------------------------------------------------------
def test_flatten_sections_handles_tuples_and_dicts():
    tuples = [("Headline", ["a", "b"]), ("Risks", ["c"])]
    assert _flatten_sections(tuples) == "Headline\na b\nRisks\nc"

    dicts = [{"heading": "H", "body": "x y"}]
    assert _flatten_sections(dicts) == "H\nx y"

    assert _flatten_sections(None) == ""
