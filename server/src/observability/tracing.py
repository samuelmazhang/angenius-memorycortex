"""V6.5.0 Phase 2 direction #6 -- OpenTelemetry tracing setup.

Sets up a :class:`TracerProvider` plus the exporter selected via
``OTEL_EXPORTER``. The whole module is import-safe even when
``opentelemetry-*`` packages are not installed -- every entry point
short-circuits to a no-op tracer when ``otel_available()`` returns
False, so production deployments that never install the OTel SDKs are
unaffected (the existing 116-test suite must continue to pass on
slim venvs).

Highlights
----------

* Single public function: :func:`setup_tracing`.
* :func:`otel_available` -- capability probe (cached).
* :func:`get_tracer` -- returns a no-op tracer when OTel is disabled.
* :func:`shutdown_tracing` -- flushes spans on FastAPI shutdown.
* :data:`memory_exporter` -- module-global handle for the in-memory
  exporter used by tests (``OTEL_EXPORTER=memory``).
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator, List, Optional

from .config import OtelConfig, load_otel_config

_LOG = logging.getLogger("cortex.observability.tracing")

# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------

_OTEL_IMPORT_ERROR: Optional[BaseException] = None
_OTEL_AVAILABLE: Optional[bool] = None
_OTEL_LOCK = threading.Lock()


def otel_available() -> bool:
    """Return True if ``opentelemetry-*`` packages are importable.

    Result is cached after the first call. The probe is intentionally
    permissive -- we only require the trace API + SDK + one exporter
    family to be present. We do NOT require metrics / logs to be
    available to call ourselves "OTel ready"; if those fail at setup
    time the relevant code paths log a warning and continue with the
    trace side armed.
    """
    global _OTEL_AVAILABLE, _OTEL_IMPORT_ERROR
    if _OTEL_AVAILABLE is not None:
        return _OTEL_AVAILABLE
    with _OTEL_LOCK:
        if _OTEL_AVAILABLE is not None:
            return _OTEL_AVAILABLE
        try:
            import opentelemetry  # noqa: F401
            import opentelemetry.sdk.trace  # noqa: F401
            import opentelemetry.sdk.resources  # noqa: F401
            from opentelemetry.sdk.trace.export import (  # noqa: F401
                BatchSpanProcessor,
                ConsoleSpanExporter,
                SimpleSpanProcessor,
            )
            # The InMemorySpanExporter lives in a sub-module -- probe it
            # too so the OTEL_EXPORTER=memory path works.
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: F401
                InMemorySpanExporter,
            )
            _OTEL_AVAILABLE = True
        except Exception as exc:  # pragma: no cover -- depends on env
            _OTEL_AVAILABLE = False
            _OTEL_IMPORT_ERROR = exc
            _LOG.info(
                "OpenTelemetry SDK not importable (%s); tracing is no-op",
                exc.__class__.__name__,
            )
    return _OTEL_AVAILABLE


def otel_import_error() -> Optional[BaseException]:
    """Return the cached import error from :func:`otel_available`.

    Used by the test suite to assert that the cold path is genuinely
    robust when the SDK is missing.
    """
    return _OTEL_IMPORT_ERROR


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# The InMemorySpanExporter is a module global so tests can read it.
memory_exporter = None  # type: ignore[var-annotated]

# Active tracer provider handle -- used by shutdown / reconfigure.
_active_provider = None  # type: ignore[var-annotated]
_active_exporter = None  # type: ignore[var-annotated]
_setup_lock = threading.Lock()


def is_tracing_active() -> bool:
    """True if setup_tracing() has armed a tracer provider."""
    return _active_provider is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_tracing(
    cfg: Optional[OtelConfig] = None,
    service_version: str = "6.5.0",
) -> bool:
    """Arm a tracer provider based on the given config.

    Returns True on success, False otherwise (logged at WARNING).
    Re-calling this with a different exporter flushes the old provider
    first -- the function is idempotent.
    """
    global _active_provider, _active_exporter, memory_exporter
    if cfg is None:
        cfg = load_otel_config(service_version=service_version)
    if not cfg.enabled:
        _LOG.info("OTEL_ENABLED=false; tracing setup skipped (no-op)")
        return False
    if not otel_available():
        _LOG.warning(
            "OTEL_ENABLED=true but opentelemetry SDK is not importable; "
            "tracing stays disabled. Install with: pip install "
            "opentelemetry-api opentelemetry-sdk"
        )
        return False

    with _setup_lock:
        # Tear down any previous provider.
        shutdown_tracing()

        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import (
                BatchSpanProcessor,
                ConsoleSpanExporter,
                SimpleSpanProcessor,
            )

            resource = Resource.create(
                {
                    "service.name": cfg.service_name,
                    "service.version": cfg.service_version,
                    "service.namespace": "memory-cortex",
                    "deployment.environment": _safe_deployment_env(),
                }
            )
            provider = TracerProvider(resource=resource)

            if cfg.exporter_is_otlp:
                try:
                    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                        OTLPSpanExporter,
                    )

                    exporter = OTLPSpanExporter(
                        endpoint=cfg.otlp_endpoint, insecure=True
                    )
                    provider.add_span_processor(
                        BatchSpanProcessor(exporter)
                    )
                    _LOG.info(
                        "OTel tracing armed: OTLP exporter -> %s",
                        cfg.otlp_endpoint,
                    )
                except Exception as exc:
                    _LOG.warning(
                        "OTLP exporter unavailable (%s); falling back to console",
                        exc.__class__.__name__,
                    )
                    provider.add_span_processor(
                        BatchSpanProcessor(ConsoleSpanExporter())
                    )
            elif cfg.exporter_is_console:
                provider.add_span_processor(
                    BatchSpanProcessor(ConsoleSpanExporter())
                )
                _LOG.info("OTel tracing armed: console exporter")
            elif cfg.exporter_is_memory:
                try:
                    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
                        InMemorySpanExporter,
                    )

                    exporter = InMemorySpanExporter()
                    memory_exporter = exporter  # expose to tests
                    provider.add_span_processor(
                        SimpleSpanProcessor(exporter)
                    )
                    _LOG.info("OTel tracing armed: InMemorySpanExporter (test mode)")
                except Exception as exc:
                    _LOG.warning(
                        "InMemorySpanExporter unavailable (%s); no exporter wired",
                        exc.__class__.__name__,
                    )
            else:
                _LOG.info("OTEL_EXPORTER=none; tracer provider armed without exporter")

            trace.set_tracer_provider(provider)
            _active_provider = provider
            return True
        except Exception as exc:
            _LOG.warning(
                "Failed to set up OpenTelemetry tracing: %s; staying no-op",
                exc,
            )
            return False


def shutdown_tracing() -> None:
    """Flush + tear down the active tracer provider (idempotent)."""
    global _active_provider, _active_exporter, memory_exporter
    if _active_provider is None:
        return
    try:
        _active_provider.shutdown()
    except Exception:  # pragma: no cover -- best-effort cleanup
        pass
    _active_provider = None
    _active_exporter = None
    memory_exporter = None


def get_tracer(name: str = "cortex"):
    """Return a tracer.

    Returns a NoOp tracer when OTel is disabled OR the SDK is not
    installed -- callers can use the returned object unconditionally
    and pay zero cost in the disabled case.
    """
    if not otel_available():
        return _noop_tracer(name)
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except Exception:  # pragma: no cover -- defensive
        return _noop_tracer(name)


@contextmanager
def start_span(name: str, attributes: Optional[dict] = None, **attrs) -> Iterator:
    """Context manager around ``tracer.start_as_current_span``.

    ``attributes`` may be a dict (preferred -- mirrors the OTel SDK
    signature). Extra keyword arguments are also accepted and applied
    one-by-one to the span via ``set_attribute``.

    Falls back to a no-op context when OTel is disabled or the SDK is
    not installed. Attribute writes are best-effort -- non-scalar
    values are silently dropped so the hot path never raises.
    """
    tracer = get_tracer()
    if not otel_available() or tracer is _NOOP:
        yield _noop_span()
        return
    with tracer.start_as_current_span(name) as span:
        # Apply the attributes dict first (preferred shape).
        if attributes:
            for k, v in attributes.items():
                try:
                    span.set_attribute(k, v)
                except Exception:  # pragma: no cover -- attribute conversion
                    pass
        # Then any leftover kwargs (mostly for ad-hoc callers).
        for k, v in attrs.items():
            try:
                span.set_attribute(k, v)
            except Exception:  # pragma: no cover -- attribute conversion
                pass
        yield span


def captured_memory_spans() -> List:
    """Return captured spans from the in-memory exporter (test helper).

    Returns an empty list when OTel is disabled, the SDK is missing,
    ``OTEL_EXPORTER`` is not ``memory``, or no spans have been emitted
    yet. The returned list is the *current* snapshot -- mutations made
    by the exporter after this call are NOT reflected.
    """
    if memory_exporter is None:
        return []
    try:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        if isinstance(memory_exporter, InMemorySpanExporter):
            return list(memory_exporter.get_finished_spans())
    except Exception:  # pragma: no cover -- defensive
        return []
    return []


def reset_memory_exporter() -> None:
    """Drop any spans already captured (test helper)."""
    if memory_exporter is None:
        return
    try:
        memory_exporter.clear()
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _safe_deployment_env() -> str:
    import os

    return (
        os.getenv("DEPLOYMENT_ENV")
        or os.getenv("ENV")
        or os.getenv("CORTEX_ENV")
        or "development"
    )


class _NoOpSpan:
    """Minimal stand-in for an OTel span when the SDK is unavailable."""

    def set_attribute(self, key, value):  # noqa: D401 -- simple shim
        return None

    def set_status(self, *args, **kwargs):
        return None

    def record_exception(self, *args, **kwargs):
        return None

    def end(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _NoOpTracer:
    def __init__(self, name: str):
        self.name = name

    def start_as_current_span(self, name, **kwargs):  # noqa: D401 -- shim
        return _noop_span()

    def start_span(self, name, **kwargs):
        return _noop_span()


def _noop_span():
    return _NoOpSpan()


_NOOP = _NoOpTracer("noop")
_NOOP_TRACER = _NOOP
