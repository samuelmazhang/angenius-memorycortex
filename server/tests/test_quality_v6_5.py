"""Unit tests for Cortex v6.5.0 fact quality monitor (Phase 2 #NEW3).

Covers the 3 dispatch-mandated tests + helper / regression tests.

  Required (mapping to spec):
    1. test_quality_monitor_marks_low_confidence
    2. test_quality_monitor_threshold_configurable
    3. test_quality_monitor_returns_review_ids

  Helpers / regression:
    4.  test_analyze_empty_input_returns_zero_shape
    5.  test_analyze_none_input_returns_zero_shape
    6.  test_analyze_all_low_confidence_yields_full_ratio
    7.  test_analyze_all_high_confidence_yields_zero_ratio
    8.  test_avg_confidence_is_mean_of_valid_only
    9.  test_analyze_skips_non_dict_entries
   10.  test_analyze_skips_missing_confidence
   11.  test_analyze_skips_non_numeric_confidence
   12.  test_analyze_skips_out_of_range_confidence
   13.  test_analyze_skips_bool_confidence
   14.  test_ratio_is_zero_when_no_valid_facts
   15.  test_needs_review_ids_in_input_order
   16.  test_id_resolution_prefers_explicit_id
   17.  test_id_resolution_falls_back_to_entity_predicate
   18.  test_id_resolution_falls_back_to_positional
   19.  test_threshold_validation_rejects_out_of_range
   20.  test_threshold_validation_rejects_non_numeric
   21.  test_threshold_validation_rejects_bool
   22.  test_is_low_quality_convenience_method
   23.  test_module_functional_wrapper_analyze_facts
   24.  test_module_public_surface
   25.  test_idempotency_same_input_same_output

No LLM, no httpx, no live Postgres. The module is exercised against
in-memory dicts shaped like ``extract.py`` output.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consolidate  # noqa: E402
from consolidate import (  # noqa: E402
    DEFAULT_LOW_QUALITY_THRESHOLD,
    FactQualityMonitor,
    analyze_facts,
)
from consolidate.quality import (  # noqa: E402
    _coerce_confidence,
    _fact_id,
)


# ============================================================================
# Fixtures
# ============================================================================

def _fact(conf, *, entity="user", predicate="likes", value="tea", id=None):
    """Build a canonical extract.py-shaped fact."""
    f = {
        "entity": entity,
        "predicate": predicate,
        "value": value,
        "confidence": conf,
    }
    if id is not None:
        f["id"] = id
    return f


# ============================================================================
# 3 dispatch-mandated tests
# ============================================================================

class DispatchMandatedTests(unittest.TestCase):
    """The three tests the dispatch prompt explicitly requires."""

    def test_quality_monitor_marks_low_confidence(self):
        """#1: low-confidence facts land in needs_review_ids."""
        monitor = FactQualityMonitor()  # default threshold 0.5
        facts = [
            _fact(0.9, id="a"),
            _fact(0.3, id="b"),  # low
            _fact(0.49, id="c"),  # low (just below 0.5)
            _fact(0.5, id="d"),  # not low (boundary is exclusive)
            _fact(0.8, id="e"),
        ]
        result = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result["low_quality_count"], 2)
        self.assertEqual(result["needs_review_ids"], ["b", "c"])
        # avg = (0.9 + 0.3 + 0.49 + 0.5 + 0.8) / 5
        self.assertAlmostEqual(result["avg_confidence"], (0.9 + 0.3 + 0.49 + 0.5 + 0.8) / 5)
        self.assertAlmostEqual(result["low_quality_ratio"], 2 / 5)

    def test_quality_monitor_threshold_configurable(self):
        """#2: raising the threshold reclassifies mid-confidence facts."""
        monitor = FactQualityMonitor(low_quality_threshold=0.9)
        facts = [
            _fact(0.95, id="a"),  # not low (>= 0.9)
            _fact(0.8, id="b"),  # low with the raised threshold
            _fact(0.1, id="c"),  # low
        ]
        result = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result["low_quality_count"], 2)
        self.assertEqual(result["needs_review_ids"], ["b", "c"])
        self.assertEqual(monitor.low_quality_threshold, 0.9)

        # Default monitor on the same input: only ``c`` is low.
        default = FactQualityMonitor()
        result_default = default.analyze_confidence_distribution(facts)
        self.assertEqual(result_default["low_quality_count"], 1)
        self.assertEqual(result_default["needs_review_ids"], ["c"])

    def test_quality_monitor_returns_review_ids(self):
        """#3: needs_review_ids is a list of ids, populated in input order."""
        monitor = FactQualityMonitor(low_quality_threshold=0.5)
        facts = [
            _fact(0.2, id="x-1"),
            _fact(0.4, id="x-2"),
            _fact(0.6, id="x-3"),  # not low
            _fact(0.1, id="x-4"),
        ]
        result = monitor.analyze_confidence_distribution(facts)
        ids = result["needs_review_ids"]
        self.assertIsInstance(ids, list)
        self.assertEqual(ids, ["x-1", "x-2", "x-4"])  # input order preserved


# ============================================================================
# Helper / regression tests
# ============================================================================

