"""OpenTelemetry middleware — automatic request tracing + Prometheus metrics.

Wraps every incoming request in an OTel span with tenant/session attributes.
Exposes /metrics endpoint for Prometheus scraping.

Metrics exported:
- agent_requests_total (counter): by tenant, model, status
- agent_request_duration_seconds (histogram): by tenant, model
- agent_tokens_total (counter): by tenant, model, type (prompt/completion)
"""

import time
import logging
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.tracing import get_tracer, inject_trace_headers

logger = logging.getLogger(__name__)

# ── Prometheus-style metrics (in-memory counters) ─────────────
# For production, replace with prometheus_client or export to OTLP metrics.

_metrics: dict[str, dict[str, float]] = {
    "requests_total": {},       # key: "tenant:model:status"
    "request_duration_sum": {}, # key: "tenant:path"
    "request_duration_count": {},
}


def record_request_metric(tenant_id: str, path: str, status: int, duration_ms: float):
    """Record request metrics (thread-safe in asyncio single-thread model)."""
    req_key = f"{tenant_id}:{path}:{status}"
    _metrics["requests_total"][req_key] = _metrics["requests_total"].get(req_key, 0) + 1

    dur_key = f"{tenant_id}:{path}"
    _metrics["request_duration_sum"][dur_key] = _metrics["request_duration_sum"].get(dur_key, 0) + duration_ms
    _metrics["request_duration_count"][dur_key] = _metrics["request_duration_count"].get(dur_key, 0) + 1


def get_metrics_summary() -> dict:
    """Get metrics summary for /metrics endpoint."""
    result = {}
    for dur_key, total in _metrics["request_duration_sum"].items():
        count = _metrics["request_duration_count"].get(dur_key, 1)
        result[dur_key] = {
            "count": int(count),
            "avg_ms": round(total / count, 1) if count > 0 else 0,
            "total_ms": round(total, 1),
        }
    return {
        "requests": _metrics["requests_total"],
        "latency": result,
    }


class TracingMiddleware(BaseHTTPMiddleware):
    """Adds OpenTelemetry spans and metrics to every request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip health checks and static files
        path = request.url.path
        if path in ("/health", "/metrics") or path.startswith("/ui/"):
            return await call_next(request)

        tracer = get_tracer()
        tenant_id = getattr(request.state, "tenant_id", "anonymous")
        start = time.monotonic()

        with tracer.start_as_current_span(
            f"{request.method} {path}",
            attributes={
                "http.method": request.method,
                "http.url": str(request.url),
                "http.route": path,
                "tenant.id": tenant_id,
            },
        ) as span:
            try:
                response = await call_next(request)
                span.set_attribute("http.status_code", response.status_code)
                return response
            except Exception as e:
                span.record_exception(e)
                span.set_attribute("http.status_code", 500)
                raise
            finally:
                duration_ms = (time.monotonic() - start) * 1000
                span.set_attribute("http.duration_ms", duration_ms)
                status = getattr(response, "status_code", 500) if "response" in dir() else 500
                record_request_metric(tenant_id, path, status, duration_ms)
