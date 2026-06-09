"""Azure Durable Functions — timer-triggered orchestrator with LangGraph agents.

Architecture:
  * Timer trigger starts the orchestrator on a schedule
  * Orchestrator calls activities for each step
  * Activities invoke compiled LangGraph agent subgraphs
  * External events for approval gates

No HTTP triggers for the orchestrator — purely timer-driven.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import azure.durable_functions as df
import azure.functions as func

# ---------------------------------------------------------------------------
# Blueprint instance
# ---------------------------------------------------------------------------
bp = df.Blueprint()


# ---------------------------------------------------------------------------
# Timer trigger — starts the orchestrator
# ---------------------------------------------------------------------------
@bp.timer_trigger(schedule="0 0 9 * * 1", arg_name="timer")  # Every Monday at 9 AM UTC
@bp.durable_client_input(client_name="client")
async def timer_start(timer: func.TimerRequest, client):
    """Timer-triggered entry point. Starts the calorch orchestrator.

    Runs every Monday at 9:00 AM UTC by default. Configurable via
    CRON_SCHEDULE env var.
    """
    import os
    # Default: look ahead 7 days
    now = datetime.now(tz=timezone.utc)
    window_start = now.isoformat()
    window_end = (now + timedelta(days=7)).isoformat()

    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    instance_id = await client.start_new(
        "calorch_orchestrator",
        instance_id=run_id,
        client_input={
            "run_id": run_id,
            "window_start": window_start,
            "window_end": window_end,
            "send_emails": False,
            "require_approval": True,
        },
    )
    return f"Started orchestration {instance_id}"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
@bp.orchestration_trigger(context_name="context")
def calorch_orchestrator(context: df.DurableOrchestrationContext) -> dict[str, Any]:
    """Main durable orchestrator for the calorch workflow.

    Steps:
      1. Scan calendar
      2. Classify events
      3. Fan-out: each event → LangGraph agent (parallel)
      4. Approval gate (external event)
      5. Fan-out: deliver events (parallel)
      6. Aggregate briefing
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

    if not events:
        return {"run_id": run_id, "status": "completed", "event_count": 0}

    # --- 2) classify events ---
    classify_result = yield context.call_activity(
        "activity_classify",
        {"events": events, "run_id": run_id},
    )
    classifications = classify_result.get("classifications", {})

    # --- 3) fan-out: prepare each event via its LangGraph agent ---
    agent_tasks = [
        context.call_activity(
            "activity_agent",
            {"event": ev, "classification": classifications.get(ev["id"], {}), "run_id": run_id},
        )
        for ev in events
    ]
    agent_results = yield context.task_all(agent_tasks)

    # Merge outputs
    documents = {}
    prepared_emails = {}
    calendar_links = {}
    errors = []
    for r in agent_results:
        documents.update(r.get("documents", {}))
        prepared_emails.update(r.get("prepared_emails", {}))
        calendar_links.update(r.get("calendar_links", {}))
        errors.extend(r.get("errors", []))

    # --- 4) approval gate ---
    delivery_approved = True
    approval_status = "not_required"
    if send_emails and require_approval and prepared_emails:
        approval_event = yield context.wait_for_external_event(
            "approval", timeout=60 * 60 * 24
        )
        approved = bool(
            approval_event.get("approved") if isinstance(approval_event, dict) else approval_event
        )
        delivery_approved = approved
        approval_status = "approved" if approved else "rejected"

    # --- 5) fan-out: deliver each event ---
    if delivery_approved:
        deliver_tasks = [
            context.call_activity(
                "activity_deliver",
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
            yield context.task_all(deliver_tasks)

    # --- 6) aggregate briefing ---
    yield context.call_activity(
        "activity_aggregate_briefing",
        {
            "events": events,
            "classifications": classifications,
            "errors": errors,
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
    }


def _new_run_id() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def get_blueprint() -> df.Blueprint:
    return bp
