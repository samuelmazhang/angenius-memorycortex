"""V6.5.0 Phase 2 direction #6 -- OpenTelemetry metrics setup.

Sets up a :class:`MeterProvider` and registers three core HTTP
instruments (request count, latency histogram, active-requests gauge).
Like :mod:`tracing`, the module degrades gracefully when the OTel
metrics packages are not installed.

Public surface:

* :func:`setup_metrics`
* :func:`shutdown_metrics`
* :func:`is_metrics_active`
* :func:`record_request` -- record one HTTP request observation.
* :func:`record_active` -- increment/decrement active-request gauge.
* :data:`memory_reader` -- test-only handle to the in-memory exporter.
* :func:`captured_memory_metrics` -- test-only snapshot of observations.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterable, List, Optional

from .config import OtelConfig, load_otel_config

_LOG = logging.getLogger("cortex.observability.metrics")


# Capability probes (cached) --------------------------------------------------

_METRICS_AVAILABLE: Optional[bool] = None
_METRICS_IMPORT_ERROR: Optional[BaseException] = None
_METRICS_LOCK = threading.Lock()


def metrics_available() -> bool:
    """Return True if the OTel metrics SDK can be imported."""
    global _METRICS_AVAILABLE, _METRICS_IMPORT_ERROR
    if _METRICS_AVAILABLE is not None:
        return _METRICS_AVAILABLE
    with _METRICS_LOCK:
        if _METRICS_AVAILABLE is not None:
            return _METRICS_AVAILABLE
        try:
            from opentelemetry import metrics as _m  # noqa: F401
            import opentelemetry.sdk.metrics  # noqa: F401
            _METRICS_AVAILABLE = True
        except Exception as exc:  # pragma: no cover -- depends on env
            _METRICS_AVAILABLE = False
            _METRICS_IMPORT_ERROR = exc
            _LOG.info(
                "OpenTelemetry metrics SDK not importable (%s); metrics no-op",
                exc.__class__.__name__,
            )
    return _METRICS_AVAILABLE


def metrics_import_error() -> Optional[BaseException]:
    """Return the cached import error from :func:`metrics_available`."""
    return _METRICS_IMPORT_ERROR


# State ------------------------------------------------------------------------

_active_meter_provider = None
_active_meter = None
memory_reader = None  # type: ignore[var-annotated]
_setup_lock = threading.Lock()


def is_metrics_active() -> bool:
    return _active_meter_provider is not None


# Instruments ------------------------------------------------------------------

# Use module-global instrument handles so we create each only once.
_request_count = None
_request_duration_ms = None
_active_requests = None


# Public API -------------------------------------------------------------------


def setup_metrics(
    cfg: Optional[OtelConfig] = None,
    service_version: str = "6.5.0",
) -> bool:
    """Arm a meter provider based on the given config.

    Success: True. Failure (or disabled): False. The caller can chain
    safely -- failures are logged at WARNING but never raised.
    """
    global _active_meter_provider, _active_meter, memory_reader
    global _request_count, _request_duration_ms, _active_requests
    if cfg is None:
        cfg = load_otel_config(service_version=service_version)
    if not cfg.enabled or not cfg.metrics_enabled:
        _LOG.info("OTel metrics disabled (OTEL_ENABLED=%s)", cfg.enabled)
        return False
    if not metrics_available():
        _LOG.warning(
            "OTel metrics requested but SDK not importable; metrics no-op"
        )
        return False

    with _setup_lock:
        shutdown_metrics()
        try:
            from opentelemetry import metrics as _metrics_mod
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import (
                ConsoleMetricExporter,
                PeriodicExportingMetricReader,
            )
            from opentelemetry.sdk.resources import Resource

            resource = Resource.create(
                {
                    "service.name": cfg.service_name,
                    "service.version": cfg.service_version,
                }
            )

            readers: List = []
            if cfg.exporter_is_otlp:
                try:
                    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                        OTLPMetricExporter,
                    )

                    readers.append(
                        PeriodicExportingMetricReader(
                            OTLPMetricExporter(
                                endpoint=cfg.otlp_endpoint, insecure=True
                            ),
                            export_interval_millis=15_000,
                        )
                    )
                except Exception as exc:
                    _LOG.warning(
                        "OTLP metrics exporter unavailable (%s); falling back to console",
                        exc.__class__.__name__,
                    )
                    readers.append(
                        PeriodicExportingMetricReader(
                            ConsoleMetricExporter(),
                            export_interval_millis=15_000,
                        )
                    )
            elif cfg.exporter_is_console:
                readers.append(
                    PeriodicExportingMetricReader(
                        ConsoleMetricExporter(),
                        export_interval_millis=15_000,
                    )
                )
            elif cfg.exporter_is_memory:
                try:
                    from opentelemetry.sdk.metrics.export import (
                        InMemoryMetricReader,
                    )

                    reader = InMemoryMetricReader()
                    readers.append(reader)
                    memory_reader = reader  # expose to tests
                    _LOG.info("OTel metrics armed: InMemoryMetricReader")
                except Exception as exc:
                    _LOG.warning(
                        "InMemoryMetricReader unavailable (%s); no reader wired",
                        exc.__class__.__name__,
                    )
            # else: arm provider without exporter (just live telemetry)

            provider = MeterProvider(resource=resource, metric_readers=readers)
            _metrics_mod.set_meter_provider(provider)
            _active_meter_provider = provider
            _active_meter = _metrics_mod.get_meter(
                "cortex", version=service_version
            )

            _request_count = _active_meter.create_counter(
                "http.server.request.count",
                description="HTTP requests served by cortex",
                unit="1",
            )
            _request_duration_ms = _active_meter.create_histogram(
                "http.server.duration",
                description="HTTP request duration in milliseconds",
                unit="ms",
            )
            _active_requests = _active_meter.create_up_down_counter(
                "http.server.active_requests",
                description="In-flight HTTP requests",
                unit="1",
            )
            return True
        except Exception as exc:
            _LOG.warning(
                "Failed to set up OpenTelemetry metrics: %s; staying no-op",
                exc,
            )
            return False


def shutdown_metrics() -> None:
    """Flush + tear down the active meter provider (idempotent)."""
    global _active_meter_provider, _active_meter, memory_reader
    if _active_meter_provider is None:
        return
    try:
        _active_meter_provider.shutdown()
    except Exception:  # pragma: no cover -- best-effort cleanup
        pass
    _active_meter_provider = None
    _active_meter = None
    memory_reader = None


def record_request(
    method: str,
    route: str,
    status_code: int,
    duration_ms: float,
    extra_attrs: Optional[dict] = None,
) -> None:
    """Record a single HTTP request observation.

    No-op when OTel is disabled or metrics are not available. Catches
    every exception -- the hot path must never raise from instrumentation.
    """
    if not is_metrics_active():
        return
    attrs = {
        "http.method": method,
        "http.route": route or "/",
        "http.status_code": int(status_code),
    }
    if extra_attrs:
        attrs.update({k: v for k, v in extra_attrs.items() if v is not None})
    try:
        if _request_count is not None:
            _request_count.add(1, attributes=attrs)
        if _request_duration_ms is not None:
            _request_duration_ms.record(duration_ms, attributes=attrs)
    except Exception:  # pragma: no cover -- defensive
        pass


def record_active(delta: int, route: str = "/") -> None:
    """Increment (or decrement, with a negative delta) the active-request gauge."""
    if not is_metrics_active():
        return
    try:
        if _active_requests is not None:
            _active_requests.add(delta, attributes={"http.route": route or "/"})
    except Exception:  # pragma: no cover
        pass


def captured_memory_metrics() -> List:
    """Return the list of recorded metrics from the in-memory reader (test helper).

    Returns an empty list when OTel is disabled, the SDK is missing,
    ``OTEL_EXPORTER`` is not ``memory``, or the provider has not been
    set up.
    """
    if memory_reader is None:
        return []
    try:
        from opentelemetry.sdk.metrics.export import (
            InMemoryMetricReader,
        )

        if isinstance(memory_reader, InMemoryMetricReader):
            data = memory_reader.get_metrics_data()
            if data is None:
                return []
            out: List = []
            for rm in data.resource_metrics:
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        out.append(m)
            return out
    except Exception:  # pragma: no cover -- defensive
        return []
    return []


def reset_memory_metrics() -> None:
    """Drop recorded observations (test helper)."""
    # InMemoryMetricReader has no clear() in 1.21 -- callers should
    # simply re-set the provider (which is what the test suite does).
    pass


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------


def _now_ms() -> float:
    return time.perf_counter() * 1000.0
