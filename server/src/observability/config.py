"""V6.5.0 Phase 2 direction #6 — OpenTelemetry configuration.

Reads OpenTelemetry-related environment variables and exposes a typed
``OtelConfig`` dataclass. The values are intentionally isolated from
``server/src/config.py`` so that our cold-path ``CortexConfig._load()``
does not need to be touched (and so that the package can be loaded
without going through the heavier config singleton).

Defaults match the spec:

* ``OTEL_ENABLED`` defaults to **false** -- zero overhead unless an
  operator opts in. This is the hard "no surprise cost" rule of the
  Phase 2 #6 brief.
* ``OTEL_EXPORTER`` defaults to ``"console"`` for safe local debugging.
  In production, ops set ``OTEL_EXPORTER=otlp`` and point
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` at their collector.
* Sampling defaults to 1.0 (always-on). For higher-traffic deployments
  ops should lower ``OTEL_SAMPLE_RATIO`` (e.g., 0.1).

The dataclass is intentionally minimal -- it does NOT pull in any
OpenTelemetry packages. That way the config can be imported even on a
slim runtime that has not installed ``opentelemetry-*`` yet (which is
the default in this repo's ``requirements.txt``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# Supported exporter kinds. Anything else is treated as "none".
VALID_EXPORTERS = ("otlp", "console", "memory", "none")


@dataclass(frozen=True)
class OtelConfig:
    """Typed view over the OpenTelemetry env vars.

    Frozen so callers cannot mutate the global singleton by accident --
    re-parse via :func:`load_otel_config` if the env changes mid-run.
    """

    enabled: bool
    service_name: str
    service_version: str
    exporter: str
    otlp_endpoint: str
    sample_ratio: float
    metrics_enabled: bool
    log_correlation: bool
    capture_headers: bool
    excluded_urls: tuple  # of str

    @property
    def exporter_is_valid(self) -> bool:
        return self.exporter in VALID_EXPORTERS

    @property
    def exporter_is_otlp(self) -> bool:
        return self.exporter == "otlp"

    @property
    def exporter_is_console(self) -> bool:
        return self.exporter == "console"

    @property
    def exporter_is_memory(self) -> bool:
        # "memory" is reserved for tests / dev -- InMemorySpanExporter
        # captured into a module-global list.
        return self.exporter == "memory"

    def as_attributes(self) -> dict:
        """Dict form for inclusion in resource attributes."""
        return {
            "otel.enabled": str(self.enabled).lower(),
            "otel.exporter": self.exporter,
            "otel.service_name": self.service_name,
            "otel.service_version": self.service_version,
            "otel.sample_ratio": str(self.sample_ratio),
            "otel.metrics_enabled": str(self.metrics_enabled).lower(),
        }


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_tuple(name: str, default: tuple = ()) -> tuple:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def load_otel_config(
    service_version: str = "6.5.0",
) -> OtelConfig:
    """Build an :class:`OtelConfig` from the current process env.

    Parameters
    ----------
    service_version:
        Version string baked into the OTel resource. Defaults to
        cortex's current 6.5.0; ``api_pg`` passes its own value so the
        resource reflects the running app.
    """
    exporter_raw = os.getenv("OTEL_EXPORTER", "console").strip().lower()
    if exporter_raw not in VALID_EXPORTERS:
        exporter_raw = "none"

    return OtelConfig(
        enabled=_env_bool("OTEL_ENABLED", "false"),
        service_name=os.getenv("OTEL_SERVICE_NAME", "memory-cortex").strip()
        or "memory-cortex",
        service_version=service_version,
        exporter=exporter_raw,
        otlp_endpoint=os.getenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
        ).strip() or "http://localhost:4317",
        sample_ratio=_env_float("OTEL_SAMPLE_RATIO", 1.0),
        metrics_enabled=_env_bool("OTEL_METRICS_ENABLED", "true"),
        log_correlation=_env_bool("OTEL_LOG_CORRELATION", "true"),
        capture_headers=_env_bool("OTEL_CAPTURE_HEADERS", "false"),
        excluded_urls=_env_tuple(
            "OTEL_EXCLUDED_URLS",
            ("/health", "/metrics", "/static", "/favicon.ico"),
        ),
    )