class EmptyAndZeroInputs(unittest.TestCase):
    def test_analyze_empty_input_returns_zero_shape(self):
        monitor = FactQualityMonitor()
        result = monitor.analyze_confidence_distribution([])
        self.assertEqual(
            result,
            {
                "low_quality_count": 0,
                "avg_confidence": 0.0,
                "low_quality_ratio": 0.0,
                "needs_review_ids": [],
            },
        )

    def test_analyze_none_input_returns_zero_shape(self):
        monitor = FactQualityMonitor()
        result = monitor.analyze_confidence_distribution(None)
        self.assertEqual(result["low_quality_count"], 0)
        self.assertEqual(result["avg_confidence"], 0.0)
        self.assertEqual(result["low_quality_ratio"], 0.0)
        self.assertEqual(result["needs_review_ids"], [])


class RatioMath(unittest.TestCase):
    def test_analyze_all_low_confidence_yields_full_ratio(self):
        monitor = FactQualityMonitor()
        facts = [_fact(0.1, id=str(i)) for i in range(5)]
        result = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result["low_quality_count"], 5)
        self.assertAlmostEqual(result["low_quality_ratio"], 1.0)
        self.assertAlmostEqual(result["avg_confidence"], 0.1)

    def test_analyze_all_high_confidence_yields_zero_ratio(self):
        monitor = FactQualityMonitor()
        facts = [_fact(0.95, id=str(i)) for i in range(3)]
        result = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result["low_quality_count"], 0)
        self.assertAlmostEqual(result["low_quality_ratio"], 0.0)
        self.assertAlmostEqual(result["avg_confidence"], 0.95)
        self.assertEqual(result["needs_review_ids"], [])

    def test_avg_confidence_is_mean_of_valid_only(self):
        """Malformed entries must NOT pollute the average."""
        monitor = FactQualityMonitor()
        facts = [
            _fact(0.4, id="a"),
            _fact("garbage", id="b"),  # invalid
            _fact(0.6, id="c"),
            {"entity": "x", "predicate": "y", "value": None},  # no confidence
        ]
        result = monitor.analyze_confidence_distribution(facts)
        # Only a and c count; b is skipped, the no-confidence one is skipped.
        self.assertEqual(result["low_quality_count"], 1)  # only "a"
        self.assertEqual(result["needs_review_ids"], ["a"])
        self.assertAlmostEqual(result["avg_confidence"], 0.5)
        self.assertAlmostEqual(result["low_quality_ratio"], 1 / 2)

    def test_ratio_is_zero_when_no_valid_facts(self):
        monitor = FactQualityMonitor()
        facts = [
            {"entity": "x", "predicate": "y", "value": None},  # no confidence
            "not a dict",
            {"entity": "a", "predicate": "b"},  # no confidence
        ]
        result = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result["low_quality_count"], 0)
        self.assertEqual(result["avg_confidence"], 0.0)
        self.assertEqual(result["low_quality_ratio"], 0.0)
        self.assertEqual(result["needs_review_ids"], [])


class MalformedInputTolerance(unittest.TestCase):
    def test_analyze_skips_non_dict_entries(self):
        monitor = FactQualityMonitor()
        facts = [
            "string-not-dict",
            42,
            None,
            _fact(0.2, id="real"),
            ["a", "list"],
        ]
        result = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result["low_quality_count"], 1)
        self.assertEqual(result["needs_review_ids"], ["real"])

    def test_analyze_skips_missing_confidence(self):
        monitor = FactQualityMonitor()
        facts = [
            {"entity": "e", "predicate": "p", "value": "v"},  # no confidence
            _fact(0.1, id="ok"),
        ]
        result = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result["low_quality_count"], 1)
        self.assertEqual(result["needs_review_ids"], ["ok"])

    def test_analyze_skips_non_numeric_confidence(self):
        monitor = FactQualityMonitor()
        facts = [
            _fact("high", id="str"),
            _fact(None, id="none"),
            _fact([0.5], id="list"),
            _fact({"c": 0.5}, id="dict"),
            _fact(0.1, id="real"),
        ]
        result = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result["low_quality_count"], 1)
        self.assertEqual(result["needs_review_ids"], ["real"])

    def test_analyze_skips_out_of_range_confidence(self):
        monitor = FactQualityMonitor()
        facts = [
            _fact(1.5, id="too_high"),
            _fact(-0.1, id="too_low"),
            _fact(0.1, id="real"),
        ]
        result = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result["low_quality_count"], 1)
        self.assertEqual(result["needs_review_ids"], ["real"])

    def test_analyze_skips_bool_confidence(self):
        """bool is an int subclass; reject it to avoid silent True=1.0."""
        monitor = FactQualityMonitor()
        facts = [
            _fact(True, id="t"),
            _fact(False, id="f"),
            _fact(0.1, id="real"),
        ]
        result = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result["low_quality_count"], 1)
        self.assertEqual(result["needs_review_ids"], ["real"])


