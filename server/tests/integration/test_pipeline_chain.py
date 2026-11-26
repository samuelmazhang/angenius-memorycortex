"""B-layer integration tests: scenarios 1, 2, 5.

Scenarios:
  1.  Single SUPERSEDE round: write fact A, write conflicting fact B,
      verify B SUPERSEDEs A (A marked is_deprecated), then run the
      resulting fact batch through ``analyze_facts`` (Phase 2 #NEW3) and
      confirm the low-quality classification still works on the surviving
      row.

  2.  Triple-round ``KEEP -> SUPERSEDE -> DEDUPE``: write the same
      (entity, predicate) three times in succession, advancing
      ``confidence`` each round. Verify that:
        - round 1 returns KEEP (DB empty beforehand)
        - round 2 returns SUPERSEDE (existing fact deprecated)
        - round 3 returns DEDUPE or KEEP depending on rule (we assert
          "no double-active row" -- the active row count never exceeds 1)
      and that ``analyze_facts`` on the final DB sees exactly one
      non-deprecated row with the highest confidence.

  5.  Bulk scalability: 500 distinct facts pushed through
      ``resolve_conflicts`` + ``analyze_facts``. Both must complete in
      well under the test:blast budget (<60s, target <10s). Asserts:
        - active row count == 500 (no spurious deprecation)
        - low-quality fraction is in the expected confidence-based range
        - conflict_log entries == 0 (every fact was KEEP, no conflicts)
"""

from __future__ import annotations

import time
import unittest

from _shared import (  # type: ignore[import-not-found]
    DEFAULT_LOW_QUALITY_THRESHOLD,
    FakeDB,
    _fact,
    analyze_facts,
    resolve_conflicts,
)


# ===========================================================================
# Scenario 1 -- single SUPERSEDE + quality gate
# ===========================================================================

