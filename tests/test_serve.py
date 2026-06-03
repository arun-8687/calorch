"""Service contract tests for draft runs and resumable send approval."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from calorch import serve
from calorch.config import get_settings


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("USE_MOCKS", "true")
    monkeypatch.setenv("USE_SEC", "false")
    monkeypatch.setenv("USE_FRED", "false")
    monkeypatch.setenv("USE_FED_H15", "false")
    monkeypatch.setenv("USE_IXBRL_SEGMENTS", "false")
    monkeypatch.setenv("USE_SEC_EFTS", "false")
    monkeypatch.setenv("REPO_BACKEND", "json")
    monkeypatch.setenv("REPO_PATH", str(tmp_path / "repo.json"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("CHECKPOINT_POSTGRES_URI", raising=False)
    monkeypatch.setattr(serve, "make_cik_lookup", lambda _settings: lambda _ticker: None)
    get_settings.cache_clear()
    serve._startup()
    yield serve
    serve._shutdown()


def _request(*, send_emails: bool, require_approval: bool = True) -> serve.RunRequest:
    return serve.RunRequest(
        start=datetime(2026, 3, 2, tzinfo=timezone.utc),
        end=datetime(2026, 3, 9, tzinfo=timezone.utc),
        send_emails=send_emails,
        require_approval=require_approval,
    )


def test_draft_run_completes_without_approval(service):
    response = service.run(_request(send_emails=False))
    assert response.status == "complete"
    assert response.events == 8


def test_send_run_pauses_and_approval_endpoint_resumes(service):
    paused = service.run(_request(send_emails=True))
    assert paused.status == "pending_approval"
    state = service.get_run(paused.thread_id)
    assert state["next"] == ["approval_gate"]

    resumed = service.approve_run(paused.thread_id, serve.ApprovalRequest(approved=True))
    assert resumed.status == "complete"
    state = service.get_run(paused.thread_id)
    assert state["values"]["approval_status"] == "approved"


def test_run_rejects_invalid_window(service):
    request = _request(send_emails=False)
    request.end = request.start
    with pytest.raises(HTTPException, match="end must be after start"):
        service.run(request)


def test_production_api_key_guard(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("USE_MOCKS", "false")
    monkeypatch.setenv("CALORCH_API_KEY", "secret")
    get_settings.cache_clear()
    with pytest.raises(HTTPException, match="invalid API key"):
        serve._require_api_key("wrong")
    serve._require_api_key("secret")


# ---------------------------------------------------------------------------
# Health and readiness probes
# ---------------------------------------------------------------------------
def test_health_endpoint(service):
    """Liveness probe always returns ok with timestamp."""
    client = TestClient(serve.app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "ts" in body


def test_ready_endpoint(service):
    """Readiness probe reports all dependencies initialised."""
    client = TestClient(serve.app)
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["graph"] is True
    assert body["checks"]["context"] is True
    assert body["checks"]["checkpointer"] is True
    assert "ts" in body


def test_security_headers_present(service):
    """All HTTP responses include security headers."""
    client = TestClient(serve.app)
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-XSS-Protection"] == "1; mode=block"
    assert "Strict-Transport-Security" in response.headers
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_metrics_endpoint(service):
    """Metrics endpoint returns HTTP client statistics."""
    client = TestClient(serve.app)
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.json()
    assert "total_requests" in body
    assert "success_rate" in body
    assert "avg_latency_ms" in body
    assert "requests_by_service" in body


def test_shutdown_closes_http_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Shutdown handler closes the shared HTTP client."""
    from calorch.http_client import get_client, close_client

    monkeypatch.setenv("USE_MOCKS", "true")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(serve, "make_cik_lookup", lambda _settings: lambda _ticker: None)
    get_settings.cache_clear()

    serve._startup()
    # Verify client was initialised
    assert get_client() is not None
    serve._shutdown()
    # After shutdown, global client is cleared
    from calorch import http_client
    assert http_client._http_client is None


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def test_run_request_rejects_window_exceeding_31_days():
    """Pydantic schema rejects windows longer than 31 days."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="at most 31 days"):
        serve.RunRequest(
            start=datetime(2026, 3, 1, tzinfo=timezone.utc),
            end=datetime(2026, 5, 1, tzinfo=timezone.utc),  # 61 days
            send_emails=False,
        )


def test_run_request_rejects_inverted_window_at_schema_level():
    """Schema validator catches end <= start before it reaches the route handler."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="end must be after start"):
        serve.RunRequest(
            start=datetime(2026, 3, 9, tzinfo=timezone.utc),
            end=datetime(2026, 3, 2, tzinfo=timezone.utc),
            send_emails=False,
        )


