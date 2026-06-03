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
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field, field_validator

from calorch.audit import get_audit_log
from calorch.config import get_settings
from calorch.graph import make_graph
from calorch.http_client import close_client, get_metrics
from calorch.llm import get_chat_model
from calorch.logging_config import (
    configure_logging,
    get_logger,
    get_request_id,
    get_run_id,
    get_thread_id,
    set_request_id,
    set_run_id,
    set_thread_id,
)
from calorch.nodes import Context, set_context
from calorch.rate_limit import get_rate_limiter
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
configure_logging()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _startup()
    try:
        yield
    finally:
        _shutdown()


app = FastAPI(title="calorch", version="0.1.0", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# CORS — only configured if explicit allow-list is set
# ---------------------------------------------------------------------------
def _configure_cors() -> None:
    """Add CORS middleware only when explicit origins are configured.

    An empty allow-list (the default) means no CORS headers are sent — the
    API is effectively same-origin only. This is the safe default for
    server-to-server deployments.
    """
    settings = get_settings()
    if not settings.cors_allowed_origins:
        log.info("CORS: no origins configured, same-origin only")
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["X-Calorch-API-Key", "X-Request-ID", "Content-Type"],
        max_age=3600,
    )
    log.info("CORS: allowed origins = %s", settings.cors_allowed_origins)


def _configure_telemetry() -> None:
    """Initialise OpenTelemetry SDK + FastAPI / httpx auto-instrumentation.

    No-ops gracefully when opentelemetry packages are not installed.
    """
    from calorch.telemetry import (
        init_tracing,
        instrument_fastapi,
        instrument_httpx,
    )
    if init_tracing(service_name="calorch"):
        log.info("OpenTelemetry SDK initialised")
    else:
        log.info("OpenTelemetry SDK not initialised (package missing or no exporter)")
    if instrument_fastapi(app):
        log.info("FastAPI auto-instrumentation enabled")
    if instrument_httpx():
        log.info("httpx auto-instrumentation enabled")


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


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next) -> Response:
    """Stamp every request with a correlation ID (honour inbound, generate if absent)."""
    inbound = request.headers.get("X-Request-ID")
    rid = set_request_id(inbound)
    try:
        response = await call_next(request)
    finally:
        # Clear request id after the request completes so the next one starts fresh
        from calorch.logging_config import clear_correlation
        clear_correlation()
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def _request_size_limit(request: Request, call_next) -> Response:
    """Reject oversized request bodies before they hit the body parser."""
    max_bytes = get_settings().max_request_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                log.warning(
                    "Rejected oversized request: %s bytes > %s",
                    content_length, max_bytes,
                )
                return Response(
                    content=f"request body too large: {content_length} > {max_bytes}",
                    status_code=413,
                    media_type="text/plain",
                )
        except ValueError:
            return Response("invalid content-length", status_code=400)
    return await call_next(request)


@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next) -> Response:
    """Per-caller rate limit. Buckets by API key (falls back to client IP)."""
    # Skip rate limiting for health/ready probes so the platform can poll freely
    if request.url.path in {"/health", "/ready"}:
        return await call_next(request)
    caller = request.headers.get("X-Calorch-API-Key") or (
        request.client.host if request.client else "unknown"
    )
    limiter = get_rate_limiter()
    allowed, retry_after = limiter.check(caller)
    if not allowed:
        log.warning("Rate limit hit for caller=%s on %s", caller[:12] + "...", request.url.path)
        get_audit_log().rate_limited(request.url.path, caller[:12] + "...")
        return Response(
            content="rate limit exceeded",
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            media_type="text/plain",
        )
    return await call_next(request)


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
    _configure_cors()
    _configure_telemetry()
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

    @field_validator("end")
    @classmethod
    def _validate_end_after_start(cls, v: datetime, info) -> datetime:
        """Reject windows that end before they start at the schema level too."""
        start = info.data.get("start")
        if start is not None and v <= start:
            raise ValueError("end must be after start")
        return v

    @field_validator("end")
    @classmethod
    def _validate_max_window(cls, v: datetime, info) -> datetime:
        """Cap window at 31 days to prevent runaway cost on misconfigured jobs."""
        start = info.data.get("start")
        if start is not None and (v - start).days > 31:
            raise ValueError("window must be at most 31 days")
        return v


class RunResponse(BaseModel):
    thread_id: str
    status: str
    events: int
    briefing_path: str | None = None
    errors: list[str] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    approved: bool
    reason: str = Field(default="", description="Audit-log note explaining the decision")


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
# Graph execution timeout
# ---------------------------------------------------------------------------
class _GraphTimeout(Exception):
    """Raised when a graph invoke() call exceeds the configured timeout."""


def _invoke_with_timeout(graph, input, config, timeout_seconds: float):
    """Invoke the graph in a worker thread bounded by ``timeout_seconds``.

    LangGraph's ``invoke()`` is synchronous and can hang on a stuck LLM or
    HTTP call. Running it in a thread lets us enforce a hard deadline via
    ``Future.result(timeout=...)``. On timeout, the thread is orphaned but
    the request returns 504 — the orphaned thread will be cleaned up when
    the graph's internal retry/reconnect logic eventually completes or the
    ACA replica is recycled.
    """
    import concurrent.futures

    if timeout_seconds <= 0:
        return graph.invoke(input, config=config)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(graph.invoke, input, config=config)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            raise _GraphTimeout(
                f"graph invoke exceeded {timeout_seconds}s"
            ) from exc


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
    set_run_id(thread_id)
    set_thread_id(thread_id)
    audit = get_audit_log()
    audit.run_started(
        thread_id=thread_id,
        window_start=req.start.isoformat(),
        window_end=req.end.isoformat(),
        send_emails=req.send_emails,
    )
    initial: OrchestratorState = {
        "window_start": req.start,
        "window_end": req.end,
        "use_mocks": False,
        "run_id": thread_id,
        "send_emails": req.send_emails,
        "require_approval": req.send_emails and req.require_approval,
    }
    cfg = {"configurable": {"thread_id": thread_id}}
    timeout = get_settings().run_timeout_seconds
    try:
        result = _invoke_with_timeout(_GRAPH, initial, cfg, timeout)
    except _GraphTimeout as exc:
        log.error("Run %s exceeded %ss timeout", thread_id, timeout)
        audit.run_timeout(thread_id=thread_id, timeout_seconds=timeout)
        raise HTTPException(504, f"run exceeded {timeout}s timeout") from exc
    except Exception as exc:
        log.error("Run %s failed: %s", thread_id, exc, exc_info=True)
        audit.run_failed(thread_id=thread_id, error=str(exc))
        raise
    response = _response(thread_id, result)
    audit.run_completed(
        thread_id=thread_id,
        status=response.status,
        events=response.events,
        errors=response.errors,
    )
    return response


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
    get_audit_log().approval_decision(
        thread_id=thread_id, approved=req.approved, reason=req.reason or ""
    )
    set_run_id(thread_id)
    set_thread_id(thread_id)
    timeout = get_settings().run_timeout_seconds
    try:
        result = _invoke_with_timeout(
            _GRAPH, Command(resume={"approved": req.approved}), cfg, timeout
        )
    except _GraphTimeout as exc:
        log.error("Approval resume for %s exceeded %ss timeout", thread_id, timeout)
        get_audit_log().run_timeout(thread_id=thread_id, timeout_seconds=timeout)
        raise HTTPException(504, f"approval resume exceeded {timeout}s timeout") from exc
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
