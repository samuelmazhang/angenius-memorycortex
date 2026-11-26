# Cortex v6.5.0 observability utilities.
# Phase 2 (方向 #6) -- FastAPI OpenTelemetry integration.
#
# Layout (mirrors server/src/graph/, server/src/consolidate/):
#   config.py           -- OtelConfig dataclass + env loader (no OTel imports)
#   tracing.py          -- TracerProvider + exporter factory + start_span
#   metrics.py          -- MeterProvider + request_count / duration / active
#   instrumentation.py  -- setup_observability(app) -- one-call FastAPI wire-up
#
# Defaults: OTEL_ENABLED=false (no overhead unless ops opts in). All entry
# points degrade to no-ops when the SDK is missing, so slim venvs (no
# opentelemetry-*) continue to work.

from .config import (
    OtelConfig,
    VALID_EXPORTERS,
    load_otel_config,
)
from .instrumentation import setup_observability, current_request_span
from .metrics import (
    captured_memory_metrics,
    is_metrics_active,
    metrics_available,
    metrics_import_error,
    record_active,
    record_request,
    setup_metrics,
    shutdown_metrics,
)
from .tracing import (
    captured_memory_spans,
    get_tracer,
    is_tracing_active,
    otel_available,
    otel_import_error,
    reset_memory_exporter,
    setup_tracing,
    shutdown_tracing,
    start_span,
)

__all__ = [
    # config
    "OtelConfig",
    "VALID_EXPORTERS",
    "load_otel_config",
    # setup
    "setup_observability",
    "setup_tracing",
    "setup_metrics",
    "shutdown_tracing",
    "shutdown_metrics",
    "current_request_span",
    # capability probes
    "otel_available",
    "otel_import_error",
    "metrics_available",
    "metrics_import_error",
    # runtime checks
    "is_tracing_active",
    "is_metrics_active",
    # instrumentation helpers
    "get_tracer",
    "start_span",
    "record_request",
    "record_active",
    # test helpers
    "reset_memory_exporter",
    "captured_memory_metrics",
    "captured_memory_spans",
]
