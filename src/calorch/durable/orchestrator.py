"""Azure Durable Functions orchestrator — calorch workflow.

ADF owns the high-level flow control (sequencing, fan-out, retries,
human-in-the-loop approval, timers); LangGraph owns the per-event agent
subgraphs that run inside the ``activity_agent`` activity.

Flow:
  1. scan calendar                       → activity (with retry)
  2. classify events                     → activity (with retry)
  3. fan-out: one LangGraph agent per event (parallel, task_all)
  4. approval gate                       → external event vs. durable timer
  5. fan-out: deliver approved events    (parallel, task_all)
  6. aggregate briefing                  → activity

Triggers:
  * timer  — scheduled run (CRON_SCHEDULE env var, default Mon 09:00 UTC)
  * HTTP POST /api/run                   — start a run on demand
  * HTTP POST /api/approval/{id}         — approve/reject a paused run
  * HTTP GET  /api/status/{id}           — query run status

Orchestrator code must be deterministic: no I/O, no wall-clock reads
(use ``context.current_utc_datetime``), no random values.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, UTC
from typing import Any

import azure.durable_functions as df
import azure.functions as func

bp = df.Blueprint()

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _bad_request(message: str) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": message}), status_code=400, mimetype="application/json"
    )

# Transient-failure retry for activities (Graph/LLM/SEC calls inside).
# Mirrors the RetryPolicy(max_attempts=3) the pure-LangGraph graph used.
RETRY = df.RetryOptions(first_retry_interval_in_milliseconds=5_000, max_number_of_attempts=3)

DEFAULT_APPROVAL_TIMEOUT_HOURS = 24


# ---------------------------------------------------------------------------
# Orchestrator body (plain generator — unit-testable without the ADF runtime)
# ---------------------------------------------------------------------------
def run_orchestrator(context: df.DurableOrchestrationContext):
    input_data = context.get_input() or {}
    # Deterministic default: derived from the orchestration's replay-safe clock.
    run_id = input_data.get("run_id") or context.current_utc_datetime.strftime("%Y%m%dT%H%M%SZ")
    send_emails = input_data.get("send_emails", False)
    require_approval = input_data.get("require_approval", True)
    window_start = input_data.get("window_start")
    window_end = input_data.get("window_end")
    approval_timeout_hours = input_data.get(
        "approval_timeout_hours", DEFAULT_APPROVAL_TIMEOUT_HOURS
    )

    # --- 1) scan calendar ---
    scan_result = yield context.call_activity_with_retry(
        "activity_scan_calendar",
        RETRY,
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
    # raw_events must travel along: the SEC form fast-path in
    # prefilter_keywords reads the raw payload's _form/_items fields.
    classify_result = yield context.call_activity_with_retry(
        "activity_classify",
        RETRY,
        {"events": events, "raw_events": raw_events, "run_id": run_id},
    )
    classifications = classify_result.get("classifications", {})

    # --- 3) fan-out: prepare each event via its LangGraph agent ---
    agent_tasks = [
        context.call_activity_with_retry(
            "activity_agent",
            RETRY,
            {"event": ev, "classification": classifications.get(ev["id"], {}), "run_id": run_id},
        )
        for ev in events
    ]
    agent_results = yield context.task_all(agent_tasks)

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

    # --- 4) approval gate (external event raced against a durable timer) ---
    delivery_approved = True
    approval_status = "not_required"
    if send_emails and require_approval and prepared_emails:
        approval_task = context.wait_for_external_event("approval")
        deadline = context.current_utc_datetime + timedelta(hours=approval_timeout_hours)
        timeout_task = context.create_timer(deadline)
        winner = yield context.task_any([approval_task, timeout_task])
        if winner == approval_task:
            timeout_task.cancel()
            decision = approval_task.result
            approved = bool(decision.get("approved") if isinstance(decision, dict) else decision)
            delivery_approved = approved
            approval_status = "approved" if approved else "rejected"
        else:
            delivery_approved = False
            approval_status = "timed_out"

    # --- 5) fan-out: deliver approved events ---
    emails: dict[str, Any] = {}
    followups: list[Any] = []
    if delivery_approved:
        deliver_tasks = [
            context.call_activity_with_retry(
                "activity_deliver",
                RETRY,
                {
                    "event": ev,
                    "classification": classifications.get(ev["id"], {}),
                    "preview": prepared_emails.get(ev["id"]),
                    "document": documents.get(ev["id"]),
                    "onedrive_url": calendar_links.get(ev["id"]),
                    "run_id": run_id,
                    "send_emails": send_emails,
                },
            )
            for ev in events
            if prepared_emails.get(ev["id"])
        ]
        if deliver_tasks:
            deliver_results = yield context.task_all(deliver_tasks)
            for r in deliver_results:
                emails.update(r.get("emails", {}))
                followups.extend(r.get("followups", []))
                errors.extend(r.get("errors", []))
                log_lines.extend(r.get("log", []))

    # --- 6) aggregate briefing ---
    briefing_result = yield context.call_activity_with_retry(
        "activity_aggregate_briefing",
        RETRY,
        {
            "events": events,
            "classifications": classifications,
            "emails": emails,
            "errors": errors,
            "followups": followups,
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
        "emails": list(emails.keys()),
        "followup_count": len(followups),
        "errors": errors,
        "log": log_lines,
        "weekly_briefing": briefing_result.get("weekly_briefing", {}),
    }


@bp.orchestration_trigger(context_name="context")
def calorch_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from run_orchestrator(context))


# ---------------------------------------------------------------------------
# Timer trigger — scheduled start
# ---------------------------------------------------------------------------
@bp.timer_trigger(schedule=os.getenv("CRON_SCHEDULE", "0 0 9 * * 1"), arg_name="timer")
@bp.durable_client_input(client_name="client")
async def timer_start(timer: func.TimerRequest, client):
    """Scheduled entry point (default: Mondays 09:00 UTC, 7-day lookahead)."""
    now = datetime.now(tz=UTC)
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    instance_id = await client.start_new(
        "calorch_orchestrator",
        instance_id=run_id,
        client_input={
            "run_id": run_id,
            "window_start": now.isoformat(),
            "window_end": (now + timedelta(days=7)).isoformat(),
            "send_emails": False,
            "require_approval": True,
        },
    )
    return f"Started orchestration {instance_id}"


# ---------------------------------------------------------------------------
# HTTP trigger — start orchestration on demand
# ---------------------------------------------------------------------------
@bp.route(route="run", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def http_start(req: func.HttpRequest, client):
    """POST /api/run — start a new calorch orchestration."""
    try:
        body = req.get_json()
    except ValueError:
        body = {}

    run_id = body.get("run_id") or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    if not _RUN_ID_RE.match(run_id):
        return _bad_request("run_id must match [A-Za-z0-9_-]{1,64}")
    # Validate ISO-8601 window bounds when supplied (None → orchestrator default).
    for field in ("start", "end"):
        val = body.get(field)
        if val is not None:
            try:
                datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            except ValueError:
                return _bad_request(f"{field} must be ISO-8601")
    instance_id = await client.start_new(
        "calorch_orchestrator",
        instance_id=run_id,
        client_input={
            "run_id": run_id,
            "window_start": body.get("start"),
            "window_end": body.get("end"),
            "send_emails": body.get("send_emails", False),
            "require_approval": body.get("require_approval", True),
            "approval_timeout_hours": body.get(
                "approval_timeout_hours", DEFAULT_APPROVAL_TIMEOUT_HOURS
            ),
        },
    )
    return client.create_check_status_response(req, instance_id)


# ---------------------------------------------------------------------------
# HTTP trigger — raise approval event
# ---------------------------------------------------------------------------
@bp.route(route="approval/{instance_id}", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def http_approval(req: func.HttpRequest, client):
    """POST /api/approval/{instance_id} — approve/reject a paused run."""
    instance_id = req.route_params.get("instance_id")
    try:
        body = req.get_json()
    except ValueError:
        body = {}

    # Reject unknown instances rather than silently accepting a no-op event.
    status = await client.get_status(instance_id)
    if status is None or status.runtime_status is None:
        return func.HttpResponse(
            json.dumps({"error": f"instance {instance_id} not found"}),
            status_code=404,
            mimetype="application/json",
        )

    approved = bool(body.get("approved", False))
    await client.raise_event(instance_id, event_name="approval", event_data={"approved": approved})
    return func.HttpResponse(
        json.dumps(
            {"status": "approved" if approved else "rejected", "instance_id": instance_id}
        ),
        status_code=202,
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# HTTP trigger — status query
# ---------------------------------------------------------------------------
@bp.route(route="status/{instance_id}", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def http_status(req: func.HttpRequest, client):
    """GET /api/status/{instance_id} — query orchestration status."""
    instance_id = req.route_params.get("instance_id")
    status = await client.get_status(instance_id)
    if status is None or status.runtime_status is None:
        return func.HttpResponse(
            json.dumps({"error": f"instance {instance_id} not found"}),
            status_code=404,
            mimetype="application/json",
        )
    return func.HttpResponse(
        json.dumps(_status_payload(status), default=str),
        status_code=200,
        mimetype="application/json",
    )


def _status_payload(status: Any) -> dict[str, Any]:
    """Project a DurableOrchestrationStatus to a non-sensitive response body.

    The full output contains ``errors`` (exception reprs that can embed event
    text/PII) and ``log``; expose counts, never bodies, and never the input.
    """
    out = status.output if isinstance(status.output, dict) else {}
    return {
        "instance_id": status.instance_id,
        "runtime_status": status.runtime_status.name,
        "created_time": status.created_time.isoformat() if status.created_time else None,
        "last_updated_time": status.last_updated_time.isoformat()
        if status.last_updated_time
        else None,
        "event_count": out.get("event_count"),
        "approval_status": out.get("approval_status"),
        "error_count": len(out.get("errors", []) or []),
        "followup_count": out.get("followup_count"),
    }


def get_blueprint() -> df.Blueprint:
    return bp
