"""OpenTelemetry tracing — distributed traces across Python agent ↔ Java backends.

HOW IT WORKS:
1. Python agent creates a root span for each chat request
2. When calling Java backends (mcp_client.py), trace-id is propagated via HTTP headers
3. Java backends pick up the trace-id and create child spans
4. All spans are exported to a collector (Jaeger, OTLP, or console)

WHAT YOU GET:
- End-to-end latency breakdown: user request → agent → LLM → tools → Java backend
- Per-tool timing: which tool calls are slow?
- Cross-service correlation: follow a single request across Python + Java

GRACEFUL DEGRADATION:
- If no OTLP collector is configured, uses NoOpTracer (zero overhead)
- Same pattern as Kafka: try to init, fall back to no-op

Usage:
    from app.tracing import get_tracer, inject_trace_headers
    tracer = get_tracer()
    with tracer.start_as_current_span("my_operation") as span:
        span.set_attribute("tool.name", "file_read")
        headers = inject_trace_headers({})  # propagate to downstream
"""

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _init_tracing():
    """Initialize OpenTelemetry tracing. Returns (tracer, propagator) or (NoOp, None)."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.propagators.composite import CompositePropagator
        from opentelemetry.trace.propagation import TraceContextTextMapPropagator
        from opentelemetry.baggage.propagation import W3CBaggagePropagator

        from app.config import settings
        otlp_endpoint = getattr(settings, "otlp_endpoint", "")

        resource = Resource.create({
            "service.name": "agent-server",
            "service.version": "0.1.0",
            "deployment.environment": "development",
        })

        provider = TracerProvider(resource=resource)

        if otlp_endpoint:
            # Export to OTLP collector (Jaeger, Tempo, etc.)
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
                otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
                provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
                logger.info("OTLP tracing enabled → %s", otlp_endpoint)
            except ImportError:
                logger.info("OTLP exporter not installed, using console exporter")
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        else:
            # Console exporter for development (logs spans to stdout)
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            logger.info("Tracing enabled (console exporter — set AGENT_OTLP_ENDPOINT for remote)")

        trace.set_tracer_provider(provider)

        # W3C Trace Context propagation (trace-id in HTTP headers)
        propagator = CompositePropagator([
            TraceContextTextMapPropagator(),
            W3CBaggagePropagator(),
        ])
        set_global_textmap(propagator)

        return trace.get_tracer("agent-server"), True

    except ImportError:
        logger.debug("opentelemetry not installed — tracing disabled")
        return None, False
    except Exception as e:
        logger.warning("Failed to initialize tracing: %s", e)
        return None, False


def get_tracer():
    """Get the OpenTelemetry tracer (or a no-op dummy)."""
    tracer, enabled = _init_tracing()
    if tracer is not None:
        return tracer
    # Return a no-op tracer that does nothing
    try:
        from opentelemetry import trace
        return trace.get_tracer("agent-server")
    except ImportError:
        return _NoOpTracer()


def inject_trace_headers(headers: dict) -> dict:
    """Inject W3C trace context into HTTP headers for downstream propagation."""
    try:
        from opentelemetry import context
        from opentelemetry.propagate import inject
        inject(headers, context=context.get_current())
    except ImportError:
        pass
    return headers


class _NoOpTracer:
    """Dummy tracer when opentelemetry is not installed."""
    def start_as_current_span(self, name, **kwargs):
        return _NoOpSpan()


class _NoOpSpan:
    """Dummy span context manager."""
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def set_attribute(self, key, value):
        pass
    def set_status(self, status):
        pass
    def record_exception(self, exc):
        pass
