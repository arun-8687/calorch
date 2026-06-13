"""Tests for structured JSON logging, PII redaction, and request ID correlation."""
from __future__ import annotations

import json
import logging
import re

import pytest

from calorch.logging_config import (
    JsonFormatter,
    TextFormatter,
    _redact_string,
    _redact_value,
    clear_correlation,
    configure_logging,
    get_logger,
    get_request_id,
    get_run_id,
    get_thread_id,
    set_request_id,
    set_run_id,
    set_thread_id,
)


@pytest.fixture(autouse=True)
def _reset_correlation():
    """Each test starts with no correlation IDs."""
    clear_correlation()
    yield
    clear_correlation()


# ---------------------------------------------------------------------------
# Correlation IDs
# ---------------------------------------------------------------------------
def test_request_id_round_trip():
    rid = set_request_id("req-123")
    assert rid == "req-123"
    assert get_request_id() == "req-123"


def test_request_id_generated_when_unset():
    rid = set_request_id()
    assert rid is not None
    assert re.match(r"^[0-9a-f-]{36}$", rid)  # UUID format
    assert get_request_id() == rid


def test_run_id_and_thread_id_round_trip():
    set_run_id("run-abc")
    set_thread_id("thread-xyz")
    assert get_run_id() == "run-abc"
    assert get_thread_id() == "thread-xyz"


def test_clear_correlation():
    set_request_id("rid")
    set_run_id("rid")
    set_thread_id("tid")
    clear_correlation()
    assert get_request_id() is None
    assert get_run_id() is None
    assert get_thread_id() is None


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------
def test_redact_string_strips_emails():
    out = _redact_string("Contact alice@example.com for details")
    assert "@" not in out
    assert "[REDACTED]" in out


def test_redact_string_strips_ssn():
    out = _redact_string("SSN 123-45-6789")
    assert "123-45-6789" not in out
    assert "[REDACTED]" in out


def test_redact_string_strips_phone_numbers():
    out = _redact_string("Call 415-555-1234")
    assert "415-555-1234" not in out
    assert "[REDACTED]" in out
    out2 = _redact_string("+1 (415) 555-1234")
    assert "415" not in out2 or "[REDACTED]" in out2


def test_redact_string_strips_bearer_tokens():
    out = _redact_string("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert "[REDACTED]" in out


def test_redact_string_strips_api_key_pairs():
    out = _redact_string('api_key=sk-12345678abcdefgh')
    assert "sk-12345678abcdefgh" not in out
    assert "[REDACTED]" in out


def test_redact_value_handles_dicts():
    out = _redact_value({
        "user": "alice@example.com",
        "balance": 1000,
        "nested": {"contact": "bob@example.com"},
    })
    assert out["user"] == "[REDACTED]"
    assert out["balance"] == 1000
    assert out["nested"]["contact"] == "[REDACTED]"


def test_redact_value_truncates_long_bodies():
    long_body = "Hello alice@example.com " + ("lorem ipsum " * 50)
    out = _redact_value({"body": long_body})
    assert len(out["body"]) < len(long_body)
    assert "alice@example.com" not in out["body"]


def test_redact_value_passes_through_non_strings():
    assert _redact_value(42) == 42
    assert _redact_value(None) is None
    assert _redact_value([1, 2, 3]) == [1, 2, 3]


# ---------------------------------------------------------------------------
# JsonFormatter
# ---------------------------------------------------------------------------
def _make_record(msg: str, level: int = logging.INFO, **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname="x.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_json_formatter_emits_valid_json():
    fmt = JsonFormatter()
    record = _make_record("hello world")
    out = fmt.format(record)
    payload = json.loads(out)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert "ts" in payload
    assert "hostname" in payload


def test_json_formatter_includes_correlation_ids():
    set_request_id("req-42")
    set_run_id("run-7")
    set_thread_id("thread-9")
    fmt = JsonFormatter()
    record = _make_record("hi")
    payload = json.loads(fmt.format(record))
    assert payload["request_id"] == "req-42"
    assert payload["run_id"] == "run-7"
    assert payload["thread_id"] == "thread-9"


def test_json_formatter_omits_unset_correlation_ids():
    fmt = JsonFormatter()
    record = _make_record("hi")
    payload = json.loads(fmt.format(record))
    assert "request_id" not in payload
    assert "run_id" not in payload
    assert "thread_id" not in payload


def test_json_formatter_redacts_pii_in_message():
    fmt = JsonFormatter()
    record = _make_record("Sent to alice@example.com")
    payload = json.loads(fmt.format(record))
    assert "alice@example.com" not in payload["message"]


def test_json_formatter_includes_exception():
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="t", level=logging.ERROR, pathname="x", lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(fmt.format(record))
    assert payload["exception"]["type"] == "ValueError"
    assert payload["exception"]["message"] == "boom"
    assert "ValueError" in payload["exception"]["traceback"]


def test_json_formatter_includes_extra_fields():
    fmt = JsonFormatter()
    record = _make_record("done", ticker="AAPL", count=5)
    payload = json.loads(fmt.format(record))
    assert payload["ticker"] == "AAPL"
    assert payload["count"] == 5


# ---------------------------------------------------------------------------
# TextFormatter
# ---------------------------------------------------------------------------
def test_text_formatter_includes_request_id():
    set_request_id("req-77")
    fmt = TextFormatter()
    record = _make_record("hello")
    out = fmt.format(record)
    assert "req-77" in out
    assert "hello" in out


def test_text_formatter_redacts_pii():
    fmt = TextFormatter()
    record = _make_record("Sent to alice@example.com")
    out = fmt.format(record)
    assert "alice@example.com" not in out


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------
def test_configure_logging_is_idempotent(monkeypatch):
    """Calling configure_logging twice should not double-configure handlers."""
    configure_logging(level="DEBUG", fmt="json")
    root = logging.getLogger()
    handlers_after_first = list(root.handlers)
    configure_logging(level="DEBUG", fmt="json")
    handlers_after_second = list(root.handlers)
    assert len(handlers_after_first) == len(handlers_after_second)


def test_get_logger_returns_configured_logger():
    log = get_logger("calorch.test")
    assert log.name == "calorch.test"
    assert log.level <= logging.INFO or logging.getLogger().level <= logging.INFO


def test_redact_string_strips_azure_connection_string_key():
    """SEC-8: AccountKey / SAS secrets in connection strings are redacted."""
    s = "DefaultEndpointsProtocol=https;AccountName=foo;AccountKey=AbCd1234XYZ+/secret==;EndpointSuffix=core.windows.net"
    out = _redact_string(s)
    assert "AbCd1234XYZ" not in out
    assert "[REDACTED]" in out
    sas = "https://x.blob.core.windows.net/c?sig=abc123DEF456ghiJKL789&se=2026"
    assert "abc123DEF456" not in _redact_string(sas)


def test_configure_logging_installs_redacting_json_handler(monkeypatch):
    """SEC-1: configure_logging(fmt=json) installs a single redacting handler."""
    import calorch.logging_config as lc

    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        monkeypatch.setattr(lc, "_configured", False)
        lc.configure_logging(fmt="json")
        assert len(root.handlers) == 1
        fmt = root.handlers[0].formatter
        assert isinstance(fmt, lc.JsonFormatter)
        rec = logging.LogRecord(
            "t", logging.INFO, __file__, 1, "contact a@b.com Bearer xyz12345", None, None
        )
        out = fmt.format(rec)
        assert "a@b.com" not in out and "xyz12345" not in out and "[REDACTED]" in out
    finally:
        root.handlers[:] = saved
        monkeypatch.setattr(lc, "_configured", True)
