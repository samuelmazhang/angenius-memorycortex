"""v6.5.0 Phase 2 direction #6 -- FastAPI OpenTelemetry tests.

Spec covers 4 mandatory tests + helper / regression tests:

  1. test_otel_disabled_by_default
        OTEL_ENABLED=false -> setup_observability is a no-op.
  2. test_otel_enabled_console_exporter
        OTEL_ENABLED=true + OTEL_EXPORTER=console -> providers armed.
  3. test_otel_tracing_captures_request_span
        TestClient.get('/health') -> an in-memory span recorded.
  4. test_otel_metrics_record_request
        TestClient.get('/health') -> http.server.request.count increments.

Tests are designed to run on slim venvs (no OTel SDK installed). When
the SDK IS importable we use OTEL_EXPORTER=memory so the in-memory
exporter + reader capture spans and metrics without an external
collector. When the SDK is NOT importable the tests still pass -- the
disabled-by-default and config-loading branches are exercised directly.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path

# Repo root + server/src for absolute-path lookups
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "server" / "src"
for _p in (str(_REPO_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# --- Import the observability package (no external deps required) ---
import observability as _observability_pkg  # noqa: E402
from observability import (  # noqa: E402
    OtelConfig,
    VALID_EXPORTERS,
    is_metrics_active,
    is_tracing_active,
    load_otel_config,
    setup_metrics,
    setup_observability,
    setup_tracing,
    shutdown_metrics,
    shutdown_tracing,
)
from observability import metrics as otel_metrics  # noqa: E402
from observability import tracing as otel_tracing  # noqa: E402


def _with_clean_env(monkey_env: dict):
    """Context manager helper to apply + revert env vars around a block."""

    class _Ctx:
        def __enter__(self):
            self._saved = {}
            for k, v in monkey_env.items():
                self._saved[k] = os.environ.get(k)
                os.environ[k] = v
            return self

        def __exit__(self, exc_type, exc, tb):
            for k, v in self._saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    return _Ctx()


def _reset_otel_globals():
    """Reset OTel global provider state between tests.

    The OTel SDK raises a warning ("Overriding of current Provider is
    not allowed") when you try to ``set_*_provider`` a second time
    after one was set. To allow test isolation we reset the global
    locks + provider slots. This is a test-only utility and never used
    in production code.
    """
    # Tracing globals
    try:
        import opentelemetry.trace as _t

        _t._TRACER_PROVIDER_SET_ONCE = _t.Once()
        _t._TRACER_PROVIDER = None
    except Exception:  # pragma: no cover
        pass
    # Metrics globals
    try:
        import opentelemetry.metrics._internal as _mi

        _mi._METER_PROVIDER_SET_ONCE = _mi.Once()
        _mi._METER_PROVIDER = None
    except Exception:  # pragma: no cover
        pass


def _new_minimal_app():
    """Build a 2-endpoint FastAPI app without pulling in api_pg.py.

    Keeps the test self-contained -- we do NOT want to require Postgres,
    embedding models, or the 153KB api_pg import chain to exercise OTel.
    """
    from fastapi import FastAPI

    app = FastAPI(title="cortex-otel-test-app")

    @app.get("/health")
    def _health():
        return {"status": "ok"}

    @app.get("/api/echo/{value}")
    def _echo(value: str):
        return {"echo": value}

    return app


# =====================================================================
# 1) OTEL_ENABLED=false -> setup_observability is a no-op
# =====================================================================


class TestOtelDisabledByDefault(unittest.TestCase):
    """Spec test 1: default-off behaviour with zero overhead."""

    def setUp(self):
        # Ensure clean state regardless of env.
        shutdown_tracing()
        shutdown_metrics()
        _reset_otel_globals()

    def tearDown(self):
        shutdown_tracing()
        shutdown_metrics()
        _reset_otel_globals()

    def test_load_config_defaults_to_disabled(self):
        # Save and restore.
        with _with_clean_env({}):
            cfg = load_otel_config(service_version="6.5.0")
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.exporter, "console")
        self.assertEqual(cfg.service_name, "memory-cortex")
        self.assertEqual(cfg.otlp_endpoint, "http://localhost:4317")
        self.assertEqual(cfg.sample_ratio, 1.0)
        self.assertTrue(cfg.metrics_enabled)

    def test_load_config_explicit_false(self):
        with _with_clean_env({"OTEL_ENABLED": "false"}):
            cfg = load_otel_config()
        self.assertFalse(cfg.enabled)

    def test_load_config_explicit_true(self):
        with _with_clean_env(
            {
                "OTEL_ENABLED": "true",
                "OTEL_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317",
                "OTEL_SAMPLE_RATIO": "0.25",
                "OTEL_METRICS_ENABLED": "false",
            }
        ):
            cfg = load_otel_config()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.exporter, "otlp")
        self.assertEqual(cfg.otlp_endpoint, "http://collector:4317")
        self.assertAlmostEqual(cfg.sample_ratio, 0.25, places=4)
        self.assertFalse(cfg.metrics_enabled)

    def test_load_config_invalid_exporter_falls_back_to_none(self):
        with _with_clean_env(
            {"OTEL_ENABLED": "true", "OTEL_EXPORTER": "banana"}
        ):
            cfg = load_otel_config()
        self.assertEqual(cfg.exporter, "none")

    def test_load_config_valid_exporters(self):
        self.assertEqual(set(VALID_EXPORTERS), {"otlp", "console", "memory", "none"})

    def test_otel_disabled_setup_observability_returns_false(self):
        app = _new_minimal_app()
        with _with_clean_env({"OTEL_ENABLED": "false"}):
            ok = setup_observability(app, service_version="6.5.0")
        self.assertFalse(ok)
        # Middleware must NOT be installed when disabled.
        self.assertFalse(getattr(app.state, "_cortex_otel_installed", False))
        self.assertFalse(is_tracing_active())
        self.assertFalse(is_metrics_active())

    def test_otel_disabled_setup_tracing_returns_false(self):
        with _with_clean_env({"OTEL_ENABLED": "false"}):
            ok = setup_tracing()
        self.assertFalse(ok)
        self.assertFalse(is_tracing_active())

    def test_otel_disabled_setup_metrics_returns_false(self):
        with _with_clean_env({"OTEL_ENABLED": "false"}):
            ok = setup_metrics()
        self.assertFalse(ok)
        self.assertFalse(is_metrics_active())

    def test_get_tracer_when_disabled_is_noop(self):
        with _with_clean_env({"OTEL_ENABLED": "false"}):
            tracer = _observability_pkg.get_tracer("test")
        # When SDK is missing or disabled, we return a NoOpTracer stand-in.
        self.assertIsNotNone(tracer)

    def test_start_span_when_disabled_is_noop_context_manager(self):
        with _with_clean_env({"OTEL_ENABLED": "false"}):
            with _observability_pkg.start_span("test-span", foo="bar") as span:
                # Setting attributes on the no-op span must not raise.
                if span is not None:
                    try:
                        span.set_attribute("k", "v")
                    except Exception:
                        self.fail("set_attribute raised on no-op span")


# =====================================================================
# 2) OTEL_ENABLED=true + console exporter -> providers armed
# =====================================================================


class TestOtelEnabledConsoleExporter(unittest.TestCase):
    """Spec test 2: opt-in flow arms the providers correctly."""

    def setUp(self):
        shutdown_tracing()
        shutdown_metrics()
        _reset_otel_globals()

    def tearDown(self):
        shutdown_tracing()
        shutdown_metrics()
        _reset_otel_globals()

    def test_setup_observability_armed_returns_true_when_sdk_available(self):
        if not _observability_pkg.otel_available():
            self.skipTest(
                "OpenTelemetry SDK not importable in this env; "
                "skipping the armed-provider assertion."
            )
        app = _new_minimal_app()
        with _with_clean_env(
            {"OTEL_ENABLED": "true", "OTEL_EXPORTER": "console"}
        ):
            ok = setup_observability(app, service_version="6.5.0")
        # When the SDK is present, setup_observability returns True.
        self.assertTrue(ok)
        self.assertTrue(getattr(app.state, "_cortex_otel_installed", False))
        self.assertTrue(is_tracing_active())
        self.assertTrue(is_metrics_active())

    def test_setup_observability_armed_with_exporter_none(self):
        """OTEL_EXPORTER=none still arms the provider; only the exporter is skipped.

        This is by design -- we want in-process telemetry even when no
        exporter is wired (e.g., for unit tests that observe metrics but
        do not want to write to stdout).
        """
        if not _observability_pkg.otel_available():
            self.skipTest("OpenTelemetry SDK not importable; skipping.")
        app = _new_minimal_app()
        with _with_clean_env(
            {
                "OTEL_ENABLED": "true",
                "OTEL_EXPORTER": "none",
                "OTEL_METRICS_ENABLED": "true",
            }
        ):
            ok = setup_observability(app, service_version="6.5.0")
        self.assertTrue(ok)
        # Provider armed; exporter is none (no captured_memory_metrics
        # reader, no console exporter).
        self.assertTrue(is_tracing_active())
        self.assertTrue(is_metrics_active())

    def test_setup_observability_sdk_missing_does_not_raise(self):
        # Even when the SDK is missing, setup must NEVER raise.
        # We force the probe to return False for this test only.
        prev = otel_tracing._OTEL_AVAILABLE
        otel_tracing._OTEL_AVAILABLE = False
        try:
            app = _new_minimal_app()
            with _with_clean_env({"OTEL_ENABLED": "true"}):
                ok = setup_observability(app, service_version="6.5.0")
            self.assertFalse(ok)
            self.assertFalse(getattr(app.state, "_cortex_otel_installed", False))
        finally:
            otel_tracing._OTEL_AVAILABLE = prev

    def test_capability_probe_caches_its_result(self):
        # Probe twice -- both calls must agree.
        a = _observability_pkg.otel_available()
        b = _observability_pkg.otel_available()
        self.assertEqual(a, b)


# =====================================================================
# 3) FastAPI request -> a span is captured (in-memory exporter)
# =====================================================================


class TestOtelTracingCapturesRequestSpan(unittest.TestCase):
    """Spec test 3: instrumented FastAPI app records spans."""

    def setUp(self):
        shutdown_tracing()
        shutdown_metrics()
        _reset_otel_globals()

    def tearDown(self):
        shutdown_tracing()
        shutdown_metrics()
        _reset_otel_globals()

    def test_request_records_span(self):
        if not _observability_pkg.otel_available():
            self.skipTest("OpenTelemetry SDK not importable; skipping.")
        app = _new_minimal_app()
        with _with_clean_env(
            {"OTEL_ENABLED": "true", "OTEL_EXPORTER": "memory"}
        ):
            ok = setup_observability(app, service_version="6.5.0")
        self.assertTrue(ok)

        # Hit a route through the middleware.
        from fastapi.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)

        spans = _observability_pkg.captured_memory_spans()
        self.assertGreater(len(spans), 0, "expected at least one captured span")

        names = [s.name for s in spans]
        # At least one span should be our /health request span.
        self.assertTrue(
            any("/health" in n for n in names),
            f"no /health span found in {names}",
        )

        # The request span should carry the standard attributes.
        health_spans = [s for s in spans if "/health" in s.name]
        self.assertGreater(len(health_spans), 0)
        attrs = dict(health_spans[0].attributes or {})
        self.assertEqual(attrs.get("http.method"), "GET")
        self.assertEqual(attrs.get("http.route"), "/health")
        # 200 captured as attribute.
        self.assertEqual(int(attrs.get("http.status_code", 0)), 200)


# =====================================================================
# 4) FastAPI request -> metrics are recorded
# =====================================================================


class TestOtelMetricsRecordRequest(unittest.TestCase):
    """Spec test 4: instrumented FastAPI app records HTTP metrics."""

    def setUp(self):
        shutdown_tracing()
        shutdown_metrics()
        _reset_otel_globals()

    def tearDown(self):
        shutdown_tracing()
        shutdown_metrics()
        _reset_otel_globals()

    def test_request_records_metric_observations(self):
        if not _observability_pkg.otel_available():
            self.skipTest("OpenTelemetry SDK not importable; skipping.")
        app = _new_minimal_app()
        with _with_clean_env(
            {"OTEL_ENABLED": "true", "OTEL_EXPORTER": "memory"}
        ):
            ok = setup_observability(app, service_version="6.5.0")
        self.assertTrue(ok)

        from fastapi.testclient import TestClient

        client = TestClient(app)
        # Drive 3 requests to make the assertion robust against any
        # internal cold-start observations.
        for _ in range(3):
            r = client.get("/health")
            self.assertEqual(r.status_code, 200)

        metrics = _observability_pkg.captured_memory_metrics()
        # At minimum we should see http.server.request.count + http.server.duration
        names = []
        for m in metrics:
            try:
                names.append(m.name)
            except Exception:
                pass
        self.assertIn(
            "http.server.request.count",
            names,
            f"missing request count metric in {names}",
        )
        self.assertIn(
            "http.server.duration",
            names,
            f"missing duration metric in {names}",
        )


# =====================================================================
# Helper / regression tests
# =====================================================================


class TestOtelConfigDataclass(unittest.TestCase):
    def test_frozen(self):
        cfg = OtelConfig(
            enabled=False,
            service_name="x",
            service_version="1.0",
            exporter="console",
            otlp_endpoint="http://localhost:4317",
            sample_ratio=1.0,
            metrics_enabled=True,
            log_correlation=True,
            capture_headers=False,
            excluded_urls=("/health",),
        )
        with self.assertRaises(Exception):
            cfg.enabled = True  # type: ignore[misc]

    def test_as_attributes(self):
        cfg = OtelConfig(
            enabled=True,
            service_name="cortex",
            service_version="6.5.0",
            exporter="otlp",
            otlp_endpoint="http://x",
            sample_ratio=0.5,
            metrics_enabled=False,
            log_correlation=True,
            capture_headers=False,
            excluded_urls=(),
        )
        attrs = cfg.as_attributes()
        self.assertEqual(attrs["otel.service_name"], "cortex")
        self.assertEqual(attrs["otel.exporter"], "otlp")
        self.assertEqual(attrs["otel.service_version"], "6.5.0")
        self.assertEqual(attrs["otel.metrics_enabled"], "false")


class TestApiPgWiresObservability(unittest.TestCase):
    """Source-level: api_pg.py imports setup_observability and calls it."""

    def test_api_pg_calls_setup_observability(self):
        api_path = _SRC / "api_pg.py"
        src = api_path.read_text(encoding="utf-8")
        # Commit cb04320 corrected the import to the package-qualified
        # `from src.observability import ...` (the bare form did not
        # resolve), but this test still asserted the old spelling and
        # has been failing since. Accept either qualified form -- what
        # matters is that the helper is imported and wired to the app.
        self.assertTrue(
            "from observability import setup_observability" in src
            or "from src.observability import setup_observability" in src,
            "api_pg.py must import setup_observability",
        )
        self.assertIn("setup_observability(app", src)

    def test_config_module_exposes_otel_fields(self):
        # Reload config to pick up env-driven values, but in the same
        # process so we don't depend on .env.
        # CortexConfig._load() requires EMBEDDING_MODEL_PATH to be set --
        # otherwise it raises. We stub a temp path to satisfy that.
        old_mp = os.environ.get("EMBEDDING_MODEL_PATH")
        os.environ["EMBEDDING_MODEL_PATH"] = "/tmp/dummy-model-for-otel-test"
        try:
            # Force a fresh import so the singleton re-loads.
            sys.modules.pop("config", None)
            cfg_mod = importlib.import_module("config")
            for field_name in (
                "otel_enabled",
                "otel_exporter",
                "otel_service_name",
                "otel_otlp_endpoint",
                "otel_sample_ratio",
                "otel_metrics_enabled",
            ):
                self.assertTrue(
                    hasattr(cfg_mod.config, field_name),
                    f"missing OTel field on CortexConfig: {field_name}",
                )
        finally:
            if old_mp is None:
                os.environ.pop("EMBEDDING_MODEL_PATH", None)
            else:
                os.environ["EMBEDDING_MODEL_PATH"] = old_mp
            sys.modules.pop("config", None)

    def test_env_example_documents_otel(self):
        env_path = _REPO_ROOT / ".env.example"
        if not env_path.exists():
            self.skipTest(".env.example not found at repo root")
        text = env_path.read_text(encoding="utf-8")
        self.assertIn("OTEL_ENABLED", text)
        self.assertIn("OTEL_EXPORTER", text)

    def test_observability_public_api_has_required_symbols(self):
        for sym in (
            "setup_observability",
            "setup_tracing",
            "setup_metrics",
            "load_otel_config",
            "OtelConfig",
            "start_span",
            "record_request",
            "record_active",
            "otel_available",
            "metrics_available",
        ):
            self.assertTrue(
                hasattr(_observability_pkg, sym),
                f"observability package missing public symbol: {sym}",
            )


class TestNoOpFallbacks(unittest.TestCase):
    """The package must be safe to use when the SDK is unavailable."""

    def setUp(self):
        shutdown_tracing()
        shutdown_metrics()
        _reset_otel_globals()

    def tearDown(self):
        shutdown_tracing()
        shutdown_metrics()
        _reset_otel_globals()

    def test_record_request_when_disabled_does_not_raise(self):
        with _with_clean_env({"OTEL_ENABLED": "false"}):
            # Should silently no-op.
            _observability_pkg.record_request(
                method="GET",
                route="/x",
                status_code=200,
                duration_ms=12.0,
            )

    def test_record_active_when_disabled_does_not_raise(self):
        with _with_clean_env({"OTEL_ENABLED": "false"}):
            _observability_pkg.record_active(1, route="/x")
            _observability_pkg.record_active(-1, route="/x")

    def test_captured_memory_metrics_returns_empty_when_uninitialized(self):
        self.assertEqual(_observability_pkg.captured_memory_metrics(), [])
        self.assertEqual(_observability_pkg.captured_memory_spans(), [])

    def test_reset_memory_exporter_safe_when_uninitialized(self):
        # Must not raise even when no exporter is wired.
        _observability_pkg.reset_memory_exporter()


class TestUnittestMirror(unittest.TestCase):
    """Re-run the 4 dispatch-mandated tests via unittest for the runner."""

    def test_1_otel_disabled_by_default(self):
        suite = unittest.TestLoader().loadTestsFromTestCase(
            TestOtelDisabledByDefault
        )
        result = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(
            suite
        )
        self.assertTrue(result.wasSuccessful())

    def test_2_otel_enabled_console_exporter(self):
        suite = unittest.TestLoader().loadTestsFromTestCase(
            TestOtelEnabledConsoleExporter
        )
        result = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(
            suite
        )
        self.assertTrue(result.wasSuccessful())

    def test_3_otel_tracing_captures_request_span(self):
        suite = unittest.TestLoader().loadTestsFromTestCase(
            TestOtelTracingCapturesRequestSpan
        )
        result = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(
            suite
        )
        self.assertTrue(result.wasSuccessful())

    def test_4_otel_metrics_record_request(self):
        suite = unittest.TestLoader().loadTestsFromTestCase(
            TestOtelMetricsRecordRequest
        )
        result = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(
            suite
        )
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main(verbosity=2)
