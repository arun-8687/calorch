"""Azure Durable Functions — data-ingestion pipeline.

A second, independent pipeline that runs on its own schedule, separate from
the report orchestrator (``calorch_orchestrator``). Its job is to download
the configured universe's source data ahead of time and persist it to Azure
Blob Storage, so the report orchestrator can read pre-ingested blobs
(``USE_BLOB_PROVIDERS=true``) instead of making live API calls on the
critical path.

Flow:
  1. timer / HTTP trigger resolves the universe (``SEC_WATCHLIST``) and starts
     the ingestion orchestration (the trigger does the I/O so the
     orchestrator stays deterministic).
  2. fan-out: one ``activity_ingest_ticker`` per ticker (parallel, retried,
     checkpointed). Each activity downloads that ticker's SEC EDGAR
     (fundamentals + segments + filing guidance) and AlphaSense (narrative +
     transcripts + sentiment) data and writes it to ``calorch-inputs``.
  3. fan-in: aggregate per-ticker outcomes into a run summary.

Triggers:
  * timer — scheduled ingestion (``INGEST_CRON_SCHEDULE``, default daily
    22:30 UTC, after the US market close)
  * HTTP POST /api/ingest — on-demand ingestion (optional ``tickers`` body)

Orchestrator code must be deterministic: no I/O, no wall-clock reads
(use ``context.current_utc_datetime``), no random values.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, UTC
from typing import Any

import azure.durable_functions as df
import azure.functions as func

log = logging.getLogger("calorch.durable.ingestion")

bp = df.Blueprint()

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Each ticker's ingestion is independent live I/O (SEC + AlphaSense) — retry
# transient failures that escape the pipeline's own per-ticker handling.
RETRY = df.RetryOptions(first_retry_interval_in_milliseconds=5_000, max_number_of_attempts=3)


# ---------------------------------------------------------------------------
# Orchestrator body (plain generator — unit-testable without the ADF runtime)
# ---------------------------------------------------------------------------
def run_ingest_orchestrator(context: df.DurableOrchestrationContext):
    """Fan out one ingestion activity per ticker, then aggregate outcomes."""
    input_data = context.get_input() or {}
    run_id = input_data.get("run_id") or context.current_utc_datetime.strftime("%Y%m%dT%H%M%SZ")
    # Pin one date across the whole run so every ticker writes the same blob
    # date partition even if the run straddles midnight UTC.
    ingest_date = input_data.get("date") or context.current_utc_datetime.strftime("%Y%m%d")
    tickers = input_data.get("tickers") or []

    if not tickers:
        return {
            "run_id": run_id,
            "status": "completed",
            "ticker_count": 0,
            "message": "No tickers configured for ingestion",
        }

    tasks = [
        context.call_activity_with_retry(
            "activity_ingest_ticker",
            RETRY,
            {"ticker": t, "date": ingest_date, "run_id": run_id},
        )
        for t in tickers
    ]
    results = yield context.task_all(tasks)

    succeeded = [r.get("ticker") for r in results if r.get("status") == "ok"]
    failed = [r.get("ticker") for r in results if r.get("status") != "ok"]
    return {
        "run_id": run_id,
        "status": "completed",
        "date": ingest_date,
        "ticker_count": len(tickers),
        "succeeded": succeeded,
        "failed": failed,
    }


@bp.orchestration_trigger(context_name="context")
def calorch_ingest_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from run_ingest_orchestrator(context))


# ---------------------------------------------------------------------------
# Activity — ingest one ticker's SEC + AlphaSense data into blob storage
# ---------------------------------------------------------------------------
def _ingest_ticker_impl(input: dict[str, Any]) -> dict[str, Any]:
    """Download + persist one ticker's data. Never raises — a bad ticker
    returns an error payload so ``task_all`` fan-in is not aborted."""
    from calorch.data_ingestion import IngestionPipeline
    from calorch.logging_config import clear_correlation, set_request_id, set_run_id

    payload = input or {}
    ticker = (payload.get("ticker") or "").strip()
    date = (payload.get("date") or "").strip()
    run_id = payload.get("run_id") or ""
    set_run_id(run_id)
    set_request_id(run_id or None)
    if not ticker:
        clear_correlation()
        return {"status": "error", "ticker": "", "error": "no ticker provided"}
    try:
        # get_settings() is cached; constructing the pipeline per activity keeps
        # each ticker's blob client/process isolation intact across workers.
        pipeline = IngestionPipeline(date=date)
        result = pipeline.run([ticker])
        detail = result.get("tickers", {}).get(ticker, {})
        return {"status": "ok", "ticker": ticker, "date": date, "detail": detail}
    except Exception as e:  # noqa: BLE001 — activity must degrade, never raise
        log.warning("ingestion failed for %s: %s", ticker, e)
        return {"status": "error", "ticker": ticker, "error": str(e)}
    finally:
        clear_correlation()


@bp.activity_trigger(input_name="input")
def activity_ingest_ticker(input: dict[str, Any]) -> dict[str, Any]:
    return _ingest_ticker_impl(input)


# ---------------------------------------------------------------------------
# Timer trigger — scheduled ingestion
# ---------------------------------------------------------------------------
@bp.timer_trigger(schedule=os.getenv("INGEST_CRON_SCHEDULE", "0 30 22 * * *"), arg_name="timer")
@bp.durable_client_input(client_name="client")
async def timer_ingest(timer: func.TimerRequest, client):
    """Scheduled data ingestion (default: daily 22:30 UTC, after US close)."""
    from calorch.config import get_settings

    now = datetime.now(tz=UTC)
    run_id = "ingest-" + now.strftime("%Y%m%dT%H%M%SZ")
    instance_id = await client.start_new(
        "calorch_ingest_orchestrator",
        instance_id=run_id,
        client_input={
            "run_id": run_id,
            "tickers": get_settings().sec_watchlist,
            "date": now.strftime("%Y%m%d"),
        },
    )
    return f"Started ingestion {instance_id}"


# ---------------------------------------------------------------------------
# HTTP trigger — on-demand ingestion
# ---------------------------------------------------------------------------
@bp.route(route="ingest", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def http_ingest(req: func.HttpRequest, client):
    """POST /api/ingest — start data ingestion on demand.

    Optional JSON body: ``{"tickers": ["AAPL", "MSFT"]}`` — defaults to the
    configured ``SEC_WATCHLIST`` universe.
    """
    from calorch.config import get_settings

    try:
        body = req.get_json()
    except ValueError:
        body = {}

    now = datetime.now(tz=UTC)
    run_id = body.get("run_id") or "ingest-" + now.strftime("%Y%m%dT%H%M%SZ")
    if not _RUN_ID_RE.match(run_id):
        return func.HttpResponse(
            json.dumps({"error": "run_id must match [A-Za-z0-9_-]{1,64}"}),
            status_code=400,
            mimetype="application/json",
        )
    tickers = body.get("tickers") or get_settings().sec_watchlist
    instance_id = await client.start_new(
        "calorch_ingest_orchestrator",
        instance_id=run_id,
        client_input={"run_id": run_id, "tickers": tickers, "date": now.strftime("%Y%m%d")},
    )
    return client.create_check_status_response(req, instance_id)


def get_blueprint() -> df.Blueprint:
    return bp
