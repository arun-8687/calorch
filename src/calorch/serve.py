"""FastAPI wrapper for Azure Container Apps.

Exposes:
  GET  /health         — liveness probe
  GET  /ready          — readiness probe (checks all dependencies)
  POST /run            — start a new orchestrator run
  GET  /runs/{id}      — fetch run state
  POST /runs/{id}/approval — approve or reject a paused send run
  GET  /briefing       — list recent briefings
  GET  /metrics        — HTTP client metrics

In production, every POST /run creates a LangGraph thread. Configure
``CHECKPOINT_POSTGRES_URI`` so approval checkpoints survive process restarts.
"""
from __future__ import annotations

import json
import hmac
import logging
import os
import signal
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from calorch.config import get_settings
from calorch.graph import make_graph
from calorch.http_client import close_client, get_metrics
from calorch.llm import get_chat_model
from calorch.nodes import Context, set_context
from calorch.state import OrchestratorState
from calorch.tools import (
    make_cik_lookup,
    make_enterprise_data_client,
    make_graph_client,
    make_onedrive_client,
    make_providers,
    make_repository,
)

log = logging.getLogger("calorch.serve")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _startup()
    try:
        yield
    finally:
        _shutdown()


app = FastAPI(title="calorch", version="0.1.0", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Security middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def _security_headers(request: Request, call_next) -> Response:
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def _build_context() -> Context:
    s = get_settings()
    out_dir = Path(os.getenv("OUTPUT_DIR", "/data/out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = Context(
        graph=make_graph_client(s),
        onedrive=make_onedrive_client(s),
        repo=make_repository(s),
        enterprise=make_enterprise_data_client(s),
        llm=get_chat_model(s),
        output_dir=out_dir,
        send_emails=False,            # always start in draft mode; caller approves
        providers=make_providers(s),
        cik_lookup=make_cik_lookup(s),
    )
    set_context(ctx)
    return ctx


_CTX: Context | None = None
_GRAPH = None
_CHECKPOINTER: Any = None
_CHECKPOINTER_CM: Any = None


def _build_checkpointer() -> tuple[Any, Any | None]:
    """Use durable PostgreSQL checkpoints when configured."""
    uri = get_settings().checkpoint_postgres_uri
    if not uri:
        log.warning(
            "CHECKPOINT_POSTGRES_URI is unset; using in-memory checkpoints. "
            "Approval resume will not survive a process restart."
        )
        return MemorySaver(), None
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:  # pragma: no cover - dependency is in the container image
        raise RuntimeError(
            "CHECKPOINT_POSTGRES_URI requires langgraph-checkpoint-postgres."
        ) from exc
    manager = PostgresSaver.from_conn_string(uri)
    checkpointer = manager.__enter__()
    checkpointer.setup()
    return checkpointer, manager


def _startup() -> None:
    global _CTX, _GRAPH, _CHECKPOINTER, _CHECKPOINTER_CM
    _CTX = _build_context()
    _CHECKPOINTER, _CHECKPOINTER_CM = _build_checkpointer()
    _GRAPH = make_graph(checkpointer=_CHECKPOINTER)
    _install_signal_handlers()
    log.info("calorch ready — mocks=%s repo=%s",
             get_settings().use_mocks, get_settings().repo_backend)


def _shutdown() -> None:
    """Clean up resources on shutdown."""
    if _CHECKPOINTER_CM is not None:
        _CHECKPOINTER_CM.__exit__(None, None, None)
    # Close shared HTTP client to release connection pool
    try:
        close_client()
        log.info("HTTP client closed")
    except Exception as e:
        log.warning("Error closing HTTP client: %s", e)


def _install_signal_handlers() -> None:
    """Install graceful shutdown handlers for SIGTERM/SIGINT."""
    def _handler(signum, frame):
        log.info("Received signal %s, initiating graceful shutdown", signum)
        _shutdown()
        import sys
        sys.exit(0)
    
    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
        log.debug("Signal handlers installed for graceful shutdown")
    except (ValueError, OSError):
        # Not on main thread or unsupported platform
        pass


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    start: datetime
    end: datetime
    send_emails: bool = Field(default=False, description="Actually send (vs. draft)")
    require_approval: bool = Field(
        default=True,
        description="Pause after previews and require approval before sending email.",
    )


class RunResponse(BaseModel):
    thread_id: str
    status: str
    events: int
    briefing_path: str | None = None
    errors: list[str] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    approved: bool


def _require_api_key(
    x_calorch_api_key: str | None = Header(default=None, alias="X-Calorch-API-Key"),
) -> None:
    """Protect workflow mutation and inspection endpoints."""
    settings = get_settings()
    expected = settings.calorch_api_key
    if not expected:
        if settings.use_mocks:
            return
        raise HTTPException(503, "CALORCH_API_KEY is not configured")
    if not x_calorch_api_key or not hmac.compare_digest(x_calorch_api_key, expected):
        raise HTTPException(401, "invalid API key")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — process is alive."""
    return {"status": "ok", "ts": datetime.now(tz=timezone.utc).isoformat()}


@app.get("/ready")
def ready() -> dict[str, Any]:
    """Readiness probe — all dependencies are initialized."""
    checks = {
        "graph": _GRAPH is not None,
        "context": _CTX is not None,
        "checkpointer": _CHECKPOINTER is not None,
    }
    ready_flag = all(checks.values())
    return {
        "status": "ready" if ready_flag else "not_ready",
        "checks": checks,
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }


@app.get("/metrics", dependencies=[Depends(_require_api_key)])
def metrics() -> dict[str, Any]:
    """HTTP client metrics — request count, latency, error rate per service."""
    return get_metrics().get_stats()


@app.post("/run", response_model=RunResponse, dependencies=[Depends(_require_api_key)])
def run(req: RunRequest) -> RunResponse:
    if _GRAPH is None:
        raise HTTPException(503, "not initialised")
    if req.end <= req.start:
        raise HTTPException(422, "end must be after start")
    thread_id = "run-" + uuid.uuid4().hex
    initial: OrchestratorState = {
        "window_start": req.start,
        "window_end": req.end,
        "use_mocks": False,
        "run_id": thread_id,
        "send_emails": req.send_emails,
        "require_approval": req.send_emails and req.require_approval,
    }
    cfg = {"configurable": {"thread_id": thread_id}}
    result = _GRAPH.invoke(initial, config=cfg)
    return _response(thread_id, result)


@app.post(
    "/runs/{thread_id}/approval",
    response_model=RunResponse,
    dependencies=[Depends(_require_api_key)],
)
def approve_run(thread_id: str, req: ApprovalRequest) -> RunResponse:
    """Resume a run paused by ``approval_gate``."""
    if _GRAPH is None:
        raise HTTPException(503, "not initialised")
    cfg = {"configurable": {"thread_id": thread_id}}
    state = _GRAPH.get_state(cfg)
    if not state.values:
        raise HTTPException(404, f"thread {thread_id} not found")
    if "approval_gate" not in (state.next or ()):
        raise HTTPException(409, f"thread {thread_id} is not waiting for approval")
    result = _GRAPH.invoke(Command(resume={"approved": req.approved}), config=cfg)
    return _response(thread_id, result)


@app.get("/runs/{thread_id}", dependencies=[Depends(_require_api_key)])
def get_run(thread_id: str) -> dict:
    if _GRAPH is None:
        raise HTTPException(503, "not initialised")
    cfg = {"configurable": {"thread_id": thread_id}}
    state = _GRAPH.get_state(cfg)
    if not state.values:
        raise HTTPException(404, f"thread {thread_id} not found")
    return {
        "thread_id": thread_id,
        "next": [n for n in (state.next or ())],
        "values": _safe(state.values),
    }


def _safe(v) -> dict:
    try:
        return json.loads(json.dumps(v, default=str))
    except Exception:
        return {"_unserialisable": str(type(v))}


def _response(thread_id: str, result: dict) -> RunResponse:
    if result.get("__interrupt__"):
        status = "pending_approval"
    elif result.get("approval_status") == "rejected":
        status = "rejected"
    else:
        status = "complete"
    return RunResponse(
        thread_id=thread_id,
        status=status,
        events=len(result.get("events", [])),
        briefing_path=result["weekly_briefing"].path if result.get("weekly_briefing") else None,
        errors=result.get("errors", []),
    )
