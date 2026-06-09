"""Azure Durable Functions orchestrator — calorch workflow.

ADF orchestrates the high-level flow:
  1. scan calendar
  2. classify events
  3. fan-out: each event → its LangGraph agent subgraph (parallel)
  4. approval gate (external event if required)
  5. fan-out: deliver each event (parallel)
  6. aggregate briefing

LangGraph handles the per-event preparation inside agent activities.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import azure.durable_functions as df
import azure.functions as func

from calorch.nodes import Context, _ctx, set_context
from calorch.state import CalendarEvent, ClassificationResult, OrchestratorState

# Create a Blueprint instance for decorators
bp = df.Blueprint()

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
@bp.orchestration_trigger(context_name="context")
def calorch_orchestrator(context: df.DurableOrchestrationContext) -> dict[str, Any]:
    """Main ADF orchestrator for the calorch workflow.

    Replaces the LangGraph ``StateGraph`` with ADF's native orchestration.
    Each step is an activity; the parallel fan-out uses ``context.task_all``.
    """
    input_data = context.get_input() or {}
    run_id = input_data.get("run_id", _new_run_id())
    send_emails = input_data.get("send_emails", False)
    require_approval = input_data.get("require_approval", True)
    window_start = input_data.get("window_start")
    window_end = input_data.get("window_end")

    # --- 1) scan calendar ---
    scan_result = yield context.call_activity(
        "activity_scan_calendar",
        {"window_start": window_start, "window_end": window_end, "run_id": run_id},
    )
    events = scan_result.get("events", [])
    raw_events = scan_result.get("raw_events", [])

    if not events:
        return {
            "run_id": run_id,
            "status": "completed",
            "event_count": 0,
            "message": "No events found in window",
        }

    # --- 2) classify events ---
    classify_result = yield context.call_activity(
        "activity_classify",
        {
            "events": events,
            "raw_events": raw_events,
            "run_id": run_id,
        },
    )
    classifications = classify_result.get("classifications", {})

    # --- 3) fan-out: prepare each event via its LangGraph agent ---
    agent_tasks = []
    for ev in events:
        cls = classifications.get(ev["id"], {})
        agent_tasks.append(
            context.call_activity(
                "activity_agent",
                {
                    "event": ev,
                    "classification": cls,
                    "run_id": run_id,
                },
            )
        )

    agent_results = yield context.task_all(agent_tasks)

    # Merge agent outputs into state
    documents: dict[str, Any] = {}
    prepared_emails: dict[str, Any] = {}
    calendar_links: dict[str, str] = {}
    errors: list[str] = []
    log_lines: list[str] = []

    for r in agent_results:
        documents.update(r.get("documents", {}))
        prepared_emails.update(r.get("prepared_emails", {}))
        calendar_links.update(r.get("calendar_links", {}))
        errors.extend(r.get("errors", []))
        log_lines.extend(r.get("log", []))

    # --- 4) approval gate ---
    delivery_approved = True
    approval_status = "not_required"
    if send_emails and require_approval and prepared_emails:
        # Wait for external approval event
        approval_event = yield context.wait_for_external_event(
            "approval",
            timeout=60 * 60 * 24,  # 24 hours
        )
        approved = bool(
            approval_event.get("approved") if isinstance(approval_event, dict) else approval_event
        )
        delivery_approved = approved
        approval_status = "approved" if approved else "rejected"

    # --- 5) fan-out: deliver each event ---
    if delivery_approved:
        deliver_tasks = []
        for ev in events:
            ev_id = ev["id"]
            preview = prepared_emails.get(ev_id)
            document = documents.get(ev_id)
            onedrive_url = calendar_links.get(ev_id)
            if preview:
                deliver_tasks.append(
                    context.call_activity(
                        "activity_deliver",
                        {
                            "event": ev,
                            "classification": classifications.get(ev_id, {}),
                            "preview": preview,
                            "document": document,
                            "onedrive_url": onedrive_url,
                            "run_id": run_id,
                            "send_emails": send_emails,
                        },
                    )
                )

        if deliver_tasks:
            deliver_results = yield context.task_all(deliver_tasks)
            for r in deliver_results:
                errors.extend(r.get("errors", []))
                log_lines.extend(r.get("log", []))

    # --- 6) aggregate briefing ---
    briefing_result = yield context.call_activity(
        "activity_aggregate_briefing",
        {
            "events": events,
            "classifications": classifications,
            "errors": errors,
            "followups": [],
            "window_start": window_start,
            "window_end": window_end,
            "run_id": run_id,
        },
    )

    return {
        "run_id": run_id,
        "status": "completed",
        "event_count": len(events),
        "approval_status": approval_status,
        "documents": list(documents.keys()),
        "prepared_emails": list(prepared_emails.keys()),
        "errors": errors,
        "log": log_lines,
        "weekly_briefing": briefing_result.get("weekly_briefing", {}),
    }


# ---------------------------------------------------------------------------
# HTTP trigger — start orchestration
# ---------------------------------------------------------------------------
@bp.route(route="api/run", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def http_start(req: func.HttpRequest, client):
    """HTTP endpoint to start a new calorch orchestration."""
    try:
        body = req.get_json()
    except ValueError:
        body = {}

    run_id = body.get("run_id", _new_run_id())
    instance_id = await client.start_new(
        "calorch_orchestrator",
        instance_id=run_id,
        client_input={
            "run_id": run_id,
            "window_start": body.get("start"),
            "window_end": body.get("end"),
            "send_emails": body.get("send_emails", False),
            "require_approval": body.get("require_approval", True),
        },
    )
    return client.create_check_status_response(req, instance_id)


# ---------------------------------------------------------------------------
# HTTP trigger — raise approval event
# ---------------------------------------------------------------------------
@bp.route(route="api/approval/{instance_id}", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def http_approval(req: func.HttpRequest, client):
    """HTTP endpoint to approve/reject a paused orchestration."""
    instance_id = req.route_params.get("instance_id")
    try:
        body = req.get_json()
    except ValueError:
        body = {}

    approved = body.get("approved", False)
    await client.raise_event(
        instance_id,
        event_name="approval",
        event_data={"approved": approved},
    )
    return func.HttpResponse(
        json.dumps({"status": "approved" if approved else "rejected", "instance_id": instance_id}),
        status_code=202,
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# HTTP trigger — status query
# ---------------------------------------------------------------------------
@bp.route(route="api/status/{instance_id}", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def http_status(req: func.HttpRequest, client):
    """HTTP endpoint to query orchestration status."""
    instance_id = req.route_params.get("instance_id")
    status = await client.get_status(instance_id)
    return func.HttpResponse(
        json.dumps(
            {
                "instance_id": status.instance_id,
                "runtime_status": status.runtime_status.name,
                "created_time": status.created_time.isoformat() if status.created_time else None,
                "last_updated_time": status.last_updated_time.isoformat() if status.last_updated_time else None,
                "input": status.input,
                "output": status.output,
            },
            default=str,
        ),
        status_code=200,
        mimetype="application/json",
    )


def _new_run_id() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def get_blueprint() -> df.Blueprint:
    """Return the Blueprint with all registered functions."""
    return bp
