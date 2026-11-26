"""V6.5.0 Phase 2 direction #6 -- FastAPI instrumentation glue.

This module is the single place where OTel is *attached* to the FastAPI
app. It exposes a ``setup_observability(app)`` function that:

1. Reads the env once (idempotent).
2. If OTel is disabled, returns immediately -- the FastAPI app is
   untouched and there is zero per-request overhead.
3. If OTel is enabled but the SDK is missing, logs a warning and
   returns -- never raises.
4. If OTel is enabled and the SDK is present, wires:

   * Tracing: a custom ASGI middleware that creates a span per
     request, sets ``http.method`` / ``http.route`` / ``http.status_code``
     / ``http.target``, and propagates context. This avoids the
     ``opentelemetry-instrumentation-fastapi`` dependency (which we
     do not want to force onto slim venvs).
   * Metrics: the ``record_request`` / ``record_active`` helpers.

The custom middleware is intentionally tiny -- ~40 lines. We *deliberately*
do not pull in ``opentelemetry-instrumentation-fastapi`` because:

* It is a heavy dependency (~200KB) that needs to be aligned with the
  exact OTel API version, otherwise we get duplicate spans.
* The 105-endpoint cortex surface is small enough that a hand-rolled
  middleware is more predictable and easier to debug.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .config import OtelConfig, load_otel_config
from .metrics import (
    is_metrics_active,
    record_active,
    record_request,
    setup_metrics,
    shutdown_metrics,
)
from .tracing import (
    get_tracer,
    is_tracing_active,
    otel_available,
    setup_tracing,
    shutdown_tracing,
    start_span,
    _NOOP_TRACER,
)

_LOG = logging.getLogger("cortex.observability.instrumentation")


def setup_observability(
    app,
    cfg: Optional[OtelConfig] = None,
    service_version: str = "6.5.0",
) -> bool:
    """One-call setup for FastAPI observability.

    Returns True if observability is wired up. Returns False when
    disabled or when the SDK is unavailable (caller does not need to
    branch on the return value).
    """
    if cfg is None:
        cfg = load_otel_config(service_version=service_version)

    if not cfg.enabled:
        _LOG.info("Observability disabled (OTEL_ENABLED=false); no-op")
        return False

    if not otel_available():
        _LOG.warning(
            "Observability requested but OTel SDK is not importable; "
            "install 'opentelemetry-api opentelemetry-sdk' to enable."
        )
        return False

    tracing_ok = setup_tracing(cfg=cfg, service_version=service_version)
    metrics_ok = False
    if cfg.metrics_enabled:
        metrics_ok = setup_metrics(cfg=cfg, service_version=service_version)

    if tracing_ok or metrics_ok:
        _install_middleware(app, cfg)
        _install_shutdown_hooks(app)
        _LOG.info(
            "Observability wired (tracing=%s, metrics=%s, exporter=%s)",
            tracing_ok,
            metrics_ok,
            cfg.exporter,
        )
        return True

    _LOG.info("Observability not wired (both tracing + metrics setup failed)")
    return False


def _install_middleware(app, cfg: OtelConfig) -> None:
    """Install the request-scoped ASGI middleware (only once)."""
    # Idempotency: the marker attribute lets us avoid double-install
    # when setup_observability is called twice (e.g., from tests).
    if getattr(app.state, "_cortex_otel_installed", False):
        return
    app.state._cortex_otel_installed = True

    @app.middleware("http")
    async def _otel_http_middleware(request, call_next):
        route = _route_template(request)
        method = request.method
        start = time.perf_counter()
        record_active(1, route=route)
        tracer = get_tracer()
        span = None
        if is_tracing_active() and tracer is not _NOOP_TRACER:
            try:
                span = tracer.start_span(
                    f"{method} {route or request.url.path}",
                    attributes={
                        "http.method": method,
                        "http.route": route or request.url.path,
                        "http.target": request.url.path,
                        "http.scheme": request.url.scheme,
                        "net.host.name": request.url.hostname or "",
                    },
                )
                span.__enter__()
            except Exception:
                span = None
        try:
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000.0
            if span is not None:
                try:
                    span.set_attribute("http.status_code", int(response.status_code))
                except Exception:
                    pass
            record_request(
                method=method,
                route=route or request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
            return response
        except Exception as exc:  # pragma: no cover -- defensive
            duration_ms = (time.perf_counter() - start) * 1000.0
            if span is not None:
                try:
                    span.set_attribute("http.status_code", 500)
                    span.set_attribute("error.type", exc.__class__.__name__)
                except Exception:
                    pass
            record_request(
                method=method,
                route=route or request.url.path,
                status_code=500,
                duration_ms=duration_ms,
                extra_attrs={
                    "error": True,
                    "error.type": exc.__class__.__name__,
                },
            )
            raise
        finally:
            if span is not None:
                try:
                    span.__exit__(None, None, None)
                except Exception:
                    pass
            record_active(-1, route=route)


def _install_shutdown_hooks(app) -> None:
    """Register FastAPI shutdown events that flush OTel."""
    if getattr(app.state, "_cortex_otel_shutdown_installed", False):
        return
    app.state._cortex_otel_shutdown_installed = True

    @app.on_event("shutdown")
    def _otel_shutdown():  # pragma: no cover -- event-driven
        shutdown_tracing()
        shutdown_metrics()


def _route_template(request) -> str:
    """Best-effort FastAPI route template lookup for the given request.

    Returns ``""`` (empty string) when no matching route is found.
    The middleware then falls back to ``request.url.path``.
    """
    try:
        route = request.scope.get("route")
        if route is not None:
            return getattr(route, "path", "") or ""
    except Exception:
        return ""
    return ""


def current_request_span():
    """Return the active span (or a no-op stand-in) for ad-hoc attributes.

    Callers should not retain the return value -- it may be a NoOpSpan
    when OTel is disabled. Safe to call from request handlers.
    """
    tracer = get_tracer()
    if not is_tracing_active():
        return None
    try:
        from opentelemetry import trace as _trace_mod

        span = _trace_mod.get_current_span()
        return span if span is not None else None
    except Exception:  # pragma: no cover
        return None
