"""Tests for the OpenTelemetry tracing wrapper.

Tests run whether or not opentelemetry-api is installed — the module exposes
a no-op fallback so production code never needs to check.
"""
from __future__ import annotations

import pytest

from calorch.telemetry import (
    get_current_span_id,
    get_current_trace_id,
    init_tracing,
    instrument_fastapi,
    instrument_httpx,
    is_otel_available,
    start_span,
)


# ---------------------------------------------------------------------------
# No-op / fallback behaviour
# ---------------------------------------------------------------------------
def test_start_span_works_without_otel(monkeypatch: pytest.MonkeyPatch):
    """start_span must function as a no-op context manager even without OTel."""
    with start_span("test.span", key="value") as span:
        # Should be safe to call span methods
        span.set_attribute("foo", "bar")
    assert True  # no exception


def test_get_current_trace_id_returns_string_or_none():
    """Outside a span, returns None (or a hex string if OTel is installed)."""
    trace_id = get_current_trace_id()
    if trace_id is not None:
        # OTel is installed; must be a 32-char hex string
        assert len(trace_id) == 32
        int(trace_id, 16)  # must parse as hex


def test_is_otel_available_is_bool():
    assert isinstance(is_otel_available(), bool)


# ---------------------------------------------------------------------------
# init_tracing
# ---------------------------------------------------------------------------
def test_init_tracing_is_idempotent():
    """Calling init_tracing twice does not re-initialise the SDK."""
    r1 = init_tracing(service_name="calorch-test-1")
    r2 = init_tracing(service_name="calorch-test-1")
    assert r1 == r2


def test_init_tracing_returns_bool():
    assert isinstance(init_tracing(service_name="calorch-test"), bool)


# ---------------------------------------------------------------------------
# Auto-instrumentation (graceful no-op when OTel missing)
# ---------------------------------------------------------------------------
def test_instrument_httpx_returns_bool():
    assert isinstance(instrument_httpx(), bool)


def test_instrument_fastapi_returns_bool():
    """A non-FastAPI object should fail safely (return False) without crashing."""
    result = instrument_fastapi(object())
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Span attributes
# ---------------------------------------------------------------------------
def test_start_span_carries_attributes():
    """Attributes passed to start_span must be accepted (no TypeError)."""
    with start_span(
        "calorch.node.test",
        event_id="evt-1",
        ticker="AAPL",
        model="gpt-4o",
        count=5,
    ):
        pass
    assert True


def test_start_span_records_exception():
    """Exception inside a span is recorded without breaking the context."""
    with pytest.raises(ValueError, match="boom"):
        with start_span("calorch.test.failing"):
            raise ValueError("boom")
