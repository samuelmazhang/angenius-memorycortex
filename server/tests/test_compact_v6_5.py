"""Unit tests for Cortex v6.5.0 consolidation pipeline (#C).

Covers the 4 dispatch-mandated tests + helper / regression tests.

  Required (mapping to spec):
    1. test_compact_preserves_key_facts     -- key facts (importance >= gate) survive
    2. test_compact_drops_redundant_chunks  -- duplicates collapse (high-importance wins)
    3. test_compact_respects_target_tokens  -- summary fits budget
    4. test_compact_idempotent              -- rerun yields same summary

  Helpers / regression:
    5. test_summarize_chunks_returns_string
    6. test_summarize_chunks_empty_input_returns_empty_string
    7. test_compact_returns_empty_summary_for_empty_input
    8. test_compact_skips_malformed_inputs
    9. test_compact_persists_to_memory_compressed
   10. test_compact_idempotency_db_guard
   11. test_compact_does_not_persist_when_db_execute_missing
   12. test_compact_handles_db_execute_failure
   13. test_compact_handles_zero_target_tokens
   14. test_compact_summary_tokens_match_summary_string
   15. test_summarize_chunks_uses_injected_fn
   16. test_summarize_chunks_falls_back_on_fn_failure
   17. test_compact_module_public_surface
   18. test_compact_dedupes_near_duplicates_via_overlap
   19. test_compact_source_chunk_id_override
   20. test_compact_synthetic_source_chunk_id_when_no_ids

No LLM, no httpx, no live Postgres. The compactor is exercised with
stub ``summarize_fn`` and an in-memory fake ``db_query`` / ``db_execute`.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consolidate  # noqa: E402
from consolidate import (  # noqa: E402
    CHARS_PER_TOKEN,
    CompactionResult,
    DEFAULT_IMPORTANCE_THRESHOLD,
    compact_long_conversation,
    summarize_chunks,
)


# ============================================================================
# Helpers
# ============================================================================

class _FakeDB:
    """In-memory fake DB mimicking ``memory_compressed`` shape.

    Three tables in one dict keyed by SQL keyword:
      * ``SELECT id FROM memory_compressed``  -> list of rows
      * ``INSERT INTO memory_compressed``     -> append + auto-increment PK
    Anything else is a no-op.
    """

    def __init__(self):
        self.rows = []  # list of {id, source_chunk_id, summary, importance}
        self.next_id = 1
        self.insert_calls = []
        self.select_calls = []

    def query(self, sql, params, fetch="all"):
        self.select_calls.append((sql, tuple(params)))
        return self._select(sql, tuple(params))

    def execute(self, sql, params):
        self.insert_calls.append((sql, tuple(params)))
        return self._insert(sql, tuple(params))

    def _select(self, sql, params):
        if "FROM memory_compressed" in sql and "SELECT id" in sql:
            src = params[0] if params else ""
            hits = [
                r for r in reversed(self.rows)
                if r["source_chunk_id"] == src
            ]
            if hits:
                return [{"id": hits[0]["id"]}]
            return []
        return []

    def _insert(self, sql, params):
        if "INSERT INTO memory_compressed" in sql:
            source = params[0]
            summary = params[1]
            importance = float(params[2])
            new_id = self.next_id
            self.next_id += 1
            self.rows.append({
                "id": new_id,
                "source_chunk_id": source,
                "summary": summary,
                "importance": importance,
            })
            return [{"id": new_id}]
        return None


def _make_chunk(cid, content, importance=5, user_id="u_test"):
    return {"id": cid, "content": content, "importance": importance, "user_id": user_id}


def _approx_tokens(s):
    if not s:
        return 0
    n = len(s.strip())
    if n == 0:
        return 0
    return (n + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


# ============================================================================
# 1. test_compact_preserves_key_facts
# ============================================================================

class _KeyFacts(unittest.TestCase):
    def test_compact_preserves_key_facts(self):
        """High-importance chunks survive even when budget is tight."""
        chunks = [
            _make_chunk("c1", "low signal filler one", importance=2),
            _make_chunk("c2", "low signal filler two", importance=3),
            _make_chunk("c3", "USER LIVES IN BEIJING", importance=9),
            _make_chunk("c4", "low signal filler three", importance=1),
            _make_chunk("c5", "USER WORKS AT OPENAI", importance=8),
        ]
        result = compact_long_conversation(chunks, target_tokens=20)

        # Both key facts must appear in the kept set.
        self.assertIn("c3", result.key_facts_preserved)
        self.assertIn("c5", result.key_facts_preserved)
        # High-importance chunks are kept.
        self.assertIn("c3", result.kept_chunk_ids)
        self.assertIn("c5", result.kept_chunk_ids)
        # Summary text must contain their content.
        self.assertIn("BEIJING", result.summary.upper())
        self.assertIn("OPENAI", result.summary.upper())


# ============================================================================
# 2. test_compact_drops_redundant_chunks
# ============================================================================

class _Dedup(unittest.TestCase):
    def test_compact_drops_redundant_chunks(self):
        """Chunks with identical content collapse to the highest-importance copy."""
        chunks = [
            _make_chunk("c1", "the cat sat on the mat", importance=4),
            _make_chunk("c2", "the cat sat on the mat", importance=7),
            _make_chunk("c3", "the cat sat on the mat", importance=2),
            _make_chunk("c4", "totally unrelated fact", importance=5),
        ]
        result = compact_long_conversation(chunks, target_tokens=200)

        self.assertEqual(len([c for c in result.kept_chunk_ids if c.startswith("c") and c != "c4"]), 1)
        # The winner is the importance-7 copy (c2), not the importance-4 (c1) or 2 (c3).
        self.assertIn("c2", result.kept_chunk_ids)
        self.assertNotIn("c1", result.kept_chunk_ids)
        self.assertNotIn("c3", result.kept_chunk_ids)
        # Duplicates recorded as dropped.
        self.assertIn("c1", result.dropped_chunk_ids)
        self.assertIn("c3", result.dropped_chunk_ids)
        # c4 (unrelated) survives.
        self.assertIn("c4", result.kept_chunk_ids)


# ============================================================================
# 3. test_compact_respects_target_tokens
# ============================================================================

class _Budget(unittest.TestCase):
    def test_compact_respects_target_tokens(self):
        """Summary fits within target_tokens (heuristic chars/4)."""
        big = "x" * 4000  # ~1000 tokens by the heuristic
        chunks = [_make_chunk(f"c{i}", big, importance=1) for i in range(10)]
        result = compact_long_conversation(chunks, target_tokens=64)

        self.assertLessEqual(result.summary_tokens, 64)
        # Flex budget consumed: most chunks got dropped.
        self.assertGreater(len(result.dropped_chunk_ids), 0)
        # The kept set, if non-empty, must fit.
        if result.kept_chunk_ids:
            tokens = sum(
                _approx_tokens("[1] (imp=1.0, id=" + c + ") " + big)
                for c in result.kept_chunk_ids
            )
            self.assertLessEqual(tokens, 64 + 50)  # 50 token header allowance


# ============================================================================
# 4. test_compact_idempotent
# ============================================================================

class _Idempotent(unittest.TestCase):
    def test_compact_idempotent(self):
        """Running twice on the same input produces the same summary."""
        chunks = [
            _make_chunk("c1", "alpha", importance=5),
            _make_chunk("c2", "beta", importance=7),
            _make_chunk("c3", "gamma", importance=6),
            _make_chunk("c1", "alpha", importance=2),  # duplicate (lower)
        ]
        now_calls = {"n": 0}

        def fake_now():
            now_calls["n"] += 1
            return 1234567.0 + now_calls["n"] * 0.0001  # tiny delta for distinct decided_at

        r1 = compact_long_conversation(chunks, target_tokens=200, now=fake_now)
        r2 = compact_long_conversation(chunks, target_tokens=200, now=fake_now)

        # The summary text and kept/dropped ids must be identical.
        self.assertEqual(r1.summary, r2.summary)
        self.assertEqual(r1.summary_tokens, r2.summary_tokens)
        self.assertEqual(r1.kept_chunk_ids, r2.kept_chunk_ids)
        self.assertEqual(r1.dropped_chunk_ids, r2.dropped_chunk_ids)
        self.assertEqual(r1.key_facts_preserved, r2.key_facts_preserved)
        self.assertEqual(r1.importance, r2.importance)
        # The decided_at timestamps differ (per-call), but the summary is
        # the deterministic part. (Caller decides whether to reset now.)


# ============================================================================
# 5. test_summarize_chunks_returns_string
# ============================================================================

class _SummarizeBasics(unittest.TestCase):
    def test_summarize_chunks_returns_string(self):
        """summarize_chunks returns a non-empty string for non-empty input."""
        chunks = [
            _make_chunk("c1", "fact one", importance=5),
            _make_chunk("c2", "fact two", importance=7),
        ]
        out = summarize_chunks(chunks)
        self.assertIsInstance(out, str)
        self.assertGreater(len(out), 0)
        self.assertIn("fact one", out)
        self.assertIn("fact two", out)

    def test_summarize_chunks_empty_input_returns_empty_string(self):
        """Empty input returns empty string, not error."""
        self.assertEqual(summarize_chunks([]), "")
        self.assertEqual(summarize_chunks(iter([])), "")
        self.assertEqual(summarize_chunks(None), "")

    def test_summarize_chunks_uses_injected_fn(self):
        """Injected summarize_fn replaces the default template."""
        chunks = [_make_chunk("a", "x", importance=1)]
        captured = []

        def fn(cs):
            captured.append(list(cs))
            short = "SHORT"
            return short  # short, not the template output

        out = summarize_chunks(chunks, summarize_fn=fn)
        self.assertEqual(out, "SHORT")
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(captured[0]), 1)

    def test_summarize_chunks_falls_back_on_fn_failure(self):
        """If summarize_fn raises, fall back to deterministic template."""
        chunks = [_make_chunk("a", "foo", importance=5)]

        def bad_fn(_):
            raise RuntimeError("LLM unreachable")

        out = summarize_chunks(chunks, summarize_fn=bad_fn)
        # Deterministic fallback still produces a non-empty string.
        self.assertIsInstance(out, str)
        self.assertGreater(len(out), 0)
        self.assertIn("foo", out)


# ============================================================================
# 6. test_compact_empty_input_handling + edge cases
# ============================================================================

class _EdgeCases(unittest.TestCase):
    def test_compact_returns_empty_summary_for_empty_input(self):
        """Empty input -> empty summary, persisted=False."""
        result = compact_long_conversation([], target_tokens=100)
        self.assertEqual(result.summary, "")
        self.assertEqual(result.summary_tokens, 0)
        self.assertEqual(result.kept_chunk_ids, [])
        self.assertEqual(result.dropped_chunk_ids, [])
        self.assertFalse(result.persisted)
        self.assertEqual(result.persisted_skipped_reason, "empty_input")

    def test_compact_skips_malformed_inputs(self):
        """Non-dict / empty-content chunks land in skipped_chunk_ids."""
        chunks = [
            _make_chunk("good", "valid content"),
            None,
            "not-a-dict",
            {"id": "empty", "content": ""},
            {"id": "blank", "content": "   "},
        ]
        result = compact_long_conversation(chunks, target_tokens=100)
        self.assertEqual(set(result.skipped_chunk_ids), {"empty", "blank"})
        self.assertEqual(result.kept_chunk_ids, ["good"])

    def test_compact_handles_zero_target_tokens(self):
        """target_tokens=0 -> empty summary, all valid chunks dropped."""
        chunks = [
            _make_chunk("c1", "fact one", importance=5),
            _make_chunk("c2", "fact two", importance=5),
        ]
        result = compact_long_conversation(chunks, target_tokens=0)
        self.assertEqual(result.summary, "")
        # All chunks dropped because no flex budget.
        self.assertEqual(set(result.kept_chunk_ids), set())
        self.assertEqual(set(result.dropped_chunk_ids), {"c1", "c2"})
        self.assertFalse(result.persisted)

    def test_compact_summary_tokens_match_summary_string(self):
        """summary_tokens field tracks approx-tokens(summary)."""
        chunks = [_make_chunk("c1", "x" * 200, importance=4)]
        result = compact_long_conversation(chunks, target_tokens=100)
        self.assertEqual(result.summary_tokens, _approx_tokens(result.summary))


# ============================================================================
# 7. test_compact_persistence behavior
# ============================================================================

class _Persistence(unittest.TestCase):
    def test_compact_does_not_persist_when_db_execute_missing(self):
        """No db_execute provided: persisted is False with reason."""
        chunks = [_make_chunk("c1", "hello", importance=5)]
        result = compact_long_conversation(chunks, target_tokens=200)
        self.assertFalse(result.persisted)
        self.assertEqual(result.persisted_skipped_reason, "no_db_execute")
        self.assertIsNone(result.compressed_id)

    def test_compact_persists_to_memory_compressed(self):
        """Provided db_execute: row written and compressed_id populated."""
        db = _FakeDB()
        chunks = [_make_chunk("c1", "hello", importance=5)]
        result = compact_long_conversation(
            chunks, target_tokens=200, db_execute=db.execute, db_query=db.query,
        )
        self.assertTrue(result.persisted)
        self.assertIsNotNone(result.compressed_id)
        # The DB got exactly one row with our content.
        self.assertEqual(len(db.insert_calls), 1)
        self.assertEqual(len(db.rows), 1)
        row = db.rows[0]
        self.assertEqual(row["source_chunk_id"], "c1")
        self.assertIn("hello", row["summary"])
        self.assertGreater(row["importance"], 0.0)

    def test_compact_idempotency_db_guard(self):
        """Re-running on the same input does NOT insert a duplicate row."""
        db = _FakeDB()
        chunks = [_make_chunk("c1", "hello", importance=5)]
        r1 = compact_long_conversation(
            chunks, target_tokens=200, db_execute=db.execute, db_query=db.query,
        )
        r2 = compact_long_conversation(
            chunks, target_tokens=200, db_execute=db.execute, db_query=db.query,
        )
        # Only one INSERT emitted; the second run reported persisted via SELECT.
        self.assertEqual(len(db.insert_calls), 1)
        self.assertEqual(len(db.select_calls), 2)
        self.assertTrue(r1.persisted)
        self.assertTrue(r2.persisted)
        # compressed_id of the second run is the existing row id.
        self.assertEqual(r1.compressed_id, r2.compressed_id)

    def test_compact_handles_db_execute_failure(self):
        """If db_execute raises or returns None, persisted stays False."""
        def bad_execute(sql, params):
            raise RuntimeError("db down")

        chunks = [_make_chunk("c1", "hello", importance=5)]
        result = compact_long_conversation(chunks, target_tokens=200, db_execute=bad_execute)
        self.assertFalse(result.persisted)
        self.assertEqual(result.persisted_skipped_reason, "db_execute_failed")
        self.assertIsNone(result.compressed_id)


# ============================================================================
# 8. Dedup + edge behavior
# ============================================================================

class _DedupAndSource(unittest.TestCase):
    def test_compact_dedupes_near_duplicates_via_overlap(self):
        """Near-duplicate content detected by the overlap fingerprint."""
        chunks = [
            # Same first 16 chars; dedupe by overlap.
            _make_chunk("c1", "the quick brown fox jumps over the lazy dog", importance=4),
            _make_chunk("c2", "the quick brown fox jumps over the lazy cat", importance=9),
            _make_chunk("c3", "totally unrelated", importance=5),
        ]
        result = compact_long_conversation(chunks, target_tokens=300)
        # c2 (importance 9) wins over c1.
        self.assertIn("c2", result.kept_chunk_ids)
        self.assertNotIn("c1", result.kept_chunk_ids)
        self.assertIn("c3", result.kept_chunk_ids)
        self.assertIn("c1", result.dropped_chunk_ids)

    def test_compact_source_chunk_id_override(self):
        """Explicit source_chunk_id forces the memory_compressed row id."""
        db = _FakeDB()
        chunks = [_make_chunk("c1", "hi", importance=5)]
        result = compact_long_conversation(
            chunks, target_tokens=200,
            source_chunk_id="conv_xyz",
            db_execute=db.execute, db_query=db.query,
        )
        self.assertEqual(result.source_chunk_id, "conv_xyz")
        self.assertEqual(db.rows[0]["source_chunk_id"], "conv_xyz")

    def test_compact_synthetic_source_chunk_id_when_no_ids(self):
        """Chunks without ids fall back to a synthetic prefix + user_id."""
        chunks = [{"user_id": "u_alice", "content": "hello", "importance": 5}]
        result = compact_long_conversation(chunks, target_tokens=200)
        self.assertTrue(result.source_chunk_id.startswith("compacted:"))
        self.assertIn("u_alice", result.source_chunk_id)

    def test_compact_importance_field_is_max_kept(self):
        """importance written to memory_compressed is max of kept set."""
        db = _FakeDB()
        chunks = [
            _make_chunk("c1", "low1", importance=2),
            _make_chunk("c2", "high", importance=8),
            _make_chunk("c3", "low2", importance=3),
        ]
        result = compact_long_conversation(
            chunks, target_tokens=300,
            db_execute=db.execute, db_query=db.query,
        )
        self.assertEqual(result.importance, 8.0)
        self.assertEqual(db.rows[0]["importance"], 8.0)


# ============================================================================
# 9. Public surface + Result dataclass
# ============================================================================

class _PublicSurface(unittest.TestCase):
    def test_compact_module_public_surface(self):
        """All advertised symbols exposed via the ``consolidate`` namespace."""
        self.assertTrue(callable(compact_long_conversation))
        self.assertTrue(callable(summarize_chunks))
        self.assertTrue(isinstance(CompactionResult, type))
        self.assertIsInstance(DEFAULT_IMPORTANCE_THRESHOLD, float)
        self.assertEqual(DEFAULT_IMPORTANCE_THRESHOLD, 6.0)
        self.assertTrue(callable(CompactionResult.to_dict))

    def test_compact_result_to_dict_roundtrip(self):
        """to_dict produces a JSON-friendly dict with all fields."""
        r = CompactionResult(
            summary="s", summary_tokens=1, kept_chunk_ids=["a"],
            dropped_chunk_ids=["b"], skipped_chunk_ids=["c"],
            key_facts_preserved=["a"],
            source_chunk_id="a", importance=5.0, persisted=False,
        )
        d = r.to_dict()
        for f in ("summary", "summary_tokens", "kept_chunk_ids",
                  "dropped_chunk_ids", "skipped_chunk_ids",
                  "key_facts_preserved", "source_chunk_id",
                  "importance", "persisted"):
            self.assertIn(f, d)


class _UnittestMirror(unittest.TestCase):
    """Plain ``unittest`` mirror of the 4 dispatch-mandated tests."""

    def test_compact_preserves_key_facts(self):
        chunks = [
            _make_chunk("c1", "filler", importance=1),
            _make_chunk("c2", "USER LIVES IN TOKYO", importance=9),
            _make_chunk("c3", "filler2", importance=2),
        ]
        result = compact_long_conversation(chunks, target_tokens=15)
        self.assertIn("c2", result.key_facts_preserved)
        self.assertIn("TOKYO", result.summary.upper())

    def test_compact_drops_redundant_chunks(self):
        chunks = [
            _make_chunk("a", "same thing", importance=2),
            _make_chunk("b", "same thing", importance=8),
        ]
        result = compact_long_conversation(chunks, target_tokens=100)
        self.assertEqual(result.kept_chunk_ids, ["b"])
        self.assertEqual(result.dropped_chunk_ids, ["a"])

    def test_compact_respects_target_tokens(self):
        chunks = [_make_chunk(str(i), "x" * 400, importance=1) for i in range(20)]
        result = compact_long_conversation(chunks, target_tokens=64)
        self.assertLessEqual(result.summary_tokens, 64)

    def test_compact_idempotent(self):
        chunks = [
            _make_chunk("c1", "alpha", importance=5),
            _make_chunk("c2", "beta", importance=7),
        ]
        r1 = compact_long_conversation(chunks, target_tokens=200)
        r2 = compact_long_conversation(chunks, target_tokens=200)
        self.assertEqual(r1.summary, r2.summary)
        self.assertEqual(r1.kept_chunk_ids, r2.kept_chunk_ids)
        self.assertEqual(r1.dropped_chunk_ids, r2.dropped_chunk_ids)
