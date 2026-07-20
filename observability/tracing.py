"""
OpenTelemetry instrumentation for distributed tracing.

Sends traces to Tempo (OTLP gRPC) when available. Gracefully no-ops
when OTel exporter is not configured or unreachable.

Usage:
  from observability.tracing import init_tracing, instrument_app
  init_tracing()  # call once at startup
  instrument_app(app)  # call after FastAPI app is created
"""
from __future__ import annotations

import logging
import os

from loguru import logger

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False
    logger.warning("OpenTelemetry not installed, tracing disabled")


_initialized = False


def init_tracing(
    service_name: str = "stock-analysis",
    otlp_endpoint: str = None,
) -> bool:
    """Initialize OpenTelemetry tracing.

    Args:
        service_name: name of the service in traces
        otlp_endpoint: OTLP gRPC endpoint, e.g. "tempo:4317"
                      If None, reads OTEL_EXPORTER_OTLP_ENDPOINT env var.
                      If still None, no exporter is configured (tracing still works but no remote export).

    Returns True if tracing was set up.
    """
    global _initialized
    if _initialized:
        return True
    if not HAS_OTEL:
        return False

    otlp_endpoint = otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not otlp_endpoint:
        logger.info("No OTLP endpoint configured, tracing in-process only")
        return True  # still init in-process tracer

    try:
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _initialized = True
        logger.info(f"OTel tracing initialized, exporting to {otlp_endpoint}")
        return True
    except Exception as e:
        logger.warning(f"OTel init failed: {e}")
        return False


def instrument_app(app) -> bool:
    """Instrument FastAPI app with automatic tracing.

    Returns True if instrumentation was applied.
    """
    if not HAS_OTEL:
        return False
    if not _initialized:
        return False
    try:
        FastAPIInstrumentor.instrument_app(app)
        logger.info("FastAPI instrumented with OTel")
        return True
    except Exception as e:
        logger.warning(f"FastAPI instrumentation failed: {e}")
        return False


def instrument_requests() -> bool:
    """Instrument requests library (used by httpx-like calls)."""
    if not HAS_OTEL:
        return False
    try:
        RequestsInstrumentor().instrument()
        return True
    except Exception:
        return False


def get_tracer(name: str = "stock-analysis"):
    """Get a tracer for manual instrumentation."""
    if HAS_OTEL:
        return trace.get_tracer(name)
    return None


def add_span_attributes(span, **attrs):
    """Add attributes to current span if OTel is available."""
    if HAS_OTEL:
        for k, v in attrs.items():
            span.set_attribute(k, v)