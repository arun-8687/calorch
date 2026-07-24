"""Azure Functions — scheduled data-ingestion pipeline.

A standalone background function, separate from the report orchestrator
(``calorch_orchestrator``), that downloads the configured universe's source
data ahead of time and persists it to Azure Blob Storage. The report
orchestrator then reads pre-ingested blobs (``USE_BLOB_PROVIDERS=true``)
instead of making live API calls on its critical path.

Ingestion is a background batch with no human-in-the-loop gate, so it needs
none of the durable machinery the report flow uses: it simply loops over
``SEC_WATCHLIST`` via ``IngestionPipeline.run()``, which already iterates the
ticker list and isolates per-ticker failures so one bad ticker can't abort
the batch.

Triggers:
  * timer — scheduled ingestion (``INGEST_CRON_SCHEDULE``, default daily
    22:30 UTC, after the US market close)
  * HTTP POST /api/ingest — on-demand ingestion (optional ``tickers`` body)
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, UTC
from typing import Any

import azure.functions as func

from calorch.logging_config import clear_correlation, set_request_id, set_run_id

log = logging.getLogger("calorch.durable.ingestion")

bp = func.Blueprint()

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_TICKERS = 500


def _bad_request(message: str) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": message}), status_code=400, mimetype="application/json"
    )


def _new_run_id() -> str:
    return "ingest-" + datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def run_ingestion(tickers: list[str] | None, run_id: str) -> dict[str, Any]:
    """Download + persist the universe's SEC + AlphaSense data to blob.

    Loops over the tickers (defaulting to ``SEC_WATCHLIST``) via
    ``IngestionPipeline.run``, which catches per-ticker failures so one bad
    ticker can't abort the batch. A failure to *start* the batch at all
    (unreadable settings, blob container unreachable) still propagates, so
    the invocation is recorded as failed rather than silently reporting
    success.
    """
    from calorch.config import get_settings
    from calorch.data_ingestion import IngestionPipeline

    set_run_id(run_id)
    set_request_id(run_id or None)
    try:
        universe = tickers or get_settings().sec_watchlist
        if not universe:
            log.info("ingestion %s: no tickers configured", run_id)
            return {
                "run_id": run_id,
                "status": "completed",
                "ticker_count": 0,
                "message": "No tickers configured for ingestion",
            }
        results = IngestionPipeline().run(universe)
        ingested = list(results.get("tickers", {}).keys())
        log.info("ingestion %s: %d tickers persisted", run_id, len(ingested))
        return {
            "run_id": run_id,
            "status": "completed",
            "ticker_count": len(universe),
            "tickers": ingested,
        }
    finally:
        clear_correlation()


# ---------------------------------------------------------------------------
# Timer trigger — scheduled ingestion
# ---------------------------------------------------------------------------
@bp.timer_trigger(schedule=os.getenv("INGEST_CRON_SCHEDULE", "0 30 22 * * *"), arg_name="timer")
def timer_ingest(timer: func.TimerRequest) -> None:
    """Scheduled data ingestion (default: daily 22:30 UTC, after US close)."""
    run_ingestion(None, _new_run_id())


# ---------------------------------------------------------------------------
# HTTP trigger — on-demand ingestion
# ---------------------------------------------------------------------------
@bp.route(route="ingest", methods=["POST"])
def http_ingest(req: func.HttpRequest) -> func.HttpResponse:
    """POST /api/ingest — run data ingestion on demand.

    Optional JSON body: ``{"tickers": ["AAPL", "MSFT"]}`` — defaults to the
    configured ``SEC_WATCHLIST`` universe. Runs synchronously and returns the
    per-run summary.
    """
    try:
        body = req.get_json()
    except ValueError:
        body = {}

    run_id = body.get("run_id") or _new_run_id()
    if not _RUN_ID_RE.match(run_id):
        return _bad_request("run_id must match [A-Za-z0-9_-]{1,64}")

    # `tickers` must be an explicit list. A bare string would otherwise be
    # iterated character-by-character by the pipeline ("AAPL" -> A, A, P, L),
    # and an unbounded list would let one request fan out arbitrarily.
    tickers = body.get("tickers")
    if tickers is not None:
        if not isinstance(tickers, list) or not all(isinstance(t, str) for t in tickers):
            return _bad_request("tickers must be a list of strings")
        tickers = [t.strip().upper() for t in tickers if t.strip()]
        if len(tickers) > _MAX_TICKERS:
            return _bad_request(f"tickers may not exceed {_MAX_TICKERS} entries")

    result = run_ingestion(tickers or None, run_id)
    return func.HttpResponse(json.dumps(result), status_code=200, mimetype="application/json")


def get_blueprint() -> func.Blueprint:
    return bp
