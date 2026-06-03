"""OpenTelemetry tracing — works whether or not the OTel SDK is installed.

If ``opentelemetry-api`` is not installed, all tracer functions are no-ops.
If it is installed but no exporter is configured, spans are created in-memory
and discarded. To export, set ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var.

Provides:
  * ``tracer`` — namespaced tracer for calorch
  * ``start_span(name, **attrs)`` — context manager that opens a span
  * ``get_current_trace_id()`` — returns the active trace_id as hex
  * ``init_tracing()`` — initialises the SDK with OTLP HTTP exporter if available
  * ``instrument_httpx()`` — patches the shared HTTP client to emit spans

Span hierarchy:
  calorch.run
    calorch.node.scan_calendar
      calorch.http.sec_ixbrl
    calorch.node.prefilter_keywords
    calorch.node.llm_classify
      calorch.llm.invoke (model, tokens)
    calorch.node.prepare_event × N
    calorch.node.approval_gate
    calorch.node.deliver_event × N
    calorch.node.aggregate_briefing
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

# Try to import the OTel API. If unavailable, fall back to a no-op tracer.
try:
    from opentelemetry import trace
    from opentelemetry.trace import (
        Span,
        SpanKind,
        Status,
        StatusCode,
        Tracer,
    )
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on optional dep
    _OTEL_AVAILABLE = False
    Span = Any  # type: ignore
    Tracer = Any  # type: ignore
    SpanKind = Any  # type: ignore
    Status = Any  # type: ignore
    StatusCode = Any  # type: ignore

    class _NoOpTracer:
        """Mimics the OTel Tracer API for the no-deps case."""

        @contextmanager
        def start_as_current_span(self, name: str, attributes=None, **kw) -> Iterator[Any]:
            yield _NoOpSpan()

    class _NoOpSpan:
        def set_attribute(self, k: str, v: Any) -> None: ...
        def set_status(self, *a: Any, **kw: Any) -> None: ...
        def record_exception(self, e: BaseException) -> None: ...
        def end(self) -> None: ...

    class _NoOpTraceModule:
        @property
        def get_tracer(self) -> Any:
            return lambda *a, **kw: _NoOpTracer()

        def get_current_span(self) -> _NoOpSpan:
            return _NoOpSpan()

    trace = _NoOpTraceModule()  # type: ignore


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------
_TRACER_NAME = "calorch"


def _get_tracer() -> Any:
    if _OTEL_AVAILABLE:
        return trace.get_tracer(_TRACER_NAME)
    return _NoOpTracer()


tracer: Any = _get_tracer()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
@contextmanager
def start_span(name: str, **attrs: Any) -> Iterator[Span]:
    """Open a span. Works as a no-op when OTel is not installed.

    Usage:
        with start_span("calorch.node.llm_classify", event_id="evt-1") as span:
            ...
            span.set_attribute("model", "gpt-4o")
    """
    if _OTEL_AVAILABLE:
        with tracer.start_as_current_span(name, attributes=attrs) as span:
            try:
                yield span
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
    else:
        with tracer.start_as_current_span(name, **attrs) as span:
            yield span


def get_current_trace_id() -> str | None:
    """Return the active trace ID as a hex string, or None if no active span."""
    if not _OTEL_AVAILABLE:
        return None
    span = trace.get_current_span()
    if span is None:
        return None
    ctx = span.get_span_context() if hasattr(span, "get_span_context") else None
    if ctx is None or not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")


def get_current_span_id() -> str | None:
    """Return the active span ID as a hex string, or None if no active span."""
    if not _OTEL_AVAILABLE:
        return None
    span = trace.get_current_span()
    if span is None:
        return None
    ctx = span.get_span_context() if hasattr(span, "get_span_context") else None
    if ctx is None or not ctx.is_valid:
        return None
    return format(ctx.span_id, "016x")


# ---------------------------------------------------------------------------
# SDK initialisation
# ---------------------------------------------------------------------------
_initialised = False


def init_tracing(*, service_name: str = "calorch") -> bool:
    """Initialise the OpenTelemetry SDK with OTLP HTTP exporter.

    Returns True if SDK was initialised, False if OTel is not installed
    or ``OTEL_EXPORTER_OTLP_ENDPOINT`` is not set.

    Idempotent — calling twice is a no-op.
    """
    global _initialised
    if _initialised:
        return True
    if not _OTEL_AVAILABLE:
        return False
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # No exporter configured; spans go to in-memory and are discarded
        _initialised = True
        return False

    try:
        from opentelemetry import trace as _trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        _trace.set_tracer_provider(provider)
        _initialised = True
        return True
    except Exception:
        # If anything goes wrong, fall back to no-op rather than crash the app
        _initialised = True
        return False


# ---------------------------------------------------------------------------
# Auto-instrumentation
# ---------------------------------------------------------------------------
def instrument_httpx() -> bool:
    """Patch httpx to emit CLIENT spans. Returns True if successful."""
    if not _OTEL_AVAILABLE:
        return False
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        return True
    except Exception:
        return False


def instrument_fastapi(app: Any) -> bool:
    """Patch a FastAPI app to emit SERVER spans. Returns True if successful."""
    if not _OTEL_AVAILABLE:
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        return True
    except Exception:
        return False


def is_otel_available() -> bool:
    """True if the opentelemetry-api package is importable."""
    return _OTEL_AVAILABLE
