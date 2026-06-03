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