class TestScenario1ExtractConflictQuality(unittest.TestCase):
    """Cross-module: resolve_conflicts (#A.2) + analyze_facts (#NEW3)."""

    def test_supersede_then_quality_classification(self):
        # _fact() signature: (entity, predicate, value, confidence, user_id, source_chunk_id).
        # Footgun: positional 5th arg is user_id, NOT source_chunk_id. Single-test
        # convention is to leave user_id at the default "u_001" so SUPERSEDE actually
        # finds the previous row (resolve_conflicts filters by user_id).
        db = FakeDB()
        # Round 1: original fact (medium confidence)
        chunk_a = _fact("alice", "age", "30", confidence=0.6)
        actions_a = resolve_conflicts([chunk_a], db.query, db.execute)
        self.assertEqual(len(actions_a), 1)
        self.assertEqual(actions_a[0].action, "KEEP")
        # Round 2: conflicting fact (higher confidence, newer value) -- should SUPERSEDE
        chunk_b = _fact("alice", "age", "31", confidence=0.95)
        actions_b = resolve_conflicts([chunk_b], db.query, db.execute)
        self.assertEqual(len(actions_b), 1)
        self.assertEqual(actions_b[0].action, "SUPERSEDE")
        # DB invariants after supersede:
        active = [r for r in db.facts if not r["is_deprecated"]]
        deprecated = [r for r in db.facts if r["is_deprecated"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(len(deprecated), 1)
        self.assertEqual(active[0]["confidence"], 0.95)
        self.assertEqual(deprecated[0]["confidence"], 0.6)
        # ADR-068 writes one audit row per action regardless of type
        # (KEEP, DEDUPE, SUPERSEDE all log for traceability -- see
        # conflict_resolve.py module docstring L28).
        self.assertEqual(len(db.conflict_log), 2)
        log_actions = [l["action"] for l in db.conflict_log]
        self.assertEqual(log_actions, ["keep", "supersede"])
        # Hand off to quality monitor (#NEW3) -- must classify active row
        # without confusing it with the deprecated one.
        result = analyze_facts(db.facts)
        # analyze_facts returns {low_quality_count, avg_confidence,
        # low_quality_ratio, needs_review_ids}. It counts ALL facts
        # (including deprecated) but only counts those with confidence
        # in [0,1]. Both our rows have valid confidence.
        self.assertEqual(result["low_quality_count"], 0)
        # avg = (0.6 + 0.95) / 2 = 0.775
        self.assertAlmostEqual(result["avg_confidence"], 0.775, places=4)
        self.assertEqual(result["low_quality_ratio"], 0.0)


# ===========================================================================
# Scenario 2 -- KEEP -> SUPERSEDE -> DEDUPE loop, with quality guard
# ===========================================================================

class TestScenario2TripleRoundConflictResolution(unittest.TestCase):
    """Cross-module: resolve_conflicts (#A.2) running three times +
    analyze_facts (#NEW3) on the final DB.

    Verifies that the active-row invariant is maintained across multiple
    resolution rounds AND that the quality monitor classifies the
    surviving row correctly.
    """

    def test_keep_supersede_dedupe_invariants(self):
        db = FakeDB()
        # Round 1: KEEP (no existing row)
        chunk1 = _fact("bob", "city", "Beijing", confidence=0.5)
        actions1 = resolve_conflicts([chunk1], db.query, db.execute)
        self.assertEqual(len(actions1), 1)
        self.assertEqual(actions1[0].action, "KEEP")
        # Round 2: SUPERSEDE (same key, different value, higher confidence)
        chunk2 = _fact("bob", "city", "Shanghai", confidence=0.85)
        actions2 = resolve_conflicts([chunk2], db.query, db.execute)
        self.assertEqual(actions2[0].action, "SUPERSEDE")
        # Round 3: DEDUPE / KEEP -- same key, same value as round 2, lower
        # confidence. resolve_conflicts sees an existing active row with
        # the *same* value and should NOT mark it deprecated.
        chunk3 = _fact("bob", "city", "Shanghai", confidence=0.7)
        actions3 = resolve_conflicts([chunk3], db.query, db.execute)
        # Active-row invariant: there must NEVER be more than one
        # non-deprecated row for this (entity, predicate) pair.
        active = [r for r in db.facts if not r["is_deprecated"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["confidence"], 0.85)
        # Conflict log: 3 rows total (one per action). Round 1 KEEP,
        # round 2 SUPERSEDE, round 3 DEDUPE-or-KEEP. Exactly one
        # SUPERSEDE row.
        supersede_logs = [l for l in db.conflict_log if l["action"] == "supersede"]
        self.assertEqual(len(supersede_logs), 1)
        self.assertEqual(len(db.conflict_log), 3)
        # Quality gate: surviving row (conf=0.85) must classify as
        # high-quality. DEDUPE does NOT insert a new row (it would be a
        # duplicate), so the final fact count is 2, not 3: one active
        # (the SUPERSEDEr, conf=0.85) and one deprecated (round 1, conf=0.5).
        result = analyze_facts(db.facts)
        # Both confidences >= 0.5 threshold.
        self.assertEqual(result["low_quality_count"], 0)
        self.assertAlmostEqual(result["avg_confidence"], (0.5 + 0.85) / 2, places=4)
        self.assertEqual(len(result["needs_review_ids"]), 0)
        # And confirm our assumption: 2 rows in the DB, exactly 1 deprecated.
        self.assertEqual(len(db.facts), 2)
        self.assertEqual(sum(1 for r in db.facts if r["is_deprecated"]), 1)


# ===========================================================================
# Scenario 5 -- 500-fact bulk pipeline
# ===========================================================================

class TestScenario5BulkScalability(unittest.TestCase):
    """Cross-module: resolve_conflicts (#A.2) + analyze_facts (#NEW3) on
    500 distinct facts. No external deps. Wall-clock budget: <10s.

    The point is to surface any quadratic-time regressions or
    accidental O(N^2) materialization between the two modules.
    """

    BULK_N = 500

    def test_500_distinct_facts_round_trip(self):
        db = FakeDB()
        chunks = []
        for i in range(self.BULK_N):
            # 80% high-confidence, 20% low-confidence -- exercises both
            # quality buckets. Distinct (entity, predicate) so every
            # round is KEEP.
            conf = 0.95 if i % 5 != 0 else 0.4
            chunks.append(
                _fact(
                    entity=f"entity_{i}",
                    predicate="has_property",
                    value=f"value_{i}",
                    confidence=conf,
                    source_chunk_id=f"src_{i}",
                )
            )
        t0 = time.perf_counter()
        actions = resolve_conflicts(chunks, db.query, db.execute)
        elapsed_resolve = time.perf_counter() - t0
        # Every action must be KEEP (no duplicate keys).
        self.assertEqual(len(actions), self.BULK_N)
        self.assertTrue(all(a.action == "KEEP" for a in actions))
        # DB invariants: every KEEP inserts one fact AND writes one
        # audit log row (ADR-068 design).
        self.assertEqual(len(db.facts), self.BULK_N)
        self.assertEqual(len(db.conflict_log), self.BULK_N)
        self.assertTrue(all(l["action"] == "keep" for l in db.conflict_log))
        # Quality monitor hand-off.
        t1 = time.perf_counter()
        result = analyze_facts(db.facts)
        elapsed_quality = time.perf_counter() - t1
        # 100 of 500 have conf=0.4 < threshold=0.5 -> low_quality_count
        # The ratio 100/500 = 0.2.
        self.assertEqual(result["low_quality_count"], 100)
        self.assertAlmostEqual(result["low_quality_ratio"], 0.2, places=3)
        # Wall-clock budget -- the test:blast gate is <60s, our budget is
        # <10s. We assert *resolved*+quality under 10s; the module
        # boundary should stay linear.
        total = elapsed_resolve + elapsed_quality
        self.assertLess(total, 10.0,
            f"Bulk pipeline took {total:.2f}s (resolve={elapsed_resolve:.2f}s, "
            f"quality={elapsed_quality:.2f}s). Likely O(N^2) regression.")
        # Diagnostic line -- pytest -v shows this.
        print(
            f"\n[BULK N={self.BULK_N}] resolve={elapsed_resolve*1000:.1f}ms "
            f"quality={elapsed_quality*1000:.1f}ms total={total*1000:.1f}ms"
        )


if __name__ == "__main__":
    unittest.main()