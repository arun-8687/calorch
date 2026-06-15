"""ADF activity functions — invoke LangGraph nodes/agents inside durable activities.

Each activity:
  1. Ensures the runtime ``Context`` (Graph, OneDrive, LLM, …) is set up —
     activities may run in a different process than the orchestrator.
  2. Deserializes its input (ISO-8601 strings → datetimes, dicts → models).
  3. Calls the appropriate LangGraph node or compiled agent subgraph.
  4. Returns a JSON-serializable result for ADF persistence.
"""
from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

import azure.durable_functions as df

from calorch.durable.state import parse_classification, parse_event, parse_iso, serialize_state
from calorch.logging_config import clear_correlation, set_request_id, set_run_id
from calorch.nodes import _ctx, set_context

log = logging.getLogger("calorch.durable.activities")

bp = df.Blueprint()


def _correlated(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Stamp the activity's logs with the run_id so App Insights can group
    concurrent activities by run (OBS-5)."""

    @functools.wraps(fn)
    def wrapper(input: dict[str, Any]) -> dict[str, Any]:
        run_id = (input or {}).get("run_id", "") or ""
        set_run_id(run_id)
        set_request_id(run_id or None)
        try:
            return fn(input)
        finally:
            clear_correlation()

    return wrapper


def _ensure_context() -> None:
    """Ensure the module-level Context is set for this worker process."""
    try:
        _ctx()
    except Exception:
        from calorch.durable.app import _build_context

        set_context(_build_context())


def _rehydrate_email_html(preview: Any, run_id: str) -> Any:
    """Reconstruct the email body stripped by activity_agent (REL-1).

    The body is fetched from the local artifact (same-instance fast path),
    then from blob storage, by deterministic path. If neither is available
    the preview is returned unchanged (deliver_event records the failure).
    """
    if not isinstance(preview, dict) or preview.get("html"):
        return preview
    from pathlib import Path

    # a) local html_path (same worker instance that prepared it)
    path = preview.get("html_path")
    if path and Path(path).exists():
        try:
            preview["html"] = Path(path).read_text(encoding="utf-8")
            return preview
        except OSError as e:
            log.warning("local email body read failed for %s: %s", preview.get("event_id"), e)

    # b) blob storage, by deterministic output path
    c = _ctx()
    store = getattr(c, "blob_store", None)
    event_id = str(preview.get("event_id", ""))
    if store is not None and event_id:
        from calorch.blob_store import output_blob_path
        from calorch.nodes import _safe_artifact_name

        name = output_blob_path(
            _safe_artifact_name(str(run_id)), _safe_artifact_name(event_id),
            f"{_safe_artifact_name(event_id)}.html",
        )
        try:
            data = store.download_bytes(getattr(store, "output_container", "calorch-outputs"), name)
            if data:
                preview["html"] = data.decode("utf-8")
        except Exception as e:  # noqa: BLE001 - best-effort rehydration
            log.warning("blob email body fetch failed for %s: %s", event_id, e)
    return preview


# ---------------------------------------------------------------------------
# Activity 1: scan calendar
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
@_correlated
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
@_correlated
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
@_correlated
def activity_agent(input: dict[str, Any]) -> dict[str, Any]:
    """Run the LangGraph agent subgraph for a single classified event."""
    return _agent_impl(input)


def _agent_impl(input: dict[str, Any]) -> dict[str, Any]:
    """Body of activity_agent (plain function for unit tests).

    The subgraph (selected by event type) runs the full preparation
    pipeline: data fetch → analysis → DOCX/HTML render. Never raises: a
    single event's failure is captured in ``errors`` so the parent
    ``task_all`` does not abort the whole run.
    """
    _ensure_context()
    ev_id = (input.get("event") or {}).get("id", "?")
    try:
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
        prepared = serialize_state(result.get("prepared_emails", {}))
        # Drop the full HTML body from the value carried through orchestration
        # history / queue messages (can be large × many events). It is
        # rehydrated from local disk or blob storage in activity_deliver.
        for v in prepared.values():
            if isinstance(v, dict):
                v["html"] = ""
        return {
            "documents": serialize_state(result.get("documents", {})),
            "prepared_emails": prepared,
            "calendar_links": result.get("calendar_links", {}),
            "errors": result.get("errors", []),
            "log": result.get("log", []),
        }
    except Exception as e:  # noqa: BLE001 - per-event degradation, never kill the run
        log.exception("agent activity failed for %s", ev_id)
        return {
            "documents": {}, "prepared_emails": {}, "calendar_links": {},
            "errors": [f"agent:{ev_id}:{e!r}"], "log": [],
        }


# ---------------------------------------------------------------------------
# Activity 4: deliver event
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
@_correlated
def activity_deliver(input: dict[str, Any]) -> dict[str, Any]:
    """Deliver an approved event (create draft / send email, idempotent)."""
    return _deliver_impl(input)


def _deliver_impl(input: dict[str, Any]) -> dict[str, Any]:
    """Body of activity_deliver (plain function for unit tests).

    Never raises: a single delivery failure is captured in ``errors`` so the
    parent ``task_all`` does not abort the whole run.
    """
    _ensure_context()
    ev_id = (input.get("event") or {}).get("id", "?")
    try:
        from calorch.nodes import deliver_event

        preview = _rehydrate_email_html(input.get("preview"), input.get("run_id", ""))
        result = deliver_event(
            {
                "event": input["event"],
                "classification": input["classification"],
                "preview": preview,
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
    except Exception as e:  # noqa: BLE001 - per-event degradation, never kill the run
        log.exception("deliver activity failed for %s", ev_id)
        return {"emails": {}, "followups": [], "errors": [f"deliver:{ev_id}:{e!r}"], "log": []}


# ---------------------------------------------------------------------------
# Activity 5: aggregate briefing
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
@_correlated
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
    # One per-run summary line of outbound HTTP client metrics (OBS-3).
    try:
        from calorch.http_client import get_metrics

        log.info("http client metrics: %s", get_metrics())
    except Exception as e:  # noqa: BLE001 - metrics are best-effort
        log.debug("http metrics unavailable: %s", e)
    return {
        "weekly_briefing": serialize_state(result.get("weekly_briefing", {})),
        "log": result.get("log", []),
    }


# ---------------------------------------------------------------------------
# Activity 6: request approval — notify approvers that a run is paused
# ---------------------------------------------------------------------------
@bp.activity_trigger(input_name="input")
@_correlated
def activity_request_approval(input: dict[str, Any]) -> dict[str, Any]:
    """Email the approvers a run summary + tokenized review-page link."""
    return _request_approval_impl(input)


def _request_approval_impl(input: dict[str, Any]) -> dict[str, Any]:
    """Body of activity_request_approval (plain function for unit tests).

    Never raises: a notification failure must not fail the run — the gate
    still works via logs and the key-protected approval API.
    """
    import os

    from calorch.config import get_settings
    from calorch.durable.approval import build_approval_email

    try:
        _ensure_context()
        s = get_settings()
        if not s.approver_emails:
            return {"notified": [], "log": ["approval notify skipped: APPROVER_EMAILS not set"]}

        base = s.approval_base_url or (
            f"https://{os.environ['WEBSITE_HOSTNAME']}"
            if os.getenv("WEBSITE_HOSTNAME")
            else "http://localhost:7071"
        )
        instance_id = input["instance_id"]
        review_url = f"{base.rstrip('/')}/api/review/{instance_id}?token={input['token']}"
        subject, html_body = build_approval_email(
            run_id=input.get("run_id", ""),
            prepared=input.get("prepared", []),
            review_url=review_url,
            timeout_hours=float(input.get("timeout_hours", 24)),
        )
        c = _ctx()
        c.graph.send_mail(to=s.approver_emails, subject=subject, html=html_body, attachment_b64=None)
        return {
            "notified": list(s.approver_emails),
            "log": [f"approval request sent to {len(s.approver_emails)} approver(s)"],
        }
    except Exception as e:  # noqa: BLE001 - notification is best-effort
        log.exception("approval notification failed for %s", input.get("run_id", "?"))
        return {"notified": [], "errors": [f"approval_notify:{e!r}"], "log": []}


activity_register = [
    activity_scan_calendar,
    activity_classify,
    activity_agent,
    activity_deliver,
    activity_aggregate_briefing,
    activity_request_approval,
]


def get_blueprint() -> df.Blueprint:
    return bp
