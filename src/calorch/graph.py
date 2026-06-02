"""StateGraph assembly — the orchestrator's main wiring.

Shape of the graph (Option C, per the ADR):

    START
      → scan_calendar
      → prefilter_keywords                (Pass 1)
      → llm_classify                      (Pass 2, GPT-4o Structured Output)
      → [fan-out] prepare_event × N        (parallel via Send)
      → approval_gate                      (interrupt() before external email)
      → [fan-out] deliver_event × N        (idempotent external side effects)
      → aggregate_briefing
      → END
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from calorch.nodes import (
    aggregate_briefing,
    approval_gate,
    deliver_event,
    fan_out_delivery,
    fan_out_prepare_events,
    llm_classify,
    prepare_event,
    prefilter_keywords,
    scan_calendar,
)
from calorch.state import OrchestratorState

log = logging.getLogger("calorch.graph")


def make_graph(
    *,
    checkpointer: Any | None = None,
):
    """Build and compile the orchestrator graph.

    Args:
      checkpointer: pass a ``MemorySaver`` (or any BaseCheckpointSaver) to
        enable resumption across interrupts.
    """
    builder = StateGraph(OrchestratorState)

    builder.add_node("scan_calendar", scan_calendar, retry_policy=RetryPolicy(max_attempts=3))
    builder.add_node("prefilter_keywords", prefilter_keywords)
    builder.add_node("llm_classify", llm_classify)
    builder.add_node("prepare_event", prepare_event)
    builder.add_node("approval_gate", approval_gate)
    builder.add_node("deliver_event", deliver_event)
    builder.add_node("aggregate_briefing", aggregate_briefing)

    # Linear front of the pipeline
    builder.add_edge(START, "scan_calendar")
    builder.add_edge("scan_calendar", "prefilter_keywords")
    builder.add_edge("prefilter_keywords", "llm_classify")

    # Conditional fan-out: dispatch one Send per classified event
    builder.add_conditional_edges(
        "llm_classify",
        fan_out_prepare_events,
        path_map=["prepare_event", "approval_gate"],
    )

    # Prepared previews re-join at a side-effect-free human approval gate.
    builder.add_edge("prepare_event", "approval_gate")
    builder.add_conditional_edges(
        "approval_gate",
        fan_out_delivery,
        path_map=["deliver_event", "aggregate_briefing"],
    )

    # All approved delivery workers re-join at the briefing aggregator.
    builder.add_edge("deliver_event", "aggregate_briefing")
    builder.add_edge("aggregate_briefing", END)

    return builder.compile(
        checkpointer=checkpointer or MemorySaver(),
        name="calorch-orchestrator",
    )


# ---------------------------------------------------------------------------
# Default entry-point for `langgraph dev`
# ---------------------------------------------------------------------------
def get_graph():
    """A bare graph used by ``langgraph dev`` / ``langgraph up``.

    Runtime clients (Graph, OneDrive, repo, LLM) are not bound here; the
    consumer must set them via ``calorch.nodes.set_context`` before invoking.
    """
    return make_graph()
