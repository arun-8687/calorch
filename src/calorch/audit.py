"""Append-only audit log for approval decisions and run lifecycle.

Every approval, rejection, and run lifecycle event is written to a JSONL file
so financial-compliance auditors can reconstruct who-did-what-when.

Format (one JSON object per line):
    {"ts": "...", "event": "approval", "thread_id": "...", "decision": "approved",
     "actor": "api-key-id", "request_id": "..."}

Default location: ``./out/audit.jsonl``. Override with ``AUDIT_LOG_PATH`` env var.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from calorch.logging_config import get_request_id, get_run_id


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class AuditLog:
    """Thread-safe append-only JSONL audit log."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, payload: dict[str, Any]) -> None:
        """Append a single JSON line. Best-effort: log but never raise on the hot path."""
        from calorch.logging_config import get_logger
        log = get_logger("calorch.audit")
        payload.setdefault("ts", _now_iso())
        payload.setdefault("request_id", get_request_id())
        payload.setdefault("run_id", get_run_id())
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, default=str, ensure_ascii=False))
                f.write("\n")
        except OSError as e:
            log.error("Failed to write audit log entry: %s", e, extra={"audit_payload": payload})

    def run_started(self, thread_id: str, window_start: str, window_end: str, send_emails: bool) -> None:
        self._append({
            "event": "run_started",
            "thread_id": thread_id,
            "window_start": window_start,
            "window_end": window_end,
            "send_emails": send_emails,
            "actor": _api_key_id(),
        })

    def run_completed(self, thread_id: str, status: str, events: int, errors: list[str]) -> None:
        self._append({
            "event": "run_completed",
            "thread_id": thread_id,
            "status": status,
            "events": events,
            "error_count": len(errors),
            "actor": _api_key_id(),
        })

    def run_failed(self, thread_id: str, error: str) -> None:
        self._append({
            "event": "run_failed",
            "thread_id": thread_id,
            "error": error,
            "actor": _api_key_id(),
        })

    def approval_decision(self, thread_id: str, approved: bool, reason: str = "") -> None:
        self._append({
            "event": "approval_decision",
            "thread_id": thread_id,
            "decision": "approved" if approved else "rejected",
            "reason": reason,
            "actor": _api_key_id(),
        })

    def run_timeout(self, thread_id: str, timeout_seconds: float) -> None:
        self._append({
            "event": "run_timeout",
            "thread_id": thread_id,
            "timeout_seconds": timeout_seconds,
            "actor": _api_key_id(),
        })

    def rate_limited(self, endpoint: str, caller: str) -> None:
        self._append({
            "event": "rate_limited",
            "endpoint": endpoint,
            "actor": caller,
        })

    def read(self) -> list[dict[str, Any]]:
        """Read all entries (for compliance export)."""
        if not self._path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def _api_key_id() -> str:
    """Best-effort actor identifier from the env (CALORCH_ACTOR_ID or 'anonymous')."""
    return os.getenv("CALORCH_ACTOR_ID", "anonymous")


# Global audit log instance
_audit: AuditLog | None = None


def get_audit_log() -> AuditLog:
    """Get or create the global audit log instance."""
    global _audit
    if _audit is None:
        from calorch.config import get_settings
        _audit = AuditLog(get_settings().audit_log_path)
    return _audit


def reset_audit_log() -> None:
    """Reset for tests."""
    global _audit
    _audit = None
