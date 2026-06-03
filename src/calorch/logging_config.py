"""Structured JSON logging with request ID correlation and PII redaction.

Production logging for calorch. Emits one JSON object per log line to stdout,
so Azure Container Apps log streaming / Log Analytics / Datadog can ingest
records without a custom parser.

Features:
  * JSON formatter (timestamp, level, logger, message, request_id, run_id,
    thread_id, exception, extra fields)
  * Request ID and run ID propagation through ``contextvars`` so any code
    path — sync, async, graph node, HTTP client — can stamp its logs
  * PII redaction (emails, account numbers, SSN-like patterns, calendar
    body content) before the line is emitted
  * Opt-in via ``LOG_FORMAT=json`` env var; default is text for local dev
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import socket
import sys
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Correlation IDs
# ---------------------------------------------------------------------------
_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "calorch_request_id", default=None
)
_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "calorch_run_id", default=None
)
_thread_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "calorch_thread_id", default=None
)


def set_request_id(request_id: str | None = None) -> str:
    """Set the current request ID (or generate one). Returns the value."""
    rid = request_id or str(uuid.uuid4())
    _request_id_var.set(rid)
    return rid


def get_request_id() -> str | None:
    return _request_id_var.get()


def set_run_id(run_id: str | None) -> None:
    _run_id_var.set(run_id)


def get_run_id() -> str | None:
    return _run_id_var.get()


def set_thread_id(thread_id: str | None) -> None:
    _thread_id_var.set(thread_id)


def get_thread_id() -> str | None:
    return _thread_id_var.get()


def clear_correlation() -> None:
    """Clear all correlation IDs (call between requests in tests)."""
    _request_id_var.set(None)
    _run_id_var.set(None)
    _thread_id_var.set(None)


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
_ACCOUNT_RE = re.compile(r"\b\d{8,17}\b")  # bank account / routing numbers
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")
_API_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password)[\"'\s:=]+([A-Za-z0-9._\-]{8,})")

# Calendar body PII — only used when message key is 'body' or 'body_preview'
_BODY_KEYS = frozenset({"body", "body_preview", "bodyPreview", "description"})

_REDACTION = "[REDACTED]"


def _redact_string(s: str) -> str:
    """Redact PII from a free-form string."""
    s = _EMAIL_RE.sub(_REDACTION, s)
    s = _SSN_RE.sub(_REDACTION, s)
    s = _CARD_RE.sub(_REDACTION, s)
    s = _PHONE_RE.sub(_REDACTION, s)
    s = _BEARER_RE.sub(rf"\1{_REDACTION}", s)
    s = _API_KEY_RE.sub(rf"\1={_REDACTION}", s)
    return s


def _redact_value(v: Any, *, in_body: bool = False) -> Any:
    """Recursively redact PII from a log value (dict / list / str)."""
    if isinstance(v, str):
        if in_body and len(v) > 200:
            # Long body text — redact aggressively, keep first 80 chars
            return _redact_string(v[:80]) + "…[truncated]"
        return _redact_string(v)
    if isinstance(v, dict):
        return {
            k: _redact_value(val, in_body=(k in _BODY_KEYS))
            for k, val in v.items()
        }
    if isinstance(v, (list, tuple)):
        return [_redact_value(item, in_body=in_body) for item in v]
    return v


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
_RESERVED_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line."""

    def __init__(self, *, hostname: str | None = None) -> None:
        super().__init__()
        self._hostname = hostname or socket.gethostname()

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "hostname": self._hostname,
        }

        rid = get_request_id()
        if rid:
            payload["request_id"] = rid
        run_id = get_run_id()
        if run_id:
            payload["run_id"] = run_id
        thread_id = get_thread_id()
        if thread_id:
            payload["thread_id"] = thread_id

        # Custom fields passed via `extra=`
        for k, v in record.__dict__.items():
            if k in _RESERVED_ATTRS or k.startswith("_"):
                continue
            payload[k] = _redact_value(v)

        if record.exc_info:
            payload["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": "".join(traceback.format_exception(*record.exc_info)),
            }

        # Redact PII in the message itself
        payload["message"] = _redact_string(payload["message"])

        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            # Last-resort fallback
            return json.dumps({
                "ts": payload["ts"],
                "level": payload["level"],
                "logger": payload["logger"],
                "message": "[unserialisable log record]",
            })


class TextFormatter(logging.Formatter):
    """Human-readable formatter for local dev."""

    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s",
            defaults={"request_id": "-"},
        )

    def format(self, record: logging.LogRecord) -> str:
        rid = get_request_id()
        if not hasattr(record, "request_id") or rid:
            record.request_id = rid or "-"
        msg = super().format(record)
        return _redact_string(msg)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_configured = False


def configure_logging(
    *,
    level: str | int | None = None,
    fmt: str | None = None,
) -> None:
    """Configure root logger. Idempotent.

    Args:
        level: log level (defaults to ``LOG_LEVEL`` env var or INFO).
        fmt: ``"json"`` or ``"text"`` (defaults to ``LOG_FORMAT`` env var or text).
    """
    global _configured
    if _configured:
        return

    level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or os.getenv("LOG_FORMAT") or "text").lower()

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter())

    root = logging.getLogger()
    # Remove any existing handlers (e.g. uvicorn's default)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Get a logger with calorch's default config applied."""
    configure_logging()
    return logging.getLogger(name)
