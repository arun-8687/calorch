"""Agent registry and default subgraph factory.

An *agent* is the unit of per-event-type behaviour: a compiled LangGraph
subgraph that receives one classified event and returns its artifacts
(documents, prepared emails, links, errors, log). Each agent is described
by an :class:`AgentSpec` and registered here — usually at import time by
a module in ``calorch.agents.builtin``.

The rest of the system never hard-codes event types. The parent graph,
the ``Send`` fan-out, the keyword pre-filter, and the analysis dispatch
all consult this registry, so adding an agent requires no changes to the
orchestrator, the durable activities, or the classification nodes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional
from collections.abc import Callable, Iterator

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from calorch.nodes import _ctx, _prepare_event_inner, _safe_artifact_name
from calorch.state import (
    AgentInput,
    AgentOutput,
    AgentState,
    CalendarEvent,
    ClassificationResult,
    EventType,
)
from calorch.telemetry import start_span

log = logging.getLogger("calorch.agents")


# ---------------------------------------------------------------------------
# Agent specification
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AgentSpec:
    """Everything the system needs to know about one event-type agent.

    Attributes:
      event_type:       the EventType this agent handles.
      analysis_builder: ``(event, cls, enterprise_data, llm_call, *,
                        providers, cik_lookup) -> EventAnalysis`` — the
                        per-type analysis logic.
      keywords:         pass-1 classification keywords for this type.
      node_name:        the node name in the parent graph (defaults to
                        ``agent_{event_type.value}``).
      graph_factory:    optional ``(spec) -> CompiledStateGraph`` override
                        for agents that need a custom subgraph shape
                        (multiple nodes, tools, inner loops). When omitted
                        the default single-node prepare pipeline is used.
    """

    event_type: EventType
    analysis_builder: Callable[..., Any]
    keywords: tuple[str, ...] = ()
    node_name: str = ""
    graph_factory: Callable[[AgentSpec], Any] | None = None

    def __post_init__(self) -> None:
        if not self.node_name:
            object.__setattr__(self, "node_name", f"agent_{self.event_type.value}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_REGISTRY: dict[EventType, AgentSpec] = {}


def register(spec: AgentSpec, *, replace: bool = False) -> AgentSpec:
    """Register an agent for its event type.

    Pass ``replace=True`` to intentionally override a built-in agent
    (e.g. a custom earnings-call agent in a deployment).
    """
    existing = _REGISTRY.get(spec.event_type)
    if existing is not None and not replace:
        raise ValueError(
            f"agent for {spec.event_type.value!r} already registered "
            f"(node {existing.node_name!r}); pass replace=True to override"
        )
    _REGISTRY[spec.event_type] = spec
    log.debug("registered agent %s for %s", spec.node_name, spec.event_type.value)
    return spec


def unregister(event_type: EventType) -> None:
    """Remove an agent registration (mainly for tests)."""
    _REGISTRY.pop(event_type, None)


def get_agent(event_type: EventType) -> AgentSpec:
    """Look up the agent for an event type, falling back to UNKNOWN.

    The fallback keeps the pipeline resilient when the classifier emits a
    type that has no dedicated agent (yet).
    """
    spec = _REGISTRY.get(event_type)
    if spec is not None:
        return spec
    fallback = _REGISTRY.get(EventType.UNKNOWN)
    if fallback is None:
        raise KeyError(
            f"no agent registered for {event_type.value!r} and no UNKNOWN fallback; "
            "is calorch.agents.builtin imported?"
        )
    log.warning("no agent for %s — falling back to %s", event_type.value, fallback.node_name)
    return fallback


def iter_agents() -> Iterator[AgentSpec]:
    """All registered agents, in registration order."""
    return iter(list(_REGISTRY.values()))


def agent_node_names() -> dict[EventType, str]:
    """EventType → parent-graph node name, for graph wiring and routing."""
    return {t: s.node_name for t, s in _REGISTRY.items()}


def classification_keywords() -> dict[EventType, tuple[str, ...]]:
    """EventType → pass-1 keywords, consumed by prefilter_keywords."""
    return {t: s.keywords for t, s in _REGISTRY.items() if s.keywords}


# ---------------------------------------------------------------------------
# Default subgraph factory (single prepare node)
# ---------------------------------------------------------------------------
def _make_prepare_node(spec: AgentSpec) -> Callable[..., dict[str, Any]]:
    def _prepare_agent(
        state: AgentState, config: Optional[RunnableConfig] = None
    ) -> dict[str, Any]:
        c = _ctx(config)
        ev = CalendarEvent.model_validate(state["event"])
        cls = ClassificationResult.model_validate(state["classification"])
        run_name = _safe_artifact_name(str(state.get("run_id", "run")))
        event_name = _safe_artifact_name(ev.id)

        with start_span(
            f"calorch.agent.{spec.event_type.value}",
            event_id=ev.id,
            event_type=cls.final_label.value,
            confidence=cls.confidence,
        ) as span:
            return _prepare_event_inner(c, ev, cls, run_name, event_name, span)

    return _prepare_agent


def default_graph_factory(spec: AgentSpec) -> Any:
    """Default agent shape: one prepare node running the full pipeline
    (data fetch → analysis via spec.analysis_builder → DOCX/HTML render).

    The subgraph exposes AgentInput as the entry schema (what Send()
    passes) and AgentOutput as the exit schema (what the parent merges).
    """
    builder = StateGraph(AgentState, input_schema=AgentInput, output_schema=AgentOutput)
    builder.add_node("prepare", _make_prepare_node(spec))
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", END)
    return builder.compile(name=spec.node_name)


def make_agent_subgraph(event_type: EventType) -> Any:
    """Build the compiled LangGraph subgraph for one event type."""
    spec = get_agent(event_type)
    factory = spec.graph_factory or default_graph_factory
    return factory(spec)
