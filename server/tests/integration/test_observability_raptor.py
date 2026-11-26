"""B-layer integration test: scenario 6.

Scenario 6 -- Observability + RAPTOR can co-exist.

Cross-module:
  * observability.start_span / captured_memory_spans (Phase 2 #6)
  * raptor.build_tree + query_with_raptor (Phase 2 #9)

These two modules never call each other. The integration contract is
much weaker: when OTel is enabled and we wrap a RAPTOR query in a span,
the span MUST be captured AND the RAPTOR result MUST be unchanged
(observability cannot mutate the data path).

If OTel SDK is not installed, the test skips cleanly (the production
code degrades to no-ops -- this is part of the contract we want to
preserve).
"""

from __future__ import annotations

import contextlib
import os
import unittest

from _shared import (  # type: ignore[import-not-found]
    _two_clusters,
    build_tree,
    np,
    query_with_raptor,
)

# Import the observability package directly -- this works even when the
# OTel SDK is missing (it degrades to no-ops).
from observability import (  # type: ignore[import-not-found]  # noqa: E402
    captured_memory_spans,
    reset_memory_exporter,
    setup_tracing,
    shutdown_tracing,
    start_span,
)
from observability import otel_available as _otel_available  # noqa: E402


@contextlib.contextmanager
def _env(monkey_env: dict):
    """Apply env vars, then restore them on exit. Lifted from
    test_otel_v6_5._with_clean_env (private helper).
    """
    saved = {}
    for k, v in monkey_env.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestObservabilityRaptorCoexist(unittest.TestCase):
    """OTel tracing wrapping a RAPTOR query: must capture the span AND
    leave the query result unchanged."""

    def setUp(self):
        # Clean any leftover state from a prior test.
        shutdown_tracing()
        reset_memory_exporter()

    def tearDown(self):
        shutdown_tracing()
        reset_memory_exporter()

    def test_raptor_query_inside_span_captures_span_and_returns_correct_results(self):
        if not _otel_available():
            self.skipTest("OpenTelemetry SDK not importable; skipping.")
        with _env({"OTEL_ENABLED": "true", "OTEL_EXPORTER": "memory"}):
            # Only OTEL_ENABLED and OTEL_EXPORTER are controlled here.
            # service_name stays at the default 'memory-cortex' (which
            # TestOtelDisabledByDefault.test_load_config_defaults_to_disabled
            # asserts on) and service_version goes through setup_tracing()
            # directly. We previously leaked OTEL_SERVICE_NAME into the
            # global env via os.environ.setdefault -- that test order
            # bug is now closed by NOT touching OTEL_SERVICE_NAME here.
            ok = setup_tracing(service_version="6.5.0")
            self.assertTrue(ok, "setup_tracing returned False under OTEL_ENABLED=true")
            reset_memory_exporter()
            # Reference run: query WITHOUT a span.
            chunks = _two_clusters(n_per=4, dim=4, seed=1729)
            tree = build_tree(chunks)
            hot_center = (np.ones(4) * 5.0).tolist()
            baseline = query_with_raptor(hot_center, tree, top_k=4)
            self.assertGreater(len(baseline), 0)
            # Now wrap a fresh query in a span.
            with start_span("raptor.query", top_k=4) as _span:
                wrapped = query_with_raptor(hot_center, tree, top_k=4)
            # Capture the span.
            spans = captured_memory_spans()
            # The data path must NOT have changed.
            self.assertEqual(
                [r["chunk_id"] for r in wrapped],
                [r["chunk_id"] for r in baseline],
                "OTel instrumentation mutated the RAPTOR query result -- contract violated.",
            )
            # And the span must have been recorded.
            self.assertGreater(len(spans), 0,
                "OTel enabled but no spans captured. The instrumentation "
                "either short-circuited or the exporter is not recording.")
            names = [getattr(s, "name", str(s)) for s in spans]
            self.assertTrue(
                any("raptor.query" in n for n in names),
                f"expected 'raptor.query' span in {names}, got none.",
            )

    def test_raptor_works_when_otel_disabled(self):
        """Regression guard: RAPTOR must still work when OTel is
        disabled (the default in slim venvs). This is part of the
        observability contract that the integration must not break.
        """
        with _env({"OTEL_ENABLED": "false"}):
            shutdown_tracing()
            reset_memory_exporter()
            # No setup_tracing() call needed -- disabled means no-ops.
            chunks = _two_clusters(n_per=4, dim=4, seed=1729)
            tree = build_tree(chunks)
            results = query_with_raptor(
                (np.ones(4) * 5.0).tolist(), tree, top_k=4
            )
            self.assertGreater(len(results), 0)
            # Captured spans remain empty (or whatever was there).
            spans = captured_memory_spans()
            # We assert only that RAPTOR worked -- we don't pin "no spans"
            # because the in-memory exporter may still hold prior runs.
            _ = spans


if __name__ == "__main__":
    unittest.main()