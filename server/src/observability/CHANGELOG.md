# Changelog -- `server/src/observability/`

Cortex v6.5.0 Phase 2 observability utilities (方向 #6). OpenTelemetry
tracing + metrics for the FastAPI surface, with a **feature-flagged**
opt-in (default off).

## 2026-08-23 -- v6.5.0 Phase 2 方向 #6 (FastAPI OpenTelemetry)

**Step 1 of 1 in the observability track.** Adds a thin OpenTelemetry
integration tuned for cortex's 105-endpoint FastAPI surface. The
expected effect (per PRD §11.1, ROI 7) is **+30% debug throughput**
(distributed traces + per-request metrics for the 1-2 most painful
endpoints, e.g. ``/search`` PPR + ``/consolidate/run``).

### Trigger conditions (per the #6 brief)

| Condition | Action |
|---|---|
| `OTEL_ENABLED=false` (default) | `setup_observability(app)` is a no-op. The FastAPI app is untouched; no middleware is installed; there is zero per-request overhead. |
| `OTEL_ENABLED=true` and SDK importable | Arm a tracer provider + meter provider and install the request middleware. |
| `OTEL_ENABLED=true` but SDK missing | Log a WARNING once at startup and stay no-op. (Avoids breaking slim venvs that never install OTel.) |

### Module surface

| Symbol | Kind | Description |
|---|---|---|
| `OtelConfig` | dataclass | Typed view over the OTel env vars. No OTel imports. |
| `load_otel_config(service_version=...)` | function | Parse env vars into an `OtelConfig`. |
| `setup_observability(app, cfg=None, ...)` | function | **One-call wire-up**: tracing + metrics + FastAPI middleware + shutdown hooks. |
| `setup_tracing(cfg=None, ...)` | function | Tracer-provider setup only. |
| `setup_metrics(cfg=None, ...)` | function | Meter-provider setup only. |
| `shutdown_tracing()` / `shutdown_metrics()` | function | Flush + tear down. |
| `is_tracing_active()` / `is_metrics_active()` | function | Runtime capability checks. |
| `otel_available()` / `metrics_available()` | function | Capability probes (cached). |
| `get_tracer(name)` | function | Returns a real tracer when active, a NoOpTracer otherwise. |
| `start_span(name, **attrs)` | function | Context-manager around `tracer.start_as_current_span`. |
| `record_request(method, route, status, ms, ...)` | function | One HTTP observation. |
| `record_active(delta, route)` | function | Active-request gauge increment / decrement. |
| `current_request_span()` | function | Active span for ad-hoc attribute setting. |
| `reset_memory_exporter()` | function | Test helper. |

### Public env vars

| Var | Default | Purpose |
|---|---|---|
| `OTEL_ENABLED` | **false** | Master switch. Cold-path default off. |
| `OTEL_EXPORTER` | `console` | `console` / `otlp` / `memory` (test) / `none`. |
| `OTEL_SERVICE_NAME` | `memory-cortex` | Resource attribute. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | Used by both trace + metric OTLP exporters. |
| `OTEL_SAMPLE_RATIO` | `1.0` | Head-based sampling ratio. |
| `OTEL_METRICS_ENABLED` | `true` | Allow opting metrics off independently of traces. |
| `OTEL_LOG_CORRELATION` | `true` | Reserve slot for trace-id log enrichment (Phase 2 stretch). |
| `OTEL_CAPTURE_HEADERS` | `false` | Capture request headers as span attributes (PII risk). |
| `OTEL_EXCLUDED_URLS` | `/health,/metrics,/static,/favicon.ico` | Comma-separated URL prefixes excluded from spans. |

### What gets captured per request

* Span name: ``"<METHOD> <ROUTE_TEMPLATE>"`` (e.g., ``"POST /search"``).
* Span attributes: ``http.method``, ``http.route``, ``http.target``,
  ``http.scheme``, ``net.host.name``, ``http.status_code``.
* Metrics (counter + histogram): ``http.server.request.count`` and
  ``http.server.duration`` tagged by ``http.method`` / ``http.route`` /
  ``http.status_code``.
* Active-request gauge: ``http.server.active_requests`` tagged by route.

### Design constraints honored

- **Default off (zero overhead).** The middleware is only installed
  when ``OTEL_ENABLED=true``. There is no per-request check.
- **No hard dependency on OTel.** All OTel imports are wrapped in
  capability probes; the existing 116-test regression suite runs on
  slim venvs without OTel installed.
- **No third-party FastAPI auto-instrumentation.** Hand-rolled 40-line
  ASGI middleware avoids the version-locked
  ``opentelemetry-instrumentation-fastapi`` dependency and the
  duplicate-span footgun. Tracing for psycopg/httpx can be added by
  Ops in a follow-up (out of #6 scope).
- **Graceful degradation.** When the SDK is missing but the operator
  has flipped the flag on, we log a single WARNING and stay no-op
  rather than crashing the FastAPI startup.
- **Test-friendly.** ``OTEL_EXPORTER=memory`` wires the OTel SDK's
  InMemorySpanExporter + InMemoryMetricReader, captured into module
  globals so the test suite can introspect spans and metrics without
  hitting a collector.

### Files added in this drop

| File | Status | Purpose |
|---|---|---|
| `server/src/observability/__init__.py` | new | Public API re-exports. |
| `server/src/observability/config.py` | new | `OtelConfig` + `load_otel_config`. |
| `server/src/observability/tracing.py` | new | TracerProvider setup + no-op fallbacks. |
| `server/src/observability/metrics.py` | new | MeterProvider + instruments. |
| `server/src/observability/instrumentation.py` | new | `setup_observability(app)` FastAPI glue. |
| `server/src/observability/CHANGELOG.md` | new | This file. |
| `server/src/api_pg.py` | updated | Calls `setup_observability(app)` after CORS. |
| `server/src/config.py` | updated | Mirror OTel flags into the CortexConfig singleton. |
| `server/tests/test_otel_v6_5.py` | new | 4 dispatch-mandated tests + helpers. |
| `.env.example` | updated | Documents the OTel env vars. |
| `CHANGELOG.md` (root) | updated | v6.5.0 #6 entry. |
| `.sprint/v6.5.0-phase-2-direction-6-report.md` | new | Sprint completion report. |

### Files NOT touched (per dispatch constraint)

- `server/src/graph/*` (owned by #B).
- `server/src/consolidate/*` (owned by #A).
- `server/src/memory_taxonomy.py`.
- `server/src/etl/etl_reembed.py` (owned by #3).
- `tests/` at worktree root (per P31).

### Test coverage

The 4 required tests map as follows:

| # | Test | Spec point exercised |
|---|---|---|
| 1 | `test_otel_disabled_by_default` | `OTEL_ENABLED=false` -> `setup_observability` is a no-op, no middleware, no providers. |
| 2 | `test_otel_enabled_console_exporter` | `OTEL_ENABLED=true, OTEL_EXPORTER=console` -> tracer + meter providers armed. |
| 3 | `test_otel_tracing_captures_request_span` | `TestClient.get('/health')` -> an in-memory span is recorded with route + method + status. |
| 4 | `test_otel_metrics_record_request` | `TestClient.get('/health')` -> `http.server.request.count` increments by 1. |

Plus helper / regression tests for the config dataclass, the no-op
tracer shim, and the OTel capability probe.

### Open follow-ups (not in this drop)

- **psycopg + httpx auto-instrumentation**: requires
  `opentelemetry-instrumentation-psycopg2` + matching version pinning.
- **Structured-log bridge**: pipe trace_id / span_id into the cortex
  `logger` so logs can be correlated with traces (Phase 2 stretch).
- **Sampling at the call site**: today we always-on; route-aware
  sampling for ``/search`` / ``/consolidate/*`` can be added when
  traffic grows.
- **Production rollout**: flip ``OTEL_ENABLED=true`` and
  ``OTEL_EXPORTER=otlp`` in the deployment .env once a collector is
  stood up.
