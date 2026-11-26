"""
Cortex v6.5.0 -- Consolidation Pipeline (#A.3): background scheduler.

v6.5.0 Phase 2 (2026-09-01): write-time enqueue model.

Behavior change vs v6.5.0 Phase 1:
  * Phase 1: in-memory deque + ``threshold`` counter + daemon thread polling
    every ``poll_interval`` seconds.
  * Phase 2: write-time enqueue via Postgres ``memory_maintenance_queue`` table.
    The hot path (``record_chunk``) only appends to a thread-local in-memory
    buffer; a flush thread persists to PG every ``flush_interval`` seconds.
    A single worker thread drains the queue with
    ``SELECT ... FOR UPDATE SKIP LOCKED`` so we never block on the hot path
    and never lose chunks on a crash (the queue is durable in PG).

Backward compatibility:
  * ``run_once(chunks)`` / ``maybe_run()`` keep the Phase 1 semantics.
  * ``start()`` / ``stop()`` keep the same lifecycle.
  * ``state`` / ``record_chunk`` are unchanged from Phase 1.

Design constraints:
  * Thread-safe (RLock; reentrant because ``_run_unlocked`` may call back
    into mutators when re-queuing after a crash).
  * Persistable: state survives process restart via ``state_path``.
  * No LLM in the hot path of the scheduler itself; ``extract_fn`` is
    injected so tests can stub it with a synchronous function.
  * Fail-soft: any exception inside the loop is logged and swallowed;
    the worker must keep ticking.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional


from .state import ConsolidationState


logger = logging.getLogger("cortex.consolidate.scheduler")

# Phase 1 defaults (kept for backward compatibility).
DEFAULT_THRESHOLD = 100            # chunks -- M spec §5.2 Step 3
DEFAULT_POLL_INTERVAL = 60.0       # seconds between background checks

# Phase 2 new defaults.
DEFAULT_FLUSH_INTERVAL = 0.2       # seconds between in-memory → PG flushes
DEFAULT_WORKER_POLL_INTERVAL = 1.0 # seconds between PG queue scans
DEFAULT_LEASE_SECONDS = 30.0       # worker lease duration before reclaim
DEFAULT_MAX_RETRIES = 5            # retry_count threshold → status='dead'

EMPTY_ENVELOPE = {"facts": [], "timeline": [], "preferences": []}


def _new_run_id() -> str:
    return f"{int(time.time())}-{secrets.token_hex(4)}"


class ConsolidationScheduler:
    """Thread-safe background scheduler for the consolidation pipeline.

    Phase 2 lifecycle
    -----------------
    1. ``start()``: spawn the flush thread + worker thread.
    2. ``record_chunk(chunk_id)``: append to in-memory buffer (O(1), non-blocking).
    3. Flush thread (every ``flush_interval``): batch-INSERT buffered chunk_ids
       into ``memory_maintenance_queue`` with ``ON CONFLICT DO NOTHING``.
    4. Worker thread (every ``worker_poll_interval``):
       ``SELECT chunk_id FROM memory_maintenance_queue
        WHERE status='pending'
        ORDER BY enqueued_at
        LIMIT 1 FOR UPDATE SKIP LOCKED``;
       set status='processing', lease_until=now+lease_seconds, run extract+resolve,
       on success → status='done'; on exception → retry_count++, status='pending'.
       retry_count > max_retries → status='dead'.
    5. ``stop()``: set stop event, flush thread drains buffer once, worker joins.

    Parameters
    ----------
    cfg : Any
        Forwarded to ``extract_fn``. Production: ``CortexConfig``;
        tests typically pass ``None`` plus a stub ``extract_fn``.
    db_query, db_execute : Callable
        Forwarded to ``resolve_conflicts_fn``. Tests inject in-memory
        fakes; production wires ``server.src.api_pg.query`` / ``.execute``.
    extract_fn : Callable, optional
        ``(chunk, context_chunks, cfg) -> dict``. Default: returns an
        empty envelope so the scheduler can run with no LLM attached.
    resolve_conflicts_fn : Callable, optional
        ``(new_chunks, db_query, db_execute) -> list``. Defaults to
        ``consolidate.conflict_resolve.resolve_conflicts``.
    threshold : int, default 100
        Legacy Phase 1 threshold (kept for ``maybe_run``).
    poll_interval : float, default 60.0
        Legacy Phase 1 polling interval (kept for backward compat).
    flush_interval : float, default 0.2
        Phase 2: seconds between in-memory → PG flushes.
    worker_poll_interval : float, default 1.0
        Phase 2: seconds between PG queue scans.
    lease_seconds : float, default 30.0
        Phase 2: lease duration; ``lease_until`` is set to ``now + lease_seconds``.
    max_retries : int, default 5
        Phase 2: retry_count threshold above which status flips to 'dead'.
    db_enqueue : Callable, optional
        ``(chunk_ids: List[str]) -> int`` Phase 2: batch-insert into queue.
        Production wires to a SQL ``INSERT ... ON CONFLICT DO NOTHING``.
    db_claim_next : Callable, optional
        ``() -> Optional[str]`` Phase 2: claim next pending chunk_id (sets
        lease_until + worker_id). None if no work available.
        Production wires to ``SELECT ... FOR UPDATE SKIP LOCKED``.
    db_mark_done : Callable, optional
        ``(chunk_id: str) -> None`` Phase 2: mark chunk processed.
    db_mark_failed : Callable, optional
        ``(chunk_id: str, error: str) -> int`` Phase 2: increment retry_count;
        returns new retry_count so caller can flip to 'dead' if over threshold.
    state_path : str, optional
        Filesystem path for JSON state persistence. ``None`` disables
        persistence (handy for ephemeral / test instances).
    state_io : (load_fn, save_fn), optional
        Override the default file-based state I/O.
    clock : Callable, optional
        ``() -> float`` epoch source. Tests inject a fake.
    """

    def __init__(
        self,
        *,
        cfg: Any = None,
        db_query: Optional[Callable[..., Any]] = None,
        db_execute: Optional[Callable[..., Any]] = None,
        extract_fn: Optional[Callable[..., Any]] = None,
        resolve_conflicts_fn: Optional[Callable[..., Any]] = None,
        threshold: int = DEFAULT_THRESHOLD,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        worker_poll_interval: float = DEFAULT_WORKER_POLL_INTERVAL,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        db_enqueue: Optional[Callable[[List[str]], int]] = None,
        db_claim_next: Optional[Callable[[], Optional[str]]] = None,
        db_mark_done: Optional[Callable[[str], None]] = None,
        db_mark_failed: Optional[Callable[[str, str], int]] = None,
        # v6.5.1: post-process hook fired after a chunk is successfully
        # processed (after mark_done, before next claim). Use to wire
        # side-effects like auto-creating similar relations. Failures
        # here do NOT trigger retry (chunk is already marked done).
        on_process_complete: Optional[Callable[[str], None]] = None,
        worker_id: Optional[str] = None,
        state_path: Optional[str] = None,
        state_io: Optional[Any] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if threshold <= 0:
            raise ValueError("threshold must be a positive integer")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if flush_interval <= 0:
            raise ValueError("flush_interval must be positive")
        if worker_poll_interval <= 0:
            raise ValueError("worker_poll_interval must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")

        self._cfg = cfg
        self._db_query = db_query
        self._db_execute = db_execute
        self._extract_fn = extract_fn
        self._resolve_fn = resolve_conflicts_fn

        self.threshold = int(threshold)
        self.poll_interval = float(poll_interval)
        self.flush_interval = float(flush_interval)
        self.worker_poll_interval = float(worker_poll_interval)
        self.lease_seconds = float(lease_seconds)
        self.max_retries = int(max_retries)

        self._db_enqueue = db_enqueue
        self._db_claim_next = db_claim_next
        self._db_mark_done = db_mark_done
        self._db_mark_failed = db_mark_failed
        if on_process_complete is not None and not callable(on_process_complete):
            raise TypeError("on_process_complete must be callable or None")
        self._on_process_complete = on_process_complete
        self._worker_id = worker_id or f"{os.uname().nodename if hasattr(os, 'uname') else 'host'}:{os.getpid()}"

        self._clock = clock or time.time

        self._lock = threading.RLock()
        self._pending: List[Dict[str, Any]] = []    # legacy Phase 1 in-memory queue
        self._buffer: List[str] = []                  # Phase 2 in-memory chunk_id buffer
        self._state = ConsolidationState()

        self._stop_event = threading.Event()
        self._flush_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._legacy_thread: Optional[threading.Thread] = None  # Phase 1 background loop

        if state_io is not None:
            self._state_load, self._state_save = state_io
        elif state_path is not None:
            self._state_load = lambda: self._file_load(state_path)
            self._state_save = lambda p: self._file_save(state_path, p)
        else:
            self._state_load = lambda: {}
            self._state_save = lambda p: None
        self._load_state()

    # ----- Public read-only view -----

    @property
    def state(self) -> ConsolidationState:
        """Snapshot copy of the current state (safe to inspect off-thread)."""
        with self._lock:
            return ConsolidationState.from_dict(self._state.to_dict())

    @property
    def pending_count(self) -> int:
        """Legacy Phase 1: in-memory pending. Phase 2 returns 0 (queue is in PG)."""
        with self._lock:
            return len(self._pending)

    @property
    def buffered_count(self) -> int:
        """Phase 2: chunk_ids sitting in the in-memory buffer (not yet flushed)."""
        with self._lock:
            return len(self._buffer)

    @property
    def is_running(self) -> bool:
        return (
            (self._flush_thread is not None and self._flush_thread.is_alive())
            or (self._worker_thread is not None and self._worker_thread.is_alive())
            or (self._legacy_thread is not None and self._legacy_thread.is_alive())
        )

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # ----- Public mutators -----

    def record_chunk(self, chunk: Optional[Dict[str, Any]] = None) -> int:
        """Record one new chunk. Thread-safe.

        Phase 1: increments counter + appends chunk dict to legacy in-memory queue.
        Phase 2: appends ``chunk.get("id")`` (or ``chunk_id`` key) to the new
        in-memory buffer that the flush thread persists to PG.

        Both paths run unconditionally so legacy callers keep working while the
        Phase 2 worker drains the PG queue.

        Returns the new ``chunks_since_last_run`` counter.
        """
        with self._lock:
            self._state.chunks_since_last_run += 1
            if chunk is not None:
                # Legacy Phase 1: keep the dict queue for backward compat.
                self._pending.append(chunk)
                # Phase 2: also enqueue the chunk_id for the PG worker.
                cid = chunk.get("id") or chunk.get("chunk_id")
                if isinstance(cid, str) and cid:
                    self._buffer.append(cid)
            self._save_state()
            return self._state.chunks_since_last_run

    def enqueue_chunk_id(self, chunk_id: str) -> None:
            """Phase 2: pure chunk_id enqueue (no dict payload). Thread-safe.

            Idempotent on duplicate chunk_id within the same in-memory buffer
            (a PG-side ``ON CONFLICT DO NOTHING`` covers duplicates across the
            buffer→PG boundary, but we still dedup in-memory to avoid flushing
            the same id twice).
            """
            if not isinstance(chunk_id, str) or not chunk_id:
                return
            with self._lock:
                if chunk_id in self._buffer:
                    # Already buffered; do not increment counter (it's a re-enqueue).
                    return
                self._buffer.append(chunk_id)
                self._state.chunks_since_last_run += 1
                self._save_state()

    def maybe_run(self) -> bool:
        """Legacy Phase 1: run iff threshold reached. Returns True iff a run happened."""
        with self._lock:
            if self._state.chunks_since_last_run < self.threshold:
                return False
            if not self._pending:
                # Counter-only mode: reset so we do not loop forever.
                self._state.chunks_since_last_run = 0
                self._save_state()
                return False
            pending = list(self._pending)
            self._pending.clear()
        try:
            self._run_unlocked(pending)
            return True
        except Exception:  # noqa: BLE001 -- cron loop must never die
            logger.exception("scheduler maybe_run crashed; state preserved")
            with self._lock:
                self._pending = pending + self._pending
            return False

    def run_once(self, chunks: Optional[List[Dict[str, Any]]] = None) -> List[Any]:
        """Legacy Phase 1: force-run consolidation on ``chunks`` (or the pending queue)."""
        with self._lock:
            if chunks is None:
                chunks = list(self._pending)
                self._pending.clear()
        return self._run_unlocked(chunks)

    def flush_now(self) -> int:
        """Phase 2: synchronously drain in-memory buffer to PG. Returns rows enqueued.

        Useful for tests and for the ``stop()`` final flush.
        """
        return self._flush_once()

    def process_one_now(self) -> Optional[str]:
        """Phase 2: synchronously process one queue item (if available).

        Returns the processed chunk_id, or None if nothing pending.
        """
        return self._worker_tick()

    # ----- Background threads -----

    def start(self) -> None:
        """Start the daemon flush + worker threads (idempotent)."""
        with self._lock:
            if self.is_running:
                return
            self._stop_event.clear()
            started = []

            if self._db_enqueue is not None:
                # Phase 2: flush thread (in-memory → PG).
                self._flush_thread = threading.Thread(
                    target=self._flush_loop,
                    name="consolidation-flush",
                    daemon=True,
                )
                self._flush_thread.start()
                started.append(("flush", self.flush_interval))

            if self._db_claim_next is not None:
                # Phase 2: worker thread (PG → extract+resolve).
                self._worker_thread = threading.Thread(
                    target=self._worker_loop,
                    name="consolidation-worker",
                    daemon=True,
                )
                self._worker_thread.start()
                started.append(("worker", self.worker_poll_interval))

            if not started:
                # Phase 1 fallback: legacy daemon thread.
                self._legacy_thread = threading.Thread(
                    target=self._loop, name="consolidation-scheduler", daemon=True,
                )
                self._legacy_thread.start()
                started.append(("legacy", self.poll_interval))

        logger.info(
            "scheduler started (worker_id=%s, threads=%s, threshold=%d, max_retries=%d)",
            self._worker_id,
            ",".join(f"{name}@{iv:.1f}s" for name, iv in started),
            self.threshold,
            self.max_retries,
        )

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal all loops to exit, do one final flush, and join briefly."""
        self._stop_event.set()
        # Best-effort final flush so buffered chunk_ids land in PG before exit.
        try:
            self._flush_once()
        except Exception:  # noqa: BLE001
            logger.exception("final flush during stop() failed")
        for t in (self._flush_thread, self._worker_thread, self._legacy_thread):
            if t is not None:
                t.join(timeout=timeout)
        self._flush_thread = None
        self._worker_thread = None
        self._legacy_thread = None
        logger.info("scheduler stopped")

    # ----- Loop internals -----

    def _flush_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._flush_once()
            except Exception:  # noqa: BLE001
                logger.exception("flush_loop tick crashed")
            self._stop_event.wait(self.flush_interval)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._worker_tick()
            except Exception:  # noqa: BLE001
                logger.exception("worker_loop tick crashed")
            self._stop_event.wait(self.worker_poll_interval)

    def _flush_once(self) -> int:
        """Drain the in-memory buffer into the PG queue. Returns rows enqueued."""
        with self._lock:
            if not self._buffer:
                return 0
            batch = list(self._buffer)
            self._buffer.clear()
        if self._db_enqueue is None:
            # No PG backend wired — just drop the buffer (tests may still
            # observe buffered_count).
            logger.debug("flush_once: no db_enqueue wired; dropped %d ids", len(batch))
            return 0
        try:
            n = int(self._db_enqueue(batch) or 0)
            logger.debug("flush_once: enqueued %d/%d chunk_ids", n, len(batch))
            return n
        except Exception:  # noqa: BLE001
            logger.exception("flush_once: db_enqueue failed; re-buffering batch")
            with self._lock:
                # Put them back at the front so we retry next tick.
                self._buffer = batch + self._buffer
            return 0

    def _worker_tick(self) -> Optional[str]:
        """One pass: claim next → run extract+resolve → mark done/failed."""
        if self._db_claim_next is None:
            return None
        try:
            cid = self._db_claim_next()
        except Exception:  # noqa: BLE001
            logger.exception("worker_tick: db_claim_next crashed")
            return None
        if cid is None:
            return None
        try:
            self._process_one(cid)
            if self._db_mark_done is not None:
                self._db_mark_done(cid)
            # v6.5.1: post-process hook (e.g., auto-create similar relations
            # from the new chunk's embedding). Best-effort -- if it fails
            # we log and move on; the chunk is already marked done.
            if self._on_process_complete is not None:
                try:
                    self._on_process_complete(cid)
                except Exception as _hook_exc:  # noqa: BLE001
                    logger.warning(
                        "post-process hook failed for chunk_id=%s: %s",
                        cid, _hook_exc,
                    )
            with self._lock:
                self._state.total_chunks_processed += 1
                self._save_state()
            logger.debug("worker_tick: done chunk_id=%s", cid)
            return cid
        except Exception as exc:  # noqa: BLE001 -- worker must keep ticking
            err = f"{type(exc).__name__}: {exc}"[:500]
            logger.exception("worker_tick: process failed chunk_id=%s", cid)
            new_retry = -1
            if self._db_mark_failed is not None:
                try:
                    new_retry = int(self._db_mark_failed(cid, err))
                except Exception:  # noqa: BLE001
                    logger.exception("db_mark_failed itself crashed chunk_id=%s", cid)
            with self._lock:
                self._state.last_error = err
                if new_retry > self.max_retries:
                    self._state.total_dead += 1
                self._save_state()
            return None

    def _process_one(self, chunk_id: str) -> None:
        """Run extract + resolve_conflicts for a single chunk_id.

        Mirrors ``_run_unlocked`` for one item but takes a string id instead
        of a dict payload. Looks up the chunk row via ``db_query``.
        """
        if self._db_query is None or self._db_execute is None:
            logger.warning(
                "worker tick without db_query/db_execute; cannot load chunk %s",
                chunk_id,
            )
            return

        # Load chunk row. Tolerate dict or list[dict] (api_pg.py default
        # db_query returns list of dicts even for single-row SELECT).
        try:
            row = self._db_query(
                "SELECT id, content, type, source, importance, tags, metadata "
                "FROM memory_chunks WHERE id = %s",
                (chunk_id,),
            )
        except Exception:  # noqa: BLE001
            logger.exception("chunk load failed for %s", chunk_id)
            raise

        # Adapter normalization: pick the single row.
        if isinstance(row, list):
            if not row:
                # Stale queue entry (chunk was deleted). Mark done silently.
                return
            row = row[0]

        chunk_dict: Dict[str, Any] = {"id": chunk_id}
        if row is None:
            # Stale queue entry (chunk was deleted). Mark done silently.
            return
        if isinstance(row, dict):
            chunk_dict.update({
                "id": row.get("id") or chunk_id,
                "content": row.get("content") or "",
                "type": row.get("type") or "general",
                "source": row.get("source") or "",
                "importance": row.get("importance") or 5,
                "tags": row.get("tags") or [],
                "metadata": row.get("metadata") or {},
            })
        else:
            # tuple/sequence: column order matches SELECT above.
            try:
                (cid, content, ctype, csource, cimp, ctags, cmeta) = row
            except Exception:  # noqa: BLE001
                logger.warning("chunk row shape unexpected for %s", chunk_id)
                return
            chunk_dict.update({
                "id": cid,
                "content": content or "",
                "type": ctype or "general",
                "source": csource or "",
                "importance": cimp or 5,
                "tags": ctags or [],
                "metadata": cmeta or {},
            })

        # Run extract on the loaded chunk. Phase 2 worker semantics:
        # propagate failures so worker_tick increments retry_count and
        # can flip to status='dead' past max_retries.
        envelope = self._extract_one(chunk_dict, propagate=True)

        # Resolve conflicts.
        resolve_fn = self._resolve_fn
        if resolve_fn is None:
            from .conflict_resolve import resolve_conflicts as _rc
            resolve_fn = _rc

        try:
            actions = list(resolve_fn([envelope], self._db_query, self._db_execute))
        except Exception:  # noqa: BLE001
            logger.exception("resolve_conflicts crashed for %s", chunk_id)
            raise

        with self._lock:
            self._state.last_run_at = float(self._clock())
            self._state.last_run_id = _new_run_id()
            self._state.total_runs += 1
            self._state.total_actions += len(actions)
            self._save_state()

    # ----- Legacy Phase 1 background loop -----

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.maybe_run():
                    logger.info("scheduler triggered run")
            except Exception:  # noqa: BLE001
                logger.exception("scheduler loop tick crashed")
            self._stop_event.wait(self.poll_interval)

    # ----- Legacy Phase 1 internals (unchanged) -----

    def _run_unlocked(self, chunks: List[Dict[str, Any]]) -> List[Any]:
        """Execute extract -> resolve. Caller must NOT hold ``self._lock``."""
        if not chunks:
            return []
        extracted: List[Dict[str, Any]] = []
        for chunk in chunks:
            extracted.append(self._extract_one(chunk))

        resolve_fn = self._resolve_fn
        if resolve_fn is None:
            from .conflict_resolve import resolve_conflicts as _rc
            resolve_fn = _rc

        actions: List[Any] = []
        if self._db_query is None or self._db_execute is None:
            logger.warning(
                "scheduler run_once without db_query/db_execute; "
                "skipping resolve_conflicts"
            )
        else:
            try:
                actions = list(
                    resolve_fn(extracted, self._db_query, self._db_execute)
                )
            except Exception:  # noqa: BLE001
                logger.exception("resolve_conflicts crashed in scheduler")
                actions = []

        non_empty = sum(
            1 for e in extracted
            if isinstance(e, dict) and (
                e.get("facts") or e.get("timeline") or e.get("preferences")
            )
        )
        with self._lock:
            self._state.chunks_since_last_run = 0
            self._state.last_run_at = float(self._clock())
            self._state.last_run_id = _new_run_id()
            self._state.total_runs += 1
            self._state.total_chunks_processed += non_empty
            self._state.total_actions += len(actions)
            self._save_state()
        logger.info(
            "scheduler run complete: chunks=%d non_empty=%d actions=%d run_id=%s",
            len(chunks), non_empty, len(actions), self._state.last_run_id,
        )
        return actions

    def _extract_one(self, chunk: Any, propagate: bool = False) -> Dict[str, Any]:
        """Run ``extract_fn`` for one chunk, with envelope + metadata merge.

        ``propagate=False`` (default, Phase 1 ``run_once`` callers): swallow
        ``extract_fn`` exceptions and return the empty envelope so the
        legacy ``run_once`` does not crash on a single bad chunk.

        ``propagate=True`` (Phase 2 ``_process_one`` worker tick): let
        exceptions bubble so the worker can record ``retry_count`` and
        flip to ``status='dead'`` past ``max_retries``.
        """
        base = dict(EMPTY_ENVELOPE)
        if isinstance(chunk, dict):
            base["user_id"] = chunk.get("user_id") or ""
            base["source_chunk_id"] = chunk.get("id")
        if self._extract_fn is None:
            return base
        try:
            result = self._extract_fn(chunk, [], self._cfg)
        except Exception as exc:  # noqa: BLE001
            if propagate:
                raise
            logger.warning("extract failed for chunk: %s", exc)
            return base
        if not isinstance(result, dict):
            return base
        # Merge so user_id / source_chunk_id survive even if stub returns
        # only the canonical envelope shape.
        merged = dict(base)
        merged.update({k: v for k, v in result.items() if v})
        return merged

    # ----- Persistence -----

    def _load_state(self) -> None:
        try:
            payload = self._state_load() or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduler state load failed: %s", exc)
            payload = {}
        self._state = ConsolidationState.from_dict(payload)

    def _save_state(self) -> None:
        try:
            self._state_save(self._state.to_dict())
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduler state save failed: %s", exc)

    @staticmethod
    def _file_load(path: str) -> Dict[str, Any]:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _file_save(path: str, payload: Dict[str, Any]) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)


__all__ = [
    "ConsolidationScheduler",
    "DEFAULT_THRESHOLD",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_FLUSH_INTERVAL",
    "DEFAULT_WORKER_POLL_INTERVAL",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_RETRIES",
]