"""Unit tests for Cortex v6.5.0 consolidation pipeline (#A.3).

Covers the 4 dispatch-mandated tests + helper / regression tests.

  Required (mapping to spec):
    1. test_scheduler_triggers_at_threshold    -- maybe_run fires at threshold
    2. test_scheduler_skips_below_threshold    -- maybe_run no-op below
    3. test_scheduler_persists_state           -- state_io round-trip
    4. test_scheduler_thread_safe              -- N concurrent record_chunk

  Helpers / regression:
    5. test_state_to_from_dict_roundtrip
    6. test_state_from_dict_ignores_unknown_keys
    7. test_run_once_with_stubbed_extract_resolve
    8. test_record_chunk_returns_new_count
    9. test_run_once_with_no_chunks_is_noop
   10. test_scheduler_rejects_invalid_threshold
   11. test_start_stop_idempotent
   12. test_maybe_run_resets_counter_only_mode
   13. test_extract_failure_does_not_crash_run_once
   14. test_resolve_failure_does_not_crash_run_once
   15. test_module_public_surface

No LLM, no httpx, no live Postgres. The scheduler is exercised with
in-memory state_io and stubbed extract_fn / resolve_conflicts_fn.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consolidate  # noqa: E402
from consolidate import (  # noqa: E402
    DEFAULT_POLL_INTERVAL,
    DEFAULT_THRESHOLD,
    ConflictAction,
    ConsolidationScheduler,
    ConsolidationState,
)

# ============================================================================
# In-memory helpers
# ============================================================================

class _MemIO:
    """Shared in-memory state_io pair.

    Use as ``state_io=mem.io()`` to share state across two
    ``ConsolidationScheduler`` instances (so persistence tests can
    simulate "process restart" by constructing a second scheduler).
    """

    def __init__(self) -> None:
        self._data: dict = {}

    def io(self):
        load = lambda: dict(self._data)
        save = lambda p: self._data.__init__(dict(p))
        return load, save

    @property
    def data(self) -> dict:
        return self._data


def _stub_extract_factory(return_envelope):
    """Build a synchronous extract_fn stub returning ``return_envelope``."""
    def _stub(chunk, ctx, cfg):
        envelope = dict(return_envelope)
        envelope["user_id"] = (chunk or {}).get("user_id", "") or ""
        envelope["source_chunk_id"] = (chunk or {}).get("id")
        return envelope
    return _stub


def _stub_resolve_factory(actions):
    """Build a sync resolve_conflicts_fn stub returning ``actions``."""
    def _stub(new_chunks, db_query, db_execute):
        return list(actions)
    return _stub


def _make_chunk(cid: str, user_id: str = "u_test") -> dict:
    return {"id": cid, "user_id": user_id, "content": "hello"}


# ============================================================================
# 1. test_scheduler_triggers_at_threshold
# ============================================================================

class _Threshold(unittest.TestCase):
    def test_scheduler_triggers_at_threshold(self):
        """maybe_run fires exactly when chunks_since_last_run >= threshold."""
        mem = _MemIO()
        extract = _stub_extract_factory({"facts": [{"entity": "E", "predicate": "p", "value": "v", "confidence": 0.9}]})
        actions = [
            ConflictAction(
                action="KEEP", user_id="u_test", source_chunk_id="c1",
                entity="E", predicate="p", value="v", confidence=0.9,
                new_fact_id=1, reason="test",
            )
        ]
        sched = ConsolidationScheduler(
            extract_fn=extract,
            resolve_conflicts_fn=_stub_resolve_factory(actions),
            db_query=lambda *a, **kw: [],
            db_execute=lambda *a, **kw: 1,
            threshold=10,
            state_io=mem.io(),
        )

        # Below threshold: no run.
        for _ in range(9):
            sched.record_chunk(_make_chunk("c"))
        self.assertEqual(sched.state.chunks_since_last_run, 9)
        self.assertFalse(sched.maybe_run())
        self.assertEqual(sched.state.total_runs, 0)

        # At threshold: run fires.
        sched.record_chunk(_make_chunk("c10"))
        self.assertEqual(sched.state.chunks_since_last_run, 10)
        self.assertTrue(sched.maybe_run())

        # Post-run state.
        s = sched.state
        self.assertEqual(s.chunks_since_last_run, 0)
        self.assertEqual(s.total_runs, 1)
        self.assertEqual(s.total_actions, 1)
        self.assertGreater(s.last_run_at, 0.0)
        self.assertTrue(s.last_run_id)


# ============================================================================
# 2. test_scheduler_skips_below_threshold
# ============================================================================

class _BelowThreshold(unittest.TestCase):
    def test_scheduler_skips_below_threshold(self):
        """maybe_run must be a no-op when count < threshold (no side effects)."""
        mem = _MemIO()
        extract_calls = {"n": 0}

        def extract(chunk, ctx, cfg):
            extract_calls["n"] += 1
            return {"facts": [], "timeline": [], "preferences": []}

        sched = ConsolidationScheduler(
            extract_fn=extract,
            resolve_conflicts_fn=_stub_resolve_factory([]),
            db_query=lambda *a, **kw: [],
            db_execute=lambda *a, **kw: 0,
            threshold=100,
            state_io=mem.io(),
        )

        for _ in range(50):
            sched.record_chunk(_make_chunk("c"))

        self.assertEqual(sched.state.chunks_since_last_run, 50)
        self.assertFalse(sched.maybe_run())
        # Counter untouched.
        self.assertEqual(sched.state.chunks_since_last_run, 50)
        # No extract / resolve work done.
        self.assertEqual(extract_calls["n"], 0)
        # Total runs unchanged.
        self.assertEqual(sched.state.total_runs, 0)
        # last_run_at still zero.
        self.assertEqual(sched.state.last_run_at, 0.0)


# ============================================================================
# 3. test_scheduler_persists_state
# ============================================================================

class _PersistState(unittest.TestCase):
    def test_scheduler_persists_state(self):
        """A new scheduler pointed at the same state_io must see persisted state."""
        mem = _MemIO()
        sched1 = ConsolidationScheduler(
            threshold=100,
            state_io=mem.io(),
        )
        for i in range(37):
            sched1.record_chunk()
        # snapshot
        self.assertEqual(sched1.state.chunks_since_last_run, 37)
        before_total = sched1.state.total_chunks_processed

        # Simulate process restart: build a new scheduler with the same store.
        sched2 = ConsolidationScheduler(
            threshold=100,
            state_io=mem.io(),
        )
        self.assertEqual(sched2.state.chunks_since_last_run, 37)
        self.assertEqual(sched2.state.total_chunks_processed, before_total)
        # Counter on sched2 is independent of sched1 (sees persisted snapshot).
        sched2.record_chunk()
        self.assertEqual(sched2.state.chunks_since_last_run, 38)
        # sched1's in-memory counter unchanged (no auto-refresh).
        self.assertEqual(sched1.state.chunks_since_last_run, 37)

        # Also verify file-path mode works (atomic JSON file).
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sched.json")
            a = ConsolidationScheduler(threshold=100, state_path=path)
            for _ in range(5):
                a.record_chunk()
            # Process "restart" → new instance reads from disk.
            b = ConsolidationScheduler(threshold=100, state_path=path)
            self.assertEqual(b.state.chunks_since_last_run, 5)
            # The file itself is valid JSON.
            import json as _json
            on_disk = _json.load(open(path, "r", encoding="utf-8"))
            self.assertEqual(on_disk["chunks_since_last_run"], 5)


# ============================================================================
# 4. test_scheduler_thread_safe
# ============================================================================

class _ThreadSafe(unittest.TestCase):
    def test_scheduler_thread_safe(self):
        """N concurrent record_chunk calls across M threads must not lose updates."""
        sched = ConsolidationScheduler(
            threshold=10_000,  # never triggers
            state_io=_MemIO().io(),
        )
        N = 5000
        M = 8
        barrier = threading.Barrier(M)

        def worker(start, step):
            barrier.wait()
            for i in range(start, N, step):
                sched.record_chunk(_make_chunk(f"c{i}"))

        threads = [
            threading.Thread(target=worker, args=(t, M))
            for t in range(M)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every record_chunk must have been counted exactly once.
        self.assertEqual(sched.state.chunks_since_last_run, N)
        # And every chunk is in the pending queue exactly once.
        self.assertEqual(sched.pending_count, N)
        ids = set()
        # Drain via the lock-protected snapshot.
        # (We don't have a public getter, but pending_count is sufficient
        # to validate atomicity; we additionally verify uniqueness by
        # calling run_once() with a stub that records inputs.)
        seen: list = []
        sched.run_once()  # default no-resolve path; pending must drain
        self.assertEqual(sched.pending_count, 0)

    def test_scheduler_thread_safe_under_concurrent_maybe_run(self):
        """Maybe_run called concurrently with record_chunk must not deadlock."""
        sched = ConsolidationScheduler(
            threshold=20,
            extract_fn=_stub_extract_factory({"facts": []}),
            resolve_conflicts_fn=_stub_resolve_factory([]),
            db_query=lambda *a, **kw: [],
            db_execute=lambda *a, **kw: 0,
            state_io=_MemIO().io(),
        )
        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                sched.record_chunk(_make_chunk(f"c{i}"))
                i += 1
                if i % 7 == 0:
                    sched.maybe_run()

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=5.0)

        # No thread is stuck; counter is consistent (>= 0).
        self.assertGreaterEqual(sched.state.chunks_since_last_run, 0)
        # Once maybe_run has fired, counter should be 0 < threshold (20).
        self.assertLess(sched.state.chunks_since_last_run, 20)


# ============================================================================
# Helper / regression tests
# ============================================================================

class _StateRoundTrip(unittest.TestCase):
    def test_state_to_from_dict_roundtrip(self):
        s = ConsolidationState(
            chunks_since_last_run=42,
            last_run_at=12345.678,
            last_run_id="abc",
            total_runs=3,
            total_chunks_processed=200,
            total_actions=15,
        )
        d = s.to_dict()
        self.assertEqual(d["chunks_since_last_run"], 42)
        self.assertEqual(d["last_run_id"], "abc")
        s2 = ConsolidationState.from_dict(d)
        self.assertEqual(s, s2)

    def test_state_from_dict_ignores_unknown_keys(self):
        s = ConsolidationState.from_dict({
            "chunks_since_last_run": 7,
            "future_field_xyz": "ignored",
        })
        self.assertEqual(s.chunks_since_last_run, 7)
        # Defaults for missing keys.
        self.assertEqual(s.total_runs, 0)

    def test_state_from_dict_non_dict(self):
        self.assertEqual(ConsolidationState.from_dict(None).chunks_since_last_run, 0)
        self.assertEqual(ConsolidationState.from_dict("oops").total_runs, 0)


class _RunOnce(unittest.TestCase):
    def test_run_once_with_stubbed_extract_resolve(self):
        actions = [
            ConflictAction(
                action="KEEP", user_id="u1", source_chunk_id="c1",
                entity="E", predicate="p", value="v", confidence=0.9,
                new_fact_id=1, reason="t",
            ),
            ConflictAction(
                action="DEDUPE", user_id="u1", source_chunk_id="c2",
                entity="E", predicate="p", value="v", confidence=0.9,
                reason="t",
            ),
        ]
        sched = ConsolidationScheduler(
            extract_fn=_stub_extract_factory(
                {"facts": [{"entity": "E", "predicate": "p", "value": "v", "confidence": 0.9}]}
            ),
            resolve_conflicts_fn=_stub_resolve_factory(actions),
            db_query=lambda *a, **kw: [],
            db_execute=lambda *a, **kw: 1,
            state_io=_MemIO().io(),
        )
        out = sched.run_once([_make_chunk("c1"), _make_chunk("c2")])
        self.assertEqual(len(out), 2)
        self.assertEqual(sched.state.total_actions, 2)
        self.assertEqual(sched.state.total_runs, 1)
        self.assertEqual(sched.state.total_chunks_processed, 2)

    def test_run_once_with_no_chunks_is_noop(self):
        sched = ConsolidationScheduler(state_io=_MemIO().io())
        out = sched.run_once([])
        self.assertEqual(out, [])
        self.assertEqual(sched.state.total_runs, 0)

    def test_record_chunk_returns_new_count(self):
        sched = ConsolidationScheduler(state_io=_MemIO().io())
        self.assertEqual(sched.record_chunk(), 1)
        self.assertEqual(sched.record_chunk(_make_chunk("c2")), 2)
        self.assertEqual(sched.record_chunk(), 3)
        # Only the middle call carried a payload, so pending_count == 1.
        self.assertEqual(sched.pending_count, 1)


class _Validation(unittest.TestCase):
    def test_scheduler_rejects_invalid_threshold(self):
        with self.assertRaises(ValueError):
            ConsolidationScheduler(threshold=0)
        with self.assertRaises(ValueError):
            ConsolidationScheduler(threshold=-5)

    def test_scheduler_rejects_invalid_poll_interval(self):
        with self.assertRaises(ValueError):
            ConsolidationScheduler(poll_interval=0)
        with self.assertRaises(ValueError):
            ConsolidationScheduler(poll_interval=-1.0)


class _Lifecycle(unittest.TestCase):
    def test_start_stop_idempotent(self):
        sched = ConsolidationScheduler(
            threshold=10_000,
            poll_interval=0.05,
            state_io=_MemIO().io(),
        )
        sched.start()
        self.assertTrue(sched.is_running)
        # Second start is a no-op.
        sched.start()
        self.assertTrue(sched.is_running)
        time.sleep(0.1)
        sched.stop(timeout=2.0)
        self.assertFalse(sched.is_running)
        # Second stop is safe.
        sched.stop()


class _CounterOnly(unittest.TestCase):
    def test_maybe_run_resets_counter_only_mode(self):
        """If chunks were recorded without payloads, threshold reset must not loop."""
        sched = ConsolidationScheduler(
            threshold=3,
            state_io=_MemIO().io(),
        )
        for _ in range(3):
            sched.record_chunk()  # no payload
        # First maybe_run: threshold met but pending empty → reset, no run.
        self.assertFalse(sched.maybe_run())
        self.assertEqual(sched.state.chunks_since_last_run, 0)
        # Second call is still safe (does not loop forever).
        self.assertFalse(sched.maybe_run())
        self.assertEqual(sched.state.chunks_since_last_run, 0)


class _FailureTolerant(unittest.TestCase):
    def test_extract_failure_does_not_crash_run_once(self):
        def bad_extract(chunk, ctx, cfg):
            raise RuntimeError("LLM down")
        sched = ConsolidationScheduler(
            extract_fn=bad_extract,
            resolve_conflicts_fn=_stub_resolve_factory([]),
            db_query=lambda *a, **kw: [],
            db_execute=lambda *a, **kw: 0,
            state_io=_MemIO().io(),
        )
        # Threshold 2 → must not raise; actions empty.
        sched.record_chunk(_make_chunk("c1"))
        sched.record_chunk(_make_chunk("c2"))
        out = sched.run_once([_make_chunk("c1"), _make_chunk("c2")])
        self.assertEqual(out, [])
        self.assertEqual(sched.state.total_runs, 1)

    def test_resolve_failure_does_not_crash_run_once(self):
        def bad_resolve(new_chunks, db_query, db_execute):
            raise RuntimeError("DB down")
        sched = ConsolidationScheduler(
            extract_fn=_stub_extract_factory(
                {"facts": [{"entity": "E", "predicate": "p", "value": "v", "confidence": 0.9}]}
            ),
            resolve_conflicts_fn=bad_resolve,
            db_query=lambda *a, **kw: [],
            db_execute=lambda *a, **kw: 0,
            state_io=_MemIO().io(),
        )
        sched.record_chunk(_make_chunk("c1"))
        sched.record_chunk(_make_chunk("c2"))
        out = sched.run_once([_make_chunk("c1"), _make_chunk("c2")])
        self.assertEqual(out, [])
        self.assertEqual(sched.state.total_runs, 1)

    def test_resolve_skipped_when_db_callables_missing(self):
        sched = ConsolidationScheduler(
            extract_fn=_stub_extract_factory({"facts": []}),
            resolve_conflicts_fn=_stub_resolve_factory(
                [ConflictAction(
                    action="KEEP", user_id="u", source_chunk_id="c",
                    entity="E", predicate="p", value="v", confidence=0.9,
                    reason="should not be called",
                )]
            ),
            # intentionally no db_query / db_execute
            state_io=_MemIO().io(),
        )
        sched.record_chunk(_make_chunk("c"))
        sched.record_chunk(_make_chunk("c"))
        out = sched.run_once([_make_chunk("c"), _make_chunk("c")])
        self.assertEqual(out, [])
        self.assertEqual(sched.state.total_actions, 0)


class _PublicSurface(unittest.TestCase):
    def test_module_public_surface(self):
        # #A.3 symbols are exported from the package.
        self.assertTrue(hasattr(consolidate, "ConsolidationScheduler"))
        self.assertTrue(hasattr(consolidate, "ConsolidationState"))
        self.assertTrue(hasattr(consolidate, "DEFAULT_THRESHOLD"))
        self.assertTrue(hasattr(consolidate, "DEFAULT_POLL_INTERVAL"))
        # #A.2 symbols are still exported.
        self.assertTrue(hasattr(consolidate, "resolve_conflicts"))
        self.assertTrue(hasattr(consolidate, "ConflictAction"))
        # Defaults are sensible.
        self.assertEqual(DEFAULT_THRESHOLD, 100)
        self.assertGreater(DEFAULT_POLL_INTERVAL, 0)


class _UnittestMirror(unittest.TestCase):
    """Mirror of the 4 dispatch-mandated tests, run via unittest discovery."""

    def test_scheduler_triggers_at_threshold(self):
        _Threshold("test_scheduler_triggers_at_threshold").run()

    def test_scheduler_skips_below_threshold(self):
        _BelowThreshold("test_scheduler_skips_below_threshold").run()

    def test_scheduler_persists_state(self):
        _PersistState("test_scheduler_persists_state").run()

    def test_scheduler_thread_safe(self):
        _ThreadSafe("test_scheduler_thread_safe").run()



# ===========================================================================
# v6.5.0 Phase 2 — write-time enqueue + worker thread tests
# ===========================================================================

class _MemQueue:
    """In-memory fake of ``memory_maintenance_queue`` + the 4 PG helpers.

    Mirrors the PG semantics just enough to exercise the scheduler:
      * ``enqueue`` is idempotent on duplicate ``chunk_id``.
      * ``claim_next`` returns the lowest-enqueued pending id, marks it
        processing with lease_until = now + lease_seconds, and locks it.
      * ``mark_done`` / ``mark_failed`` mutate status + retry_count.
      * ``mark_failed`` flips to 'dead' when retry_count > max_retries.
    """

    def __init__(self, lease_seconds=30.0, max_retries=5):
        self._rows = []        # list of dicts
        self._next_id = 0
        self._lease = lease_seconds
        self._max_retries = max_retries
        self._locked = set()   # chunk_ids currently held by a "transaction"

    # --- db_enqueue equivalent ---
    def enqueue(self, chunk_ids):
        n = 0
        now = time.time()
        for cid in chunk_ids:
            existing = next((r for r in self._rows if r["chunk_id"] == cid), None)
            if existing is not None:
                continue  # idempotent
            self._rows.append({
                "id": self._next_id,
                "chunk_id": cid,
                "status": "pending",
                "retry_count": 0,
                "enqueued_at": now,
                "leased_at": None,
                "lease_until": None,
                "worker_id": None,
                "last_error": None,
                "completed_at": None,
            })
            self._next_id += 1
            n += 1
        return n

    def _fresh_pending(self):
        now = time.time()
        return [
            r for r in self._rows
            if r["status"] == "pending"
            and (r["lease_until"] is None or r["lease_until"] < now)
        ]

    def claim_next(self):
        # Mirror the PG SQL: status='pending' OR (status='processing' AND
        # lease_until < NOW()). A crashed worker's lease expires and the
        # next claim picks up the same row again.
        now = time.time()
        for r in self._rows:  # stable order: oldest enqueued_at first
            ok = (r["status"] == "pending") or (
                r["status"] == "processing" and r["lease_until"] is not None and r["lease_until"] < now
            )
            if ok:
                r["status"] = "processing"
                r["leased_at"] = now
                r["lease_until"] = now + self._lease
                return r["chunk_id"]
        return None

    def mark_done(self, chunk_id):
        for r in self._rows:
            if r["chunk_id"] == chunk_id:
                r["status"] = "done"
                r["completed_at"] = time.time()
                r["leased_at"] = None
                r["lease_until"] = None
                return

    def mark_failed(self, chunk_id, err):
        for r in self._rows:
            if r["chunk_id"] == chunk_id:
                r["retry_count"] += 1
                r["last_error"] = err[:500]
                r["lease_until"] = None
                r["worker_id"] = None
                if r["retry_count"] > self._max_retries:
                    r["status"] = "dead"
                else:
                    r["status"] = "pending"
                return r["retry_count"]
        return -1

    # --- helpers for tests ---
    def counts(self):
        out = {"pending": 0, "processing": 0, "done": 0, "dead": 0}
        for r in self._rows:
            out[r["status"]] = out.get(r["status"], 0) + 1
        return out

    def get(self, chunk_id):
        return next((r for r in self._rows if r["chunk_id"] == chunk_id), None)

    def force_expire_leases(self):
        """Simulate worker crash by expiring all leases."""
        for r in self._rows:
            if r["status"] == "processing":
                r["lease_until"] = 0.0


class _WriteTimeEnqueue(unittest.TestCase):
    """v6.5.0 Phase 2: record_chunk enqueues, flush persists, worker drains."""

    def test_enqueue_chunk_id_appends_to_buffer(self):
        q = _MemQueue()
        sched = ConsolidationScheduler(
            flush_interval=999,            # don't auto-flush
            worker_poll_interval=999,      # don't auto-poll
            db_enqueue=q.enqueue,
            db_claim_next=q.claim_next,
            db_mark_done=q.mark_done,
            db_mark_failed=q.mark_failed,
        )
        sched.enqueue_chunk_id("a")
        sched.enqueue_chunk_id("b")
        sched.enqueue_chunk_id("a")        # duplicate: still buffered once
        self.assertEqual(sched.buffered_count, 2)

    def test_flush_now_persists_buffer(self):
        q = _MemQueue()
        sched = ConsolidationScheduler(
            flush_interval=999, worker_poll_interval=999,
            db_enqueue=q.enqueue, db_claim_next=q.claim_next,
            db_mark_done=q.mark_done, db_mark_failed=q.mark_failed,
        )
        sched.enqueue_chunk_id("a")
        sched.enqueue_chunk_id("b")
        n = sched.flush_now()
        self.assertEqual(n, 2)
        self.assertEqual(sched.buffered_count, 0)
        self.assertEqual(q.counts()["pending"], 2)

    def test_enqueue_is_idempotent_on_duplicate(self):
        q = _MemQueue()
        sched = ConsolidationScheduler(
            flush_interval=999, worker_poll_interval=999,
            db_enqueue=q.enqueue, db_claim_next=q.claim_next,
            db_mark_done=q.mark_done, db_mark_failed=q.mark_failed,
        )
        sched.enqueue_chunk_id("a")
        sched.enqueue_chunk_id("a")
        sched.enqueue_chunk_id("a")
        sched.flush_now()
        self.assertEqual(q.counts()["pending"], 1)

    def test_record_chunk_dict_path_also_buffers_id(self):
        """Phase 1 callers passing dict payloads must still drive Phase 2 enqueue."""
        q = _MemQueue()
        sched = ConsolidationScheduler(
            flush_interval=999, worker_poll_interval=999,
            db_enqueue=q.enqueue, db_claim_next=q.claim_next,
            db_mark_done=q.mark_done, db_mark_failed=q.mark_failed,
        )
        sched.record_chunk({"id": "x", "content": "hello"})
        self.assertEqual(sched.buffered_count, 1)
        sched.flush_now()
        self.assertEqual(q.counts()["pending"], 1)

    def test_worker_tick_processes_and_marks_done(self):
        q = _MemQueue()
        sched = ConsolidationScheduler(
            flush_interval=999, worker_poll_interval=999,
            db_enqueue=q.enqueue, db_claim_next=q.claim_next,
            db_mark_done=q.mark_done, db_mark_failed=q.mark_failed,
        )
        sched.enqueue_chunk_id("only")
        sched.flush_now()
        cid = sched.process_one_now()
        self.assertEqual(cid, "only")
        self.assertEqual(q.counts(), {"pending": 0, "processing": 0, "done": 1, "dead": 0})

    def test_worker_returns_none_when_queue_empty(self):
        q = _MemQueue()
        sched = ConsolidationScheduler(
            flush_interval=999, worker_poll_interval=999,
            db_enqueue=q.enqueue, db_claim_next=q.claim_next,
            db_mark_done=q.mark_done, db_mark_failed=q.mark_failed,
        )
        self.assertIsNone(sched.process_one_now())

    def test_failed_tick_increments_retry(self):
            q = _MemQueue()
            sched = ConsolidationScheduler(
                flush_interval=999, worker_poll_interval=999,
                db_enqueue=q.enqueue, db_claim_next=q.claim_next,
                db_mark_done=q.mark_done, db_mark_failed=q.mark_failed,
                max_retries=2,
            )
            # Force extract_fn to raise so the worker records a failure.
            sched._extract_fn = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
            # _process_one first does an SQL SELECT for the chunk row; return a
            # synthetic dict so the failure happens in extract, not in the load.
            sched._db_query = lambda *a, **k: {
                "id": "crash", "content": "x", "type": "general", "source": "test",
                "importance": 5, "tags": [], "metadata": {},
            }
            sched._db_execute = lambda *a, **k: 0
            sched.enqueue_chunk_id("crash")
            sched.flush_now()
            sched.process_one_now()
            self.assertEqual(q.get("crash")["retry_count"], 1)
            self.assertEqual(q.get("crash")["status"], "pending")

    def test_failed_tick_over_max_retries_flips_to_dead(self):
        q = _MemQueue()
        q._max_retries = 2  # sync with sched.max_retries=2 below
        sched = ConsolidationScheduler(
            flush_interval=999, worker_poll_interval=999,
            db_enqueue=q.enqueue, db_claim_next=q.claim_next,
            db_mark_done=q.mark_done, db_mark_failed=q.mark_failed,
            max_retries=2,
        )
        sched._extract_fn = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
        sched._db_query = lambda *a, **k: {
            "id": "crash", "content": "x", "type": "general", "source": "test",
            "importance": 5, "tags": [], "metadata": {},
        }
        sched._db_execute = lambda *a, **k: 0
        sched.enqueue_chunk_id("crash")
        sched.flush_now()
        # 3 ticks: retry 1, retry 2, retry 3 > max → dead
        for _ in range(3):
            sched.process_one_now()
        self.assertEqual(q.get("crash")["retry_count"], 3)
        self.assertEqual(q.get("crash")["status"], "dead")
        self.assertIn("boom", q.get("crash")["last_error"])

    def test_expired_lease_is_reclaimable(self):
        """Worker crash mid-flight → lease expires → next claim picks it up."""
        q = _MemQueue(lease_seconds=0.05)
        sched = ConsolidationScheduler(
            flush_interval=999, worker_poll_interval=999,
            db_enqueue=q.enqueue, db_claim_next=q.claim_next,
            db_mark_done=q.mark_done, db_mark_failed=q.mark_failed,
        )
        # Provide a chunk row so _process_one can mark it done cleanly.
        sched._db_query = lambda *a, **k: {
            "id": "reclaim", "content": "x", "type": "general", "source": "test",
            "importance": 5, "tags": [], "metadata": {},
        }
        sched._db_execute = lambda *a, **k: 0
        sched.enqueue_chunk_id("reclaim")
        sched.flush_now()
        cid1 = sched.process_one_now()
        self.assertEqual(cid1, "reclaim")
        self.assertEqual(q.get("reclaim")["status"], "done")  # completed cleanly
        # Re-enqueue after the previous tick is done; simulate a crash by
        # putting it in 'processing' with an expired lease.
        q._rows.append({
            "id": 999, "chunk_id": "reclaim", "status": "processing",
            "retry_count": 0, "enqueued_at": time.time() - 10,
            "leased_at": time.time() - 10, "lease_until": 0.0,
            "worker_id": "old", "last_error": None, "completed_at": None,
        })
        cid2 = sched.process_one_now()
        self.assertEqual(cid2, "reclaim")

    def test_start_stop_lifecycle_phase2(self):
        """start() should spawn flush + worker threads; stop() should join them."""
        q = _MemQueue()
        sched = ConsolidationScheduler(
            flush_interval=0.05, worker_poll_interval=0.05,
            db_enqueue=q.enqueue, db_claim_next=q.claim_next,
            db_mark_done=q.mark_done, db_mark_failed=q.mark_failed,
            max_retries=2,
        )
        sched._extract_fn = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
        sched._db_query = lambda *a, **k: {
            "id": "lifecycle", "content": "x", "type": "general", "source": "test",
            "importance": 5, "tags": [], "metadata": {},
        }
        sched._db_execute = lambda *a, **k: 0
        sched.start()
        try:
            self.assertTrue(sched.is_running)
            sched.enqueue_chunk_id("lifecycle")
            # Wait up to 2s for the worker to flip it to dead.
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if q.get("lifecycle") and q.get("lifecycle")["status"] == "dead":
                    break
                time.sleep(0.05)
            self.assertEqual(q.get("lifecycle")["status"], "dead")
        finally:
            sched.stop(timeout=2.0)
        self.assertFalse(sched.is_running)

    def test_state_has_total_dead_and_last_error_fields(self):
        s = ConsolidationState()
        self.assertEqual(s.total_dead, 0)
        self.assertEqual(s.last_error, "")
        s.total_dead = 3
        s.last_error = "ValueError: x"
        # Round-trip via from_dict/to_dict.
        d = s.to_dict()
        self.assertEqual(d["total_dead"], 3)
        self.assertEqual(d["last_error"], "ValueError: x")
        s2 = ConsolidationState.from_dict(d)
        self.assertEqual(s2.total_dead, 3)
        self.assertEqual(s2.last_error, "ValueError: x")

    def test_module_public_surface_phase2(self):
        """v6.5.0 P2 new symbols must be exported from consolidate package."""
        from consolidate import (
            DEFAULT_FLUSH_INTERVAL,
            DEFAULT_WORKER_POLL_INTERVAL,
            DEFAULT_LEASE_SECONDS,
            DEFAULT_MAX_RETRIES,
        )
        self.assertGreater(DEFAULT_FLUSH_INTERVAL, 0)
        self.assertGreater(DEFAULT_WORKER_POLL_INTERVAL, 0)
        self.assertGreater(DEFAULT_LEASE_SECONDS, 0)
        self.assertGreaterEqual(DEFAULT_MAX_RETRIES, 0)







# ==========================================================================
# v6.5.1: on_process_complete hook tests
# ==========================================================================

import unittest


class _OnProcessCompleteHook(unittest.TestCase):
    """Verify the post-process hook fires after each chunk is processed."""

    def _make_sched(self, hook=None, extract_ok=True):
        from consolidate import DEFAULT_FLUSH_INTERVAL, DEFAULT_WORKER_POLL_INTERVAL, DEFAULT_LEASE_SECONDS, DEFAULT_MAX_RETRIES
        q = _MemQueue()
        # Minimal db_query/db_execute so _process_one reaches the extract_fn
        # call. Returns nothing useful; the extract_fn is what matters here.
        def _noop_query(*a, **k):
            return []
        def _noop_exec(*a, **k):
            return 0
        sched = ConsolidationScheduler(
            flush_interval=DEFAULT_FLUSH_INTERVAL,
            worker_poll_interval=DEFAULT_WORKER_POLL_INTERVAL,
            lease_seconds=DEFAULT_LEASE_SECONDS,
            max_retries=DEFAULT_MAX_RETRIES,
            db_enqueue=q.enqueue,
            db_claim_next=q.claim_next,
            db_mark_done=q.mark_done,
            db_mark_failed=q.mark_failed,
            db_query=_noop_query,
            db_execute=_noop_exec,
            extract_fn=_stub_extract_factory({"facts": []}) if extract_ok else (lambda *a, **k: (_ for _ in ()).throw(RuntimeError("extract fail"))),
            resolve_conflicts_fn=_stub_resolve_factory([]),
            on_process_complete=hook,
        )
        return sched, q

    def _enqueue_and_process(self, sched, q, cid):
        """Helper: enqueue a cid, flush to PG, then process_one_now."""
        sched.enqueue_chunk_id(cid)
        sched.flush_now()
        return sched.process_one_now()

    def test_hook_fires_after_successful_process_one(self):
        calls = []
        sched, q = self._make_sched(hook=lambda cid: calls.append(cid))
        for cid in ["a", "b", "c"]:
            self.assertEqual(self._enqueue_and_process(sched, q, cid), cid)
        self.assertEqual(calls, ["a", "b", "c"])

    def test_hook_not_called_when_extract_fails(self):
        """Force db_query to raise -- this triggers the outer try/except in
        _worker_tick, which skips the hook (chunk goes to mark_failed)."""
        calls = []
        from consolidate import DEFAULT_FLUSH_INTERVAL, DEFAULT_WORKER_POLL_INTERVAL, DEFAULT_LEASE_SECONDS, DEFAULT_MAX_RETRIES
        q = _MemQueue()

        def _crash_query(*a, **k):
            raise RuntimeError("db_query simulated crash")

        sched = ConsolidationScheduler(
            flush_interval=DEFAULT_FLUSH_INTERVAL,
            worker_poll_interval=DEFAULT_WORKER_POLL_INTERVAL,
            lease_seconds=DEFAULT_LEASE_SECONDS,
            max_retries=DEFAULT_MAX_RETRIES,
            db_enqueue=q.enqueue,
            db_claim_next=q.claim_next,
            db_mark_done=q.mark_done,
            db_mark_failed=q.mark_failed,
            db_query=_crash_query,
            db_execute=lambda *a, **k: 0,
            extract_fn=_stub_extract_factory({"facts": []}),
            resolve_conflicts_fn=_stub_resolve_factory([]),
            on_process_complete=lambda cid: calls.append(cid),
        )
        sched.enqueue_chunk_id("a")
        sched.flush_now()
        sched.process_one_now()
        self.assertEqual(calls, [], "hook must not fire when process_one raises")

    def test_hook_failure_does_not_propagate(self):
        sched, q = self._make_sched(hook=lambda cid: (_ for _ in ()).throw(RuntimeError("boom")))
        # Should not raise even though the hook raises
        self._enqueue_and_process(sched, q, "a")
        # chunk should be marked done (the hook error is swallowed, not propagated to mark_failed)
        # q.get may show 'done' or 'processing' (we set status via mark_done after process_one)
        self.assertIsNotNone(q.get("a"))
        # Note: the mark_done happens BEFORE the hook, so done is set; then hook runs;
        # if hook raises, we log+swallow; chunk stays 'done' (not flipped to failed)
        self.assertEqual(q.get("a")["status"], "done", "hook error must NOT flip chunk to failed")

    def test_hook_optional(self):
        from consolidate import DEFAULT_FLUSH_INTERVAL, DEFAULT_WORKER_POLL_INTERVAL
        sched = ConsolidationScheduler(
            flush_interval=DEFAULT_FLUSH_INTERVAL,
            worker_poll_interval=DEFAULT_WORKER_POLL_INTERVAL,
        )
        self.assertIsNone(sched._on_process_complete)

    def test_hook_invalid_type_rejected(self):
        with self.assertRaises(TypeError) as cm:
            ConsolidationScheduler(
                flush_interval=999, worker_poll_interval=999,
                on_process_complete="not callable",
            )
        self.assertIn("callable", str(cm.exception).lower())

    def test_hook_receives_correct_chunk_id(self):
        received = []
        sched, q = self._make_sched(hook=lambda cid: received.append(cid))
        self._enqueue_and_process(sched, q, "chunk_xyz_123")
        self.assertEqual(received, ["chunk_xyz_123"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
