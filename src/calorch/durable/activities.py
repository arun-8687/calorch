"""ADF activity functions — invoke LangGraph agents inside durable activities.

Each activity:
  1. Sets up runtime Context (Graph, OneDrive, LLM, etc.)
  2. Calls the appropriate LangGraph node or agent subgraph
  3. Returns serialized results
"""
from __future__ import annotations

import logging
from typing import Any

import azure.durable_functions as df

from calorch.nodes import Context, _ctx, set_context
from calorch.state import CalendarEvent, ClassificationResult, EVENT_TYPE_TO_AGENT

log = logging.getLogger("calorch.durable.activities")

# Blueprint instance
bp = df.Blueprint()


def _ensure_context() -> None:
    """Ensure module-level Context is set for this activity process."""
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
    _ensure_context()
    c = _ctx()
    from datetime import datetime
    start = datetime.fromisoformat(input["window_start"].replace("Z", "+00:00")) if isinstance(input["window_start"], str) else input["window_start"]
    end = datetime.fromisoformat(input["window_end"].replace("Z", "+00:00")) if isinstance(input["window_end"], str) else input["window_end"]
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
    _ensure_context()
    from calorch.nodes import prefilter_keywords, llm_classify
    from datetime import datetime
    from calorch.state import CalendarEvent

    events = []
    for e in input.get("events", []):
        if isinstance(e, dict):
            for key in ["start", "end"]:
                if isinstance(e.get(key), str):
                    e[key] = datetime.fromisoformat(e[key].replace("Z", "+00:00"))
            events.append(CalendarEvent.model_validate(e))

    state = {"events": events, "raw_events": input.get("raw_events", []), "run_id": input.get("run_id", "")}
    p1 = prefilter_keywords(state)
    state["classifications"] = p1.get("classifications", {})
    p2 = llm_classify(state)
    return {
        "classifications": {k: v.model_dump(mode="json") for k, v in p2.get("classifications", {}).items()},
        "log": p1.get("log", []) + p2.get("log", []),
    }


# ---------------------------------------------------------------------------
# Activity 3: agent — invokes compiled LangGraph subgraph
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_agent(input: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    from calorch.agents import make_agent_subgraph
    from datetime import datetime
    from calorch.state import CalendarEvent, ClassificationResult

    ev = input["event"]
    if isinstance(ev, dict):
        for key in ["start", "end"]:
            if isinstance(ev.get(key), str):
                ev[key] = datetime.fromisoformat(ev[key].replace("Z", "+00:00"))
        event = CalendarEvent.model_validate(ev)
    else:
        event = ev

    cls_data = input["classification"]
    if isinstance(cls_data, dict):
        cls = ClassificationResult.model_validate(cls_data)
    else:
        cls = cls_data

    subgraph = make_agent_subgraph(cls.final_label)
    result = subgraph.invoke({
        "event": event.model_dump(mode="json"),
        "classification": cls.model_dump(mode="json"),
        "run_id": input.get("run_id", ""),
    })

    return {
        "documents": {k: v.model_dump(mode="json") if hasattr(v, "model_dump") else v for k, v in result.get("documents", {}).items()},
        "prepared_emails": {k: v.model_dump(mode="json") if hasattr(v, "model_dump") else v for k, v in result.get("prepared_emails", {}).items()},
        "calendar_links": result.get("calendar_links", {}),
        "errors": result.get("errors", []),
        "log": result.get("log", []),
    }


# ---------------------------------------------------------------------------
# Activity 4: deliver event
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_deliver(input: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    from calorch.nodes import deliver_event
    result = deliver_event({
        "event": input["event"],
        "classification": input["classification"],
        "preview": input["preview"],
        "document": input.get("document"),
        "onedrive_url": input.get("onedrive_url"),
        "run_id": input.get("run_id", ""),
        "send_emails": input.get("send_emails", False),
    })
    return {
        "emails": {k: v.model_dump(mode="json") if hasattr(v, "model_dump") else v for k, v in result.get("emails", {}).items()},
        "followups": [f.model_dump(mode="json") if hasattr(f, "model_dump") else f for f in result.get("followups", [])],
        "errors": result.get("errors", []),
        "log": result.get("log", []),
    }


# ---------------------------------------------------------------------------
# Activity 5: aggregate briefing
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
def activity_aggregate_briefing(input: dict[str, Any]) -> dict[str, Any]:
    _ensure_context()
    from calorch.nodes import aggregate_briefing
    from datetime import datetime
    ws = datetime.fromisoformat(input["window_start"].replace("Z", "+00:00")) if isinstance(input["window_start"], str) else input["window_start"]
    we = datetime.fromisoformat(input["window_end"].replace("Z", "+00:00")) if isinstance(input["window_end"], str) else input["window_end"]
    state = {
        "window_start": ws, "window_end": we,
        "run_id": input.get("run_id", ""),
        "events": [], "classifications": {}, "emails": {},
        "errors": input.get("errors", []), "followups": input.get("followups", []),
    }
    result = aggregate_briefing(state)
    return {
        "weekly_briefing": result.get("weekly_briefing", {}).model_dump(mode="json") if hasattr(result.get("weekly_briefing"), "model_dump") else result.get("weekly_briefing", {}),
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
