"""Tests for shared HTTP client with retry, circuit breaker, and metrics."""
from __future__ import annotations

import time
from unittest.mock import Mock, patch

import httpx
import pytest

from calorch.http_client import (
    CircuitBreaker,
    HttpClient,
    RequestMetrics,
    close_client,
    get_client,
    get_circuit_breaker,
    get_metrics,
)


# ---------------------------------------------------------------------------
# Circuit Breaker Tests
# ---------------------------------------------------------------------------
def test_circuit_breaker_starts_closed():
    cb = CircuitBreaker()
    assert cb.state == "CLOSED"
    assert cb.can_execute() is True


def test_circuit_breaker_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=3)
    assert cb.state == "CLOSED"
    
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "CLOSED"
    
    cb.record_failure()
    assert cb.state == "OPEN"
    assert cb.can_execute() is False


def test_circuit_breaker_half_open_after_timeout():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "OPEN"
    
    time.sleep(0.15)
    assert cb.can_execute() is True
    assert cb.state == "HALF_OPEN"


def test_circuit_breaker_closes_on_success():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "OPEN"
    
    time.sleep(0.15)
    cb.can_execute()  # Transitions to HALF_OPEN
    assert cb.state == "HALF_OPEN"
    
    cb.record_success()
    assert cb.state == "CLOSED"


def test_circuit_breaker_reopens_on_failure():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "OPEN"
    
    time.sleep(0.15)
    cb.can_execute()  # Transitions to HALF_OPEN
    assert cb.state == "HALF_OPEN"
    
    cb.record_failure()
    assert cb.state == "OPEN"


def test_get_circuit_breaker_creates_new():
    cb1 = get_circuit_breaker("test_service_1")
    cb2 = get_circuit_breaker("test_service_2")
    assert cb1 is not cb2
    
    cb1_again = get_circuit_breaker("test_service_1")
    assert cb1 is cb1_again


# ---------------------------------------------------------------------------
# Metrics Tests
# ---------------------------------------------------------------------------
def test_metrics_record_request():
    metrics = RequestMetrics()
    metrics.record_request("test_service", 100.0, success=True)
    
    assert metrics.total_requests == 1
    assert metrics.successful_requests == 1
    assert metrics.failed_requests == 0
    assert metrics.total_latency_ms == 100.0
    assert metrics.request_count_by_service["test_service"] == 1


def test_metrics_record_failure():
    metrics = RequestMetrics()
    metrics.record_request("test_service", 50.0, success=False)
    
    assert metrics.total_requests == 1
    assert metrics.successful_requests == 0
    assert metrics.failed_requests == 1
    assert metrics.error_count_by_service["test_service"] == 1


def test_metrics_get_stats():
    metrics = RequestMetrics()
    metrics.record_request("service_a", 100.0, success=True)
    metrics.record_request("service_a", 200.0, success=True)
    metrics.record_request("service_b", 50.0, success=False)
    
    stats = metrics.get_stats()
    assert stats["total_requests"] == 3
    assert stats["successful_requests"] == 2
    assert stats["failed_requests"] == 1
    assert stats["success_rate"] == pytest.approx(2/3)
    assert stats["avg_latency_ms"] == pytest.approx(350.0 / 3)
    assert stats["requests_by_service"]["service_a"] == 2
    assert stats["requests_by_service"]["service_b"] == 1


# ---------------------------------------------------------------------------
# HTTP Client Tests
# ---------------------------------------------------------------------------
def test_http_client_get_success():
    client = HttpClient()
    
    with patch.object(client._client, "request") as mock_request:
        mock_response = Mock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response
        
        response = client.get("https://api.example.com/data", service="test")
        
        assert response == mock_response
        mock_request.assert_called_once()
        assert "X-Request-ID" in mock_request.call_args[1]["headers"]


def test_http_client_retry_on_timeout():
    client = HttpClient(max_retries=3)
    
    with patch.object(client._client, "request") as mock_request:
        mock_request.side_effect = [
            httpx.TimeoutException("timeout"),
            httpx.TimeoutException("timeout"),
            Mock(spec=httpx.Response, status_code=200, raise_for_status=Mock()),
        ]
        
        response = client.get("https://api.example.com/data", service="test")
        
        assert response.status_code == 200
        assert mock_request.call_count == 3


def test_http_client_circuit_breaker_integration():
    # Disable retries to test circuit breaker behavior in isolation
    client = HttpClient(max_retries=1)
    cb = get_circuit_breaker("test_cb_service")
    cb.failure_threshold = 2
    
    with patch.object(client._client, "request") as mock_request:
        mock_request.side_effect = httpx.NetworkError("network error")
        
        # First two requests fail, circuit opens
        with pytest.raises(httpx.NetworkError):
            client.get("https://api.example.com/data", service="test_cb_service")
        with pytest.raises(httpx.NetworkError):
            client.get("https://api.example.com/data", service="test_cb_service")
        
        assert cb.state == "OPEN"
        
        # Third request fails fast (circuit open)
        with pytest.raises(httpx.RequestError, match="Circuit breaker OPEN"):
            client.get("https://api.example.com/data", service="test_cb_service")
        
        # Only 2 actual requests made (third failed fast)
        assert mock_request.call_count == 2


def test_http_client_metrics_integration():
    client = HttpClient()
    metrics = get_metrics()
    initial_count = metrics.total_requests
    
    with patch.object(client._client, "request") as mock_request:
        mock_response = Mock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response
        
        client.get("https://api.example.com/data", service="metrics_test")
        
        assert metrics.total_requests == initial_count + 1
        assert metrics.successful_requests >= 1
        assert "metrics_test" in metrics.request_count_by_service


def test_get_client_singleton():
    client1 = get_client()
    client2 = get_client()
    assert client1 is client2
    
    close_client()
    client3 = get_client()
    assert client3 is not client1


def test_http_client_close():
    client = HttpClient()
    client.close()
    # Should not raise
