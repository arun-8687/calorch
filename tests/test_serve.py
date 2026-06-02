"""Service contract tests for draft runs and resumable send approval."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

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

    resumed = service.approve_run(paused.thread_id, service.ApprovalRequest(approved=True))
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
