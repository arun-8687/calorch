"""LLM client wrapper.

Supported providers (priority order):

1. **Opencode Go** — when ``OPENCODE_GO_API_KEY`` is set. Uses
   ``langchain_openai.ChatOpenAI`` with a custom ``base_url`` pointing to the
   OpenAI-compatible endpoint ``https://opencode.ai/zen/go/v1``.
2. **Azure OpenAI** — when ``AZURE_OPENAI_API_KEY`` + ``AZURE_OPENAI_ENDPOINT``
   are set.
3. **MockChatModel** — deterministic stand-in used when credentials are absent
   and ``USE_MOCKS=true`` (the default in the demo).

All three expose ``.invoke(messages)`` → ``AIMessage`` so the rest of the
orchestrator is provider-agnostic.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel

from calorch.config import Settings
from calorch.state import ClassificationResult, EventType, EVENT_TYPE_TO_NODE


# ---------------------------------------------------------------------------
# Mock model — used when Azure OpenAI credentials are missing.
# Implements a minimal subset of .invoke() and .with_structured_output().
# ---------------------------------------------------------------------------
class MockChatModel(BaseChatModel):
    """Deterministic stand-in for Azure OpenAI.

    Recognises a few keywords and routes to an EventType. Used only for
    local development; never used in production.
    """

    temperature: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "mock-chat-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = _heuristic_classify(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = _heuristic_classify(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def with_structured_output(self, schema: type[BaseModel] | dict, **_: Any):
        outer = self

        class _StructuredRunnable:
            def invoke(self, input_: Any, **kw: Any) -> BaseModel | dict:
                msgs = _flatten_input(input_)
                text = _heuristic_classify(msgs)
                return self._parse(text)

            async def ainvoke(self, input_: Any, **kw: Any) -> BaseModel | dict:
                msgs = _flatten_input(input_)
                text = _heuristic_classify(msgs)
                return self._parse(text)

            async def abatch(self, inputs: list[Any], **kw: Any) -> list[BaseModel | dict]:
                return [self.invoke(inp) for inp in inputs]

            def _parse(self, text: str) -> BaseModel | dict:
                if isinstance(schema, type) and issubclass(schema, BaseModel):
                    raw = _extract_json(text) or {}
                    try:
                        return schema.model_validate(raw)
                    except Exception:
                        defaults: dict[str, Any] = {
                            "event_id": "",
                            "final_label": "unknown",
                            "confidence": 0.0,
                            "rationale": text,
                        }
                        if schema is ClassificationResult:
                            defaults.setdefault("routed_node", "handle_unknown")
                        merged = {**defaults, **raw}
                        return schema.model_validate(merged)
                return _extract_json(text) or {}

        return _StructuredRunnable()


def _flatten_input(input_: Any) -> list[BaseMessage]:
    """Normalise .invoke() inputs (string | dict | list[BaseMessage])."""
    from langchain_core.messages import HumanMessage

    if isinstance(input_, str):
        return [HumanMessage(content=input_)]
    if isinstance(input_, dict):
        msgs: list[BaseMessage] = []
        for k in ("messages", "input"):
            v = input_.get(k)
            if isinstance(v, list):
                msgs.extend(v)
        if not msgs and "input" in input_:
            msgs.append(HumanMessage(content=str(input_["input"])))
        return msgs
    if isinstance(input_, Iterable):
        return list(input_)  # type: ignore[arg-type]
    return [HumanMessage(content=str(input_))]


_KEYWORD_MAP: list[tuple[EventType, tuple[str, ...]]] = [
    (EventType.EARNINGS_CALL, ("earnings", "q1", "q2", "q3", "q4", "guidance", "results")),
    (EventType.MANAGEMENT_MEETING, ("ceo", "cfo", "cro", "1on1", "1-on-1", "town hall")),
    (EventType.CONFERENCE, ("conference", "summit", "expo", "investor day", "capital markets day")),
    (EventType.KOL_MEETING, ("kol", "expert", "kolsight", "consultant call", "thought leader")),
    (EventType.CHANNEL_CHECK, ("channel", "survey", "distributor", "reseller", "channel partner")),
    (EventType.PORTFOLIO_MEETING, ("portfolio", "holdings", "ic ", "investment committee")),
    (EventType.INTERNAL_REVIEW, ("internal", "review", "retro", "postmortem", "sprint review")),
    (EventType.ANALYST_MEETING, ("analyst", "broker", "sell-side", "buy-side")),
]


def _heuristic_classify(messages: list[BaseMessage]) -> str:
    blob = " ".join((m.content or "") for m in messages if hasattr(m, "content")).lower()
    counts: dict[EventType, int] = {}
    for ev, kws in _KEYWORD_MAP:
        c = sum(blob.count(k) for k in kws)
        if c:
            counts[ev] = c
    if not counts:
        payload = {"final_label": "unknown", "confidence": 0.0, "rationale": "no keyword hits"}
    else:
        best, hits = max(counts.items(), key=lambda kv: kv[1])
        payload = {
            "final_label": best.value,
            "routed_node": EVENT_TYPE_TO_NODE[best],
            "confidence": min(0.99, 0.4 + 0.15 * hits),
            "rationale": f"keyword hits={dict(counts)}",
        }
    return json.dumps(payload)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_chat_model(settings: Settings) -> BaseChatModel:
    # 1. Opencode Go (explicit opt-in via API key)
    if settings.opencode_go_api_key:
        from langchain_openai import ChatOpenAI  # local import to avoid hard dep in demo

        return ChatOpenAI(
            model=settings.opencode_go_model,
            api_key=settings.opencode_go_api_key,
            base_url="https://opencode.ai/zen/go/v1",
            temperature=0.0,
        )

    # 2. Azure OpenAI
    if settings.azure_openai_api_key and settings.azure_openai_endpoint:
        from langchain_openai import AzureChatOpenAI  # local import to avoid hard dep in demo

        return AzureChatOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
            deployment_name=settings.azure_openai_deployment,
            temperature=0.0,
        )

    # 3. Deterministic mock for demo / local dev
    return MockChatModel()
