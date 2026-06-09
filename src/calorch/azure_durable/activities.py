"""ADF activity functions — thin wrappers around calorch nodes.

Each activity:
  1. Deserializes input from the ADF orchestrator
  2. Sets up the runtime ``Context`` (Graph, OneDrive, etc.)
  3. Calls the appropriate LangGraph node or agent subgraph
  4. Serializes the result for ADF persistence

The agent activity is the most important: it invokes a compiled
LangGraph ``StateGraph`` subgraph for the event type.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import azure.durable_functions as df
import azure.functions as func

from calorch.nodes import Context, _ctx, set_context
from calorch.state import (
    CalendarEvent,
    ClassificationResult,
    EVENT_TYPE_TO_AGENT,
    EventType,
    OrchestratorState,
)

log = logging.getLogger("calorch.azure_durable.activities")

# Create a Blueprint instance for decorators
bp = df.Blueprint()


# ---------------------------------------------------------------------------
# Helper: ensure Context is set
# ---------------------------------------------------------------------------
def _ensure_context() -> None:
    """Initialise the module-level Context if not already set.

    In ADF, the orchestrator and activities run in separate process
    instances, so each activity must set up its own Context.
    """
    try:
        _ctx()
    except Exception:
        from calorch.azure_durable.app import _build_context
        ctx = _build_context()
        set_context(ctx)


# ---------------------------------------------------------------------------
# Activity 1: scan calendar
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_scan_calendar(input: dict[str, Any]) -> dict[str, Any]:
    """Pull events from Microsoft Graph for the requested window."""
    _ensure_context()
    c = _ctx()

    start = input["window_start"]
    end = input["window_end"]
    run_id = input["run_id"]

    # Convert ISO strings to datetime
    from datetime import datetime
    if isinstance(start, str):
        start = datetime.fromisoformat(start.replace("Z", "+00:00"))
    if isinstance(end, str):
        end = datetime.fromisoformat(end.replace("Z", "+00:00"))

    result = c.graph.list_events(start, end)
    from calorch.tools import to_calendar_event
    events = [to_calendar_event(r) for r in result]

    return {
        "events": [e.model_dump(mode="json") for e in events],
        "raw_events": result,
        "log": [f"scan_calendar: {len(events)} events"],
    }


# ---------------------------------------------------------------------------
# Activity 2: classify events
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_classify(input: dict[str, Any]) -> dict[str, Any]:
    """Two-pass classification (keywords + LLM) for all events."""
    _ensure_context()
    from calorch.nodes import prefilter_keywords, llm_classify

    events = input.get("events", [])
    raw_events = input.get("raw_events", [])
    run_id = input.get("run_id", "")

    from datetime import datetime
    from calorch.state import CalendarEvent

    # Rehydrate CalendarEvent objects
    event_objects = []
    for e in events:
        if isinstance(e, dict):
            # Handle datetime strings
            for key in ["start", "end"]:
                if isinstance(e.get(key), str):
                    e[key] = datetime.fromisoformat(e[key].replace("Z", "+00:00"))
            event_objects.append(CalendarEvent.model_validate(e))

    state: OrchestratorState = {
        "events": event_objects,
        "raw_events": raw_events,
        "run_id": run_id,
    }

    # Pass 1: keywords
    p1 = prefilter_keywords(state)
    state["classifications"] = p1.get("classifications", {})

    # Pass 2: LLM
    p2 = llm_classify(state)
    state["classifications"] = p2.get("classifications", {})

    return {
        "classifications": {
            k: v.model_dump(mode="json") for k, v in state["classifications"].items()
        },
        "log": p1.get("log", []) + p2.get("log", []),
    }


# ---------------------------------------------------------------------------
# Activity 3: agent — invokes a compiled LangGraph subgraph
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_agent(input: dict[str, Any]) -> dict[str, Any]:
    """Invoke a compiled LangGraph agent subgraph for a single event.

    The agent subgraph is determined by the event type from the classification.
    The subgraph runs the full preparation pipeline (data fetch → analysis → render).
    """
    _ensure_context()
    from calorch.agents import make_agent_subgraph

    event_data = input["event"]
    classification_data = input["classification"]
    run_id = input.get("run_id", "")

    # Rehydrate
    from datetime import datetime
    from calorch.state import CalendarEvent, ClassificationResult

    if isinstance(event_data, dict):
        for key in ["start", "end"]:
            if isinstance(event_data.get(key), str):
                event_data[key] = datetime.fromisoformat(event_data[key].replace("Z", "+00:00"))
        event = CalendarEvent.model_validate(event_data)
    else:
        event = event_data

    if isinstance(classification_data, dict):
        cls = ClassificationResult.model_validate(classification_data)
    else:
        cls = classification_data

    # Get the agent subgraph for this event type
    agent_node = EVENT_TYPE_TO_AGENT[cls.final_label]
    subgraph = make_agent_subgraph(cls.final_label)

    # Build the subgraph input
    agent_input = {
        "event": event.model_dump(mode="json"),
        "classification": cls.model_dump(mode="json"),
        "run_id": run_id,
    }

    # Invoke the subgraph
    result = subgraph.invoke(agent_input)

    return {
        "documents": {
            k: v.model_dump(mode="json") if hasattr(v, "model_dump") else v
            for k, v in result.get("documents", {}).items()
        },
        "prepared_emails": {
            k: v.model_dump(mode="json") if hasattr(v, "model_dump") else v
            for k, v in result.get("prepared_emails", {}).items()
        },
        "calendar_links": result.get("calendar_links", {}),
        "errors": result.get("errors", []),
        "log": result.get("log", []),
    }


# ---------------------------------------------------------------------------
# Activity 4: deliver event
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_deliver(input: dict[str, Any]) -> dict[str, Any]:
    """Deliver an approved event (create draft / send email)."""
    _ensure_context()
    from calorch.nodes import deliver_event

    payload = {
        "event": input["event"],
        "classification": input["classification"],
        "preview": input["preview"],
        "document": input.get("document"),
        "onedrive_url": input.get("onedrive_url"),
        "run_id": input.get("run_id", ""),
        "send_emails": input.get("send_emails", False),
    }

    result = deliver_event(payload)
    return {
        "emails": {
            k: v.model_dump(mode="json") if hasattr(v, "model_dump") else v
            for k, v in result.get("emails", {}).items()
        },
        "followups": [
            f.model_dump(mode="json") if hasattr(f, "model_dump") else f
            for f in result.get("followups", [])
        ],
        "errors": result.get("errors", []),
        "log": result.get("log", []),
    }


# ---------------------------------------------------------------------------
# Activity 5: aggregate briefing
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_aggregate_briefing(input: dict[str, Any]) -> dict[str, Any]:
    """Cross-event aggregation — weekly briefing summary."""
    _ensure_context()
    from calorch.nodes import aggregate_briefing

    from datetime import datetime

    window_start = input["window_start"]
    window_end = input["window_end"]
    if isinstance(window_start, str):
        window_start = datetime.fromisoformat(window_start.replace("Z", "+00:00"))
    if isinstance(window_end, str):
        window_end = datetime.fromisoformat(window_end.replace("Z", "+00:00"))

    state: OrchestratorState = {
        "window_start": window_start,
        "window_end": window_end,
        "run_id": input.get("run_id", ""),
        "events": [],
        "classifications": {},
        "emails": {},
        "errors": input.get("errors", []),
        "followups": input.get("followups", []),
    }

    result = aggregate_briefing(state)
    return {
        "weekly_briefing": result.get("weekly_briefing", {}).model_dump(mode="json")
        if hasattr(result.get("weekly_briefing"), "model_dump")
        else result.get("weekly_briefing", {}),
        "log": result.get("log", []),
    }


# ---------------------------------------------------------------------------
# Activity register
# ---------------------------------------------------------------------------
activity_register = [
    activity_scan_calendar,
    activity_classify,
    activity_agent,
    activity_deliver,
    activity_aggregate_briefing,
]


def get_blueprint() -> df.Blueprint:
    """Return the Blueprint with all registered activities."""
    return bp
