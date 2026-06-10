"""Shared HTTP client with retry logic, connection pooling, and circuit breaker.

Enterprise-grade HTTP client for all external API calls. Provides:
  * Connection pooling (reuse httpx.Client instances)
  * Retry logic with exponential backoff (tenacity)
  * Circuit breaker pattern (fail fast when service is down)
  * Structured logging with request/response details
  * Request ID correlation for distributed tracing
  * Metrics collection (request count, latency, error rate)

Usage:
    from calorch.http_client import get_client
    
    client = get_client()
    response = client.get("https://api.example.com/data")
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Generator

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------
@dataclass
class CircuitBreaker:
    """Circuit breaker pattern for external service calls.
    
    States:
      * CLOSED: Normal operation, requests pass through
      * OPEN: Service is down, requests fail immediately
      * HALF_OPEN: Testing if service has recovered
    
    Transitions:
      * CLOSED → OPEN: After ``failure_threshold`` consecutive failures
      * OPEN → HALF_OPEN: After ``recovery_timeout`` seconds
      * HALF_OPEN → CLOSED: After successful request
      * HALF_OPEN → OPEN: After failed request
    """
    
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    state: str = "CLOSED"
    failure_count: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    
    def can_execute(self) -> bool:
        """Check if request can proceed."""
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if time.monotonic() - self.last_failure_time >= self.recovery_timeout:
                self.state = "HALF_OPEN"
                logger.info("Circuit breaker: OPEN → HALF_OPEN (testing recovery)")
                return True
            return False
        if self.state == "HALF_OPEN":
            return True
        return False
    
    def record_success(self) -> None:
        """Record successful request."""
        self.failure_count = 0
        self.last_success_time = time.monotonic()
        if self.state == "HALF_OPEN":
            self.state = "CLOSED"
            logger.info("Circuit breaker: HALF_OPEN → CLOSED (service recovered)")
    
    def record_failure(self) -> None:
        """Record failed request."""
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning(
                f"Circuit breaker: → OPEN (threshold {self.failure_threshold} reached)"
            )


# Global circuit breakers per service
_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(service: str) -> CircuitBreaker:
    """Get or create circuit breaker for a service."""
    if service not in _circuit_breakers:
        _circuit_breakers[service] = CircuitBreaker()
    return _circuit_breakers[service]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
@dataclass
class RequestMetrics:
    """Request metrics collection."""
    
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_latency_ms: float = 0.0
    request_count_by_service: dict[str, int] = field(default_factory=dict)
    error_count_by_service: dict[str, int] = field(default_factory=dict)
    
    def record_request(self, service: str, latency_ms: float, success: bool) -> None:
        """Record a request."""
        self.total_requests += 1
        self.total_latency_ms += latency_ms
        self.request_count_by_service[service] = self.request_count_by_service.get(service, 0) + 1
        
        if success:
            self.successful_requests += 1
        else:
            self.failed_requests += 1
            self.error_count_by_service[service] = self.error_count_by_service.get(service, 0) + 1
    
    def get_stats(self) -> dict[str, Any]:
        """Get metrics summary."""
        avg_latency = self.total_latency_ms / self.total_requests if self.total_requests > 0 else 0.0
        success_rate = self.successful_requests / self.total_requests if self.total_requests > 0 else 0.0
        
        return {
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "success_rate": success_rate,
            "avg_latency_ms": avg_latency,
            "requests_by_service": self.request_count_by_service,
            "errors_by_service": self.error_count_by_service,
        }


# Global metrics
_metrics = RequestMetrics()


def get_metrics() -> RequestMetrics:
    """Get global metrics instance."""
    return _metrics


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------
class HttpClient:
    """Enterprise-grade HTTP client with retry, pooling, and circuit breaker."""
    
    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        pool_connections: int = 10,
        pool_maxsize: int = 20,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        
        # Create httpx.Client with connection pooling
        self._client = httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=pool_maxsize,
                max_keepalive_connections=pool_connections,
            ),
        )
        
        # Create retry decorator with dynamic max_retries
        self._retry_decorator = retry(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
    
    def close(self) -> None:
        """Close the HTTP client and release resources."""
        self._client.close()
    
    @contextmanager
    def request_context(
        self,
        service: str,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Generator[httpx.Response, None, None]:
        """Context manager for HTTP requests with retry, circuit breaker, and metrics."""
        
        # Generate request ID for correlation
        request_id = kwargs.pop("request_id", None) or str(uuid.uuid4())
        
        # Check circuit breaker
        cb = get_circuit_breaker(service)
        if not cb.can_execute():
            logger.warning(f"Circuit breaker OPEN for {service}, failing fast")
            raise httpx.RequestError(f"Circuit breaker OPEN for {service}")
        
        # Add request ID to headers
        headers = kwargs.get("headers", {})
        headers["X-Request-ID"] = request_id
        kwargs["headers"] = headers
        
        # Log request
        logger.debug(
            f"HTTP {method} {url}",
            extra={
                "request_id": request_id,
                "service": service,
                "method": method,
                "url": url,
            },
        )
        
        # Execute with retry
        start_time = time.monotonic()
        success = False

        # Wrap the call in an OpenTelemetry span (no-op if OTel not installed)
        from calorch.telemetry import start_span
        with start_span(
            f"calorch.http.{service}",
            method=method,
            url=url,
            request_id=request_id,
        ) as span:
            try:
                response = self._request_with_retry(method, url, **kwargs)
                success = True
                cb.record_success()
                span.set_attribute("http.status_code", response.status_code)
                yield response
            except (httpx.HTTPError, httpx.InvalidURL, ConnectionError, TimeoutError, OSError) as e:
                cb.record_failure()
                span.set_attribute("error", str(e))
                logger.error(
                    f"HTTP {method} {url} failed: {e}",
                    extra={
                        "request_id": request_id,
                        "service": service,
                        "error": str(e),
                    },
                    exc_info=True,
                )
                raise
            finally:
                # Record metrics
                latency_ms = (time.monotonic() - start_time) * 1000
                _metrics.record_request(service, latency_ms, success)
                span.set_attribute("latency_ms", latency_ms)

                logger.debug(
                    f"HTTP {method} {url} completed in {latency_ms:.1f}ms",
                    extra={
                        "request_id": request_id,
                        "service": service,
                        "latency_ms": latency_ms,
                        "success": success,
                    },
                )
    
    def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute HTTP request with retry logic."""
        # Apply the retry decorator dynamically
        @self._retry_decorator
        def _do_request() -> httpx.Response:
            response = self._client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        
        return _do_request()
    
    def get(self, url: str, *, service: str = "default", **kwargs: Any) -> httpx.Response:
        """HTTP GET request."""
        with self.request_context(service, "GET", url, **kwargs) as response:
            return response
    
    def post(self, url: str, *, service: str = "default", **kwargs: Any) -> httpx.Response:
        """HTTP POST request."""
        with self.request_context(service, "POST", url, **kwargs) as response:
            return response


# Global HTTP client instance
_http_client: HttpClient | None = None


def get_client() -> HttpClient:
    """Get or create global HTTP client instance."""
    global _http_client
    if _http_client is None:
        _http_client = HttpClient()
    return _http_client


def close_client() -> None:
    """Close global HTTP client."""
    global _http_client
    if _http_client is not None:
        _http_client.close()
        _http_client = None
