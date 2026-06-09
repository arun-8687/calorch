"""ADF activity functions — invoke LangGraph nodes/agents inside durable activities.

Each activity:
  1. Ensures the runtime ``Context`` (Graph, OneDrive, LLM, …) is set up —
     activities may run in a different process than the orchestrator.
  2. Deserializes its input (ISO-8601 strings → datetimes, dicts → models).
  3. Calls the appropriate LangGraph node or compiled agent subgraph.
  4. Returns a JSON-serializable result for ADF persistence.
"""
from __future__ import annotations

import logging
from typing import Any

import azure.durable_functions as df

from calorch.durable.state import parse_classification, parse_event, parse_iso, serialize_state
from calorch.nodes import _ctx, set_context

log = logging.getLogger("calorch.durable.activities")

bp = df.Blueprint()


def _ensure_context() -> None:
    """Ensure the module-level Context is set for this worker process."""
    try:
        _ctx()
    except Exception:
        from calorch.durable.app import _build_context

        set_context(_build_context())


# ---------------------------------------------------------------------------
# Activity 1: scan calendar
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_scan_calendar(input: dict[str, Any]) -> dict[str, Any]:
    """Pull events from Microsoft Graph for the requested window."""
    _ensure_context()
    c = _ctx()
    start = parse_iso(input["window_start"])
    end = parse_iso(input["window_end"])

    raw = c.graph.list_events(start, end)
    from calorch.tools import to_calendar_event

    events = [to_calendar_event(r) for r in raw]
    return {
        "events": [e.model_dump(mode="json") for e in events],
        "raw_events": serialize_state(raw),
        "log": [f"scan_calendar: {len(events)} events"],
    }


# ---------------------------------------------------------------------------
# Activity 2: classify events
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_classify(input: dict[str, Any]) -> dict[str, Any]:
    """Two-pass classification (keywords/SEC form + LLM) for all events."""
    _ensure_context()
    from calorch.nodes import llm_classify, prefilter_keywords

    events = [parse_event(e) for e in input.get("events", [])]
    state = {
        "events": events,
        "raw_events": input.get("raw_events", []),
        "run_id": input.get("run_id", ""),
    }
    p1 = prefilter_keywords(state)
    state["classifications"] = p1.get("classifications", {})
    p2 = llm_classify(state)
    return {
        "classifications": serialize_state(p2.get("classifications", {})),
        "log": p1.get("log", []) + p2.get("log", []),
    }


# ---------------------------------------------------------------------------
# Activity 3: agent — invokes a compiled LangGraph subgraph
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_agent(input: dict[str, Any]) -> dict[str, Any]:
    """Run the LangGraph agent subgraph for a single classified event.

    The subgraph (selected by event type) runs the full preparation
    pipeline: data fetch → analysis → DOCX/HTML render.
    """
    _ensure_context()
    from calorch.agents import make_agent_subgraph

    event = parse_event(input["event"])
    cls = parse_classification(input["classification"])

    subgraph = make_agent_subgraph(cls.final_label)
    result = subgraph.invoke(
        {
            "event": event.model_dump(mode="json"),
            "classification": cls.model_dump(mode="json"),
            "run_id": input.get("run_id", ""),
        }
    )

    return {
        "documents": serialize_state(result.get("documents", {})),
        "prepared_emails": serialize_state(result.get("prepared_emails", {})),
        "calendar_links": result.get("calendar_links", {}),
        "errors": result.get("errors", []),
        "log": result.get("log", []),
    }


# ---------------------------------------------------------------------------
# Activity 4: deliver event
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_deliver(input: dict[str, Any]) -> dict[str, Any]:
    """Deliver an approved event (create draft / send email, idempotent)."""
    _ensure_context()
    from calorch.nodes import deliver_event

    result = deliver_event(
        {
            "event": input["event"],
            "classification": input["classification"],
            "preview": input["preview"],
            "document": input.get("document"),
            "onedrive_url": input.get("onedrive_url"),
            "run_id": input.get("run_id", ""),
            "send_emails": input.get("send_emails", False),
        }
    )
    return {
        "emails": serialize_state(result.get("emails", {})),
        "followups": serialize_state(result.get("followups", [])),
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
    from calorch.state import EmailArtifact, FollowUpItem

    state = {
        "window_start": parse_iso(input["window_start"]),
        "window_end": parse_iso(input["window_end"]),
        "run_id": input.get("run_id", ""),
        "events": [parse_event(e) for e in input.get("events", [])],
        "classifications": {
            k: parse_classification(v) for k, v in input.get("classifications", {}).items()
        },
        "emails": {
            k: EmailArtifact.model_validate(v) for k, v in input.get("emails", {}).items()
        },
        "errors": input.get("errors", []),
        "followups": [FollowUpItem.model_validate(f) for f in input.get("followups", [])],
    }
    result = aggregate_briefing(state)
    return {
        "weekly_briefing": serialize_state(result.get("weekly_briefing", {})),
        "log": result.get("log", []),
    }


activity_register = [
    activity_scan_calendar,
    activity_classify,
    activity_agent,
    activity_deliver,
    activity_aggregate_briefing,
]


def get_blueprint() -> df.Blueprint:
    return bp