class IdResolution(unittest.TestCase):
    def test_needs_review_ids_in_input_order(self):
        monitor = FactQualityMonitor()
        facts = [
            _fact(0.1, id="c"),
            _fact(0.2, id="a"),
            _fact(0.3, id="b"),
        ]
        result = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result["needs_review_ids"], ["c", "a", "b"])

    def test_id_resolution_prefers_explicit_id(self):
        f = _fact(0.1, entity="alice", predicate="likes", value="coffee", id="42")
        self.assertEqual(_fact_id(f, 99), "42")

    def test_id_resolution_falls_back_to_entity_predicate(self):
        f = _fact(0.1, entity="alice", predicate="likes", value="coffee")
        self.assertEqual(_fact_id(f, 99), "alice::likes")

    def test_id_resolution_falls_back_to_positional(self):
        f = {"entity": "", "predicate": "", "value": "v", "confidence": 0.1}
        # entity and predicate are empty -> positional fallback
        self.assertEqual(_fact_id(f, 7), "fact#7")


class ThresholdValidation(unittest.TestCase):
    def test_threshold_validation_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            FactQualityMonitor(low_quality_threshold=1.5)
        with self.assertRaises(ValueError):
            FactQualityMonitor(low_quality_threshold=-0.1)

    def test_threshold_validation_rejects_non_numeric(self):
        with self.assertRaises(TypeError):
            FactQualityMonitor(low_quality_threshold="0.5")
        with self.assertRaises(TypeError):
            FactQualityMonitor(low_quality_threshold=None)

    def test_threshold_validation_rejects_bool(self):
        with self.assertRaises(TypeError):
            FactQualityMonitor(low_quality_threshold=True)

    def test_threshold_validation_accepts_boundaries(self):
        FactQualityMonitor(low_quality_threshold=0.0)  # noqa: F841
        FactQualityMonitor(low_quality_threshold=1.0)  # noqa: F841


class ConvenienceHelpers(unittest.TestCase):
    def test_is_low_quality_convenience_method(self):
        monitor = FactQualityMonitor()
        self.assertTrue(monitor.is_low_quality(_fact(0.2, id="x")))
        self.assertFalse(monitor.is_low_quality(_fact(0.9, id="y")))
        self.assertFalse(monitor.is_low_quality(_fact(0.5, id="z")))  # boundary exclusive
        # Non-dict / malformed gracefully returns False
        self.assertFalse(monitor.is_low_quality({"entity": "e", "predicate": "p"}))
        self.assertFalse(monitor.is_low_quality(None))
        self.assertFalse(monitor.is_low_quality("not a dict"))

    def test_module_functional_wrapper_analyze_facts(self):
        facts = [_fact(0.2, id="a"), _fact(0.8, id="b")]
        result_a = analyze_facts(facts)
        result_b = analyze_facts(facts, low_quality_threshold=0.9)
        # Same instance call
        monitor = FactQualityMonitor()
        result_c = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(result_a, result_c)
        # Custom threshold: both now low
        self.assertEqual(result_b["low_quality_count"], 2)
        self.assertEqual(result_b["needs_review_ids"], ["a", "b"])

    def test_module_public_surface(self):
        # All advertised symbols are importable
        self.assertTrue(hasattr(consolidate, "FactQualityMonitor"))
        self.assertTrue(hasattr(consolidate, "analyze_facts"))
        self.assertTrue(hasattr(consolidate, "DEFAULT_LOW_QUALITY_THRESHOLD"))
        # Module __all__ matches
        for name in ("FactQualityMonitor", "analyze_facts", "DEFAULT_LOW_QUALITY_THRESHOLD"):
            self.assertIn(name, consolidate.__all__)
        # Default threshold value
        self.assertEqual(DEFAULT_LOW_QUALITY_THRESHOLD, 0.5)


class Determinism(unittest.TestCase):
    def test_idempotency_same_input_same_output(self):
        monitor = FactQualityMonitor()
        facts = [
            _fact(0.1, id="a"),
            _fact(0.9, id="b"),
            _fact(0.3, id="c"),
        ]
        first = monitor.analyze_confidence_distribution(facts)
        second = monitor.analyze_confidence_distribution(facts)
        self.assertEqual(first, second)


class CoercionInternals(unittest.TestCase):
    """Direct tests for the internal _coerce_confidence helper."""

    def test_coerce_accepts_int(self):
        self.assertEqual(_coerce_confidence(0), 0.0)
        self.assertEqual(_coerce_confidence(1), 1.0)

    def test_coerce_accepts_float(self):
        self.assertAlmostEqual(_coerce_confidence(0.42), 0.42)

    def test_coerce_rejects_bool(self):
        self.assertIsNone(_coerce_confidence(True))
        self.assertIsNone(_coerce_confidence(False))

    def test_coerce_rejects_non_numeric(self):
        self.assertIsNone(_coerce_confidence("0.5"))
        self.assertIsNone(_coerce_confidence(None))
        self.assertIsNone(_coerce_confidence([0.5]))
        self.assertIsNone(_coerce_confidence({"c": 0.5}))

    def test_coerce_rejects_out_of_range(self):
        self.assertIsNone(_coerce_confidence(-0.01))
        self.assertIsNone(_coerce_confidence(1.01))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