# ---------------------------------------------------------------------------
# Request size limit
# ---------------------------------------------------------------------------
def test_request_size_limit_rejects_oversized_body(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Content-Length > max_request_bytes gets a 413 before reaching the route."""
    monkeypatch.setenv("MAX_REQUEST_BYTES", "100")
    monkeypatch.setenv("USE_MOCKS", "true")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(serve, "make_cik_lookup", lambda _settings: lambda _ticker: None)
    get_settings.cache_clear()
    serve._startup()
    try:
        client = TestClient(serve.app)
        response = client.post(
            "/run",
            content=b"x" * 200,
            headers={
                "X-Calorch-API-Key": "test",
                "Content-Type": "application/json",
                "Content-Length": "200",
            },
        )
        assert response.status_code == 413
    finally:
        serve._shutdown()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_rate_limit_returns_429_after_threshold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Once the per-minute limit is hit, subsequent calls get 429."""
    from calorch import rate_limit
    rate_limit.reset_rate_limiter()
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "3")
    monkeypatch.setenv("USE_MOCKS", "true")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(serve, "make_cik_lookup", lambda _settings: lambda _ticker: None)
    get_settings.cache_clear()
    rate_limit.reset_rate_limiter()
    serve._startup()
    try:
        client = TestClient(serve.app)
        # First 3 calls should not be rate-limited (the call may 503 since graph is
        # set up but we only care that the response isn't 429)
        for _ in range(3):
            r = client.get("/runs/no-such-thread", headers={"X-Calorch-API-Key": "test-key"})
            assert r.status_code != 429
        # 4th call should hit rate limit
        r = client.get("/runs/no-such-thread", headers={"X-Calorch-API-Key": "test-key"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers
    finally:
        serve._shutdown()
        rate_limit.reset_rate_limiter()


def test_rate_limit_does_not_apply_to_health(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Health probe is exempt so the platform can poll freely."""
    from calorch import rate_limit
    rate_limit.reset_rate_limiter()
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("USE_MOCKS", "true")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(serve, "make_cik_lookup", lambda _settings: lambda _ticker: None)
    get_settings.cache_clear()
    rate_limit.reset_rate_limiter()
    serve._startup()
    try:
        client = TestClient(serve.app)
        for _ in range(10):
            r = client.get("/health")
            assert r.status_code == 200
    finally:
        serve._shutdown()
        rate_limit.reset_rate_limiter()


# ---------------------------------------------------------------------------
# Request ID correlation
# ---------------------------------------------------------------------------
def test_request_id_generated_when_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Middleware generates a request ID and echoes it in the response header."""
    monkeypatch.setenv("USE_MOCKS", "true")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(serve, "make_cik_lookup", lambda _settings: lambda _ticker: None)
    get_settings.cache_clear()
    serve._startup()
    try:
        client = TestClient(serve.app)
        response = client.get("/health")
        assert "X-Request-ID" in response.headers
        assert len(response.headers["X-Request-ID"]) > 0
    finally:
        serve._shutdown()


def test_request_id_echoed_from_inbound_header(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Inbound X-Request-ID is preserved end-to-end."""
    monkeypatch.setenv("USE_MOCKS", "true")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(serve, "make_cik_lookup", lambda _settings: lambda _ticker: None)
    get_settings.cache_clear()
    serve._startup()
    try:
        client = TestClient(serve.app)
        response = client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
        assert response.headers["X-Request-ID"] == "trace-abc-123"
    finally:
        serve._shutdown()


# ---------------------------------------------------------------------------
# Graph execution timeout
# ---------------------------------------------------------------------------
def test_invoke_with_timeout_raises_on_hang(monkeypatch: pytest.MonkeyPatch):
    """A hung graph invoke returns _GraphTimeout after the deadline."""
    import time

    class FakeGraph:
        def invoke(self, input, config=None):
            time.sleep(5.0)
            return {"ok": True}

    with pytest.raises(serve._GraphTimeout):
        serve._invoke_with_timeout(FakeGraph(), {}, {}, timeout_seconds=0.1)


def test_invoke_with_timeout_returns_normally_when_fast(monkeypatch: pytest.MonkeyPatch):
    """A fast graph invoke returns its result within the deadline."""
    class FakeGraph:
        def invoke(self, input, config=None):
            return {"events": [1, 2, 3]}

    result = serve._invoke_with_timeout(FakeGraph(), {}, {}, timeout_seconds=2.0)
    assert result == {"events": [1, 2, 3]}


# ---------------------------------------------------------------------------
# Audit log integration
# ---------------------------------------------------------------------------
def test_run_endpoint_writes_audit_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """POST /run produces run_started and run_completed audit entries."""
    from calorch import audit as audit_mod
    audit_mod.reset_audit_log()
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("USE_MOCKS", "true")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr(serve, "make_cik_lookup", lambda _settings: lambda _ticker: None)
    get_settings.cache_clear()
    audit_mod.reset_audit_log()
    serve._startup()
    try:
        client = TestClient(serve.app)
        body = {
            "start": "2026-03-02T00:00:00Z",
            "end": "2026-03-09T00:00:00Z",
            "send_emails": False,
        }
        response = client.post(
            "/run", json=body, headers={"X-Calorch-API-Key": "test"}
        )
        assert response.status_code == 200
        entries = audit_mod.get_audit_log().read()
        events = [e["event"] for e in entries]
        assert "run_started" in events
        assert "run_completed" in events
    finally:
        serve._shutdown()
        audit_mod.reset_audit_log()


def test_approval_endpoint_writes_audit_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """POST /runs/{id}/approval produces an approval_decision audit entry."""
    from calorch import audit as audit_mod
    audit_mod.reset_audit_log()
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("USE_MOCKS", "true")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr(serve, "make_cik_lookup", lambda _settings: lambda _ticker: None)
    get_settings.cache_clear()
    audit_mod.reset_audit_log()
    serve._startup()
    try:
        client = TestClient(serve.app)
        # First, start a run that pauses for approval
        body = {
            "start": "2026-03-02T00:00:00Z",
            "end": "2026-03-09T00:00:00Z",
            "send_emails": True,
            "require_approval": True,
        }
        r1 = client.post("/run", json=body, headers={"X-Calorch-API-Key": "test"})
        assert r1.status_code == 200
        thread_id = r1.json()["thread_id"]
        # Now approve
        r2 = client.post(
            f"/runs/{thread_id}/approval",
            json={"approved": True, "reason": "All good"},
            headers={"X-Calorch-API-Key": "test"},
        )
        assert r2.status_code == 200
        entries = audit_mod.get_audit_log().read()
        decisions = [e for e in entries if e["event"] == "approval_decision"]
        assert len(decisions) >= 1
        assert decisions[0]["decision"] == "approved"
        assert decisions[0]["reason"] == "All good"
    finally:
        serve._shutdown()
        audit_mod.reset_audit_log()
