"""Tests for the append-only audit log."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from calorch.audit import AuditLog
from calorch.logging_config import clear_correlation, set_request_id, set_run_id


@pytest.fixture(autouse=True)
def _reset_correlation():
    clear_correlation()
    yield
    clear_correlation()


def test_writes_run_started(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.run_started(
        thread_id="run-abc",
        window_start="2026-03-01T00:00:00+00:00",
        window_end="2026-03-08T00:00:00+00:00",
        send_emails=False,
    )
    entries = log.read()
    assert len(entries) == 1
    e = entries[0]
    assert e["event"] == "run_started"
    assert e["thread_id"] == "run-abc"
    assert e["send_emails"] is False
    assert "ts" in e
    assert "request_id" in e  # from correlation context


def test_writes_approval_decision(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.approval_decision(thread_id="run-1", approved=True, reason="All clear")
    entries = log.read()
    assert entries[0]["decision"] == "approved"
    assert entries[0]["reason"] == "All clear"


def test_writes_rejection(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.approval_decision(thread_id="run-1", approved=False, reason="Risky")
    entries = log.read()
    assert entries[0]["decision"] == "rejected"


def test_writes_run_failed(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.run_failed(thread_id="run-1", error="LLM timeout")
    entries = log.read()
    assert entries[0]["event"] == "run_failed"
    assert entries[0]["error"] == "LLM timeout"


def test_writes_run_timeout(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.run_timeout(thread_id="run-1", timeout_seconds=300.0)
    entries = log.read()
    assert entries[0]["event"] == "run_timeout"
    assert entries[0]["timeout_seconds"] == 300.0


def test_writes_rate_limited(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.rate_limited(endpoint="/run", caller="key-abc12345")
    entries = log.read()
    assert entries[0]["event"] == "rate_limited"
    assert entries[0]["endpoint"] == "/run"


def test_writes_run_completed(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.run_completed(
        thread_id="run-1",
        status="complete",
        events=8,
        errors=[],
    )
    entries = log.read()
    assert entries[0]["event"] == "run_completed"
    assert entries[0]["events"] == 8
    assert entries[0]["status"] == "complete"


def test_correlation_ids_propagate(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    set_request_id("req-99")
    set_run_id("run-99")
    log.run_started(
        thread_id="run-99",
        window_start="2026-03-01T00:00:00+00:00",
        window_end="2026-03-08T00:00:00+00:00",
        send_emails=False,
    )
    entries = log.read()
    assert entries[0]["request_id"] == "req-99"
    assert entries[0]["run_id"] == "run-99"


def test_appends_multiple_entries(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.run_started("t1", "2026-01-01T00:00:00+00:00", "2026-01-08T00:00:00+00:00", False)
    log.approval_decision("t1", approved=True)
    log.run_completed("t1", "complete", 5, [])
    entries = log.read()
    assert len(entries) == 3
    assert [e["event"] for e in entries] == ["run_started", "approval_decision", "run_completed"]


def test_creates_parent_directory(tmp_path: Path):
    log = AuditLog(tmp_path / "deep" / "nested" / "audit.jsonl")
    log.run_started("t1", "2026-01-01T00:00:00+00:00", "2026-01-08T00:00:00+00:00", False)
    assert (tmp_path / "deep" / "nested" / "audit.jsonl").exists()


def test_read_handles_missing_file(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    # Don't write anything
    assert log.read() == []


def test_read_handles_corrupt_lines(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    p.write_text('{"event": "good"}\nnot-json\n{"event": "good2"}\n', encoding="utf-8")
    log = AuditLog(p)
    entries = log.read()
    assert len(entries) == 2
    assert entries[0]["event"] == "good"
    assert entries[1]["event"] == "good2"


def test_each_line_is_valid_json(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.run_started("t1", "2026-01-01T00:00:00+00:00", "2026-01-08T00:00:00+00:00", False)
    log.approval_decision("t1", approved=True, reason="looks good")
    log.run_completed("t1", "complete", 5, [])
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    for line in lines:
        json.loads(line)  # must not raise
