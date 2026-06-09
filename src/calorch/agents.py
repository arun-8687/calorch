"""Multi-agent subgraphs — one per event type.

Each agent is a compiled StateGraph that receives an event via Send(),
prepares the research brief (data fetch → analysis → render), and returns
per-event artifacts (documents, emails, links) that the parent graph merges.

Agent subgraph state:
  Input  → AgentInput   (event, classification, run_id)
  Output → AgentOutput  (documents, prepared_emails, calendar_links, errors, log)

Span naming: calorch.agent.{event_type} so telemetry distinguishes each agent.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

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
# Agent preparation node (shared across all event types)
# ---------------------------------------------------------------------------
def _prepare_agent(state: AgentState, config: Optional[RunnableConfig] = None) -> dict[str, Any]:
    """Run the full preparation pipeline for a single event.

    This is the single node inside every agent subgraph. It reads the
    event + classification from the subgraph state, fetches the runtime
    Context via _ctx(), and delegates to the existing _prepare_event_inner
    logic so the preparation behaviour is identical to the monolithic
    prepare_event node.
    """
    c = _ctx(config)
    ev = CalendarEvent.model_validate(state["event"])
    cls = ClassificationResult.model_validate(state["classification"])
    run_name = _safe_artifact_name(str(state.get("run_id", "run")))
    event_name = _safe_artifact_name(ev.id)

    with start_span(
        f"calorch.agent.{cls.final_label.value}",
        event_id=ev.id,
        event_type=cls.final_label.value,
        confidence=cls.confidence,
    ) as span:
        return _prepare_event_inner(c, ev, cls, run_name, event_name, span)


# ---------------------------------------------------------------------------
# Factory — one compiled subgraph per event type
# ---------------------------------------------------------------------------
def make_agent_subgraph(event_type: EventType) -> Any:
    """Build a compiled StateGraph for one event type.

    The subgraph uses AgentState internally but exposes AgentInput as the
    entry schema (what Send() passes) and AgentOutput as the exit schema
    (what gets merged back into the parent OrchestratorState).
    """
    builder = StateGraph(
        AgentState,
        input_schema=AgentInput,
        output_schema=AgentOutput,
    )
    builder.add_node("prepare", _prepare_agent)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", END)
    return builder.compile()
