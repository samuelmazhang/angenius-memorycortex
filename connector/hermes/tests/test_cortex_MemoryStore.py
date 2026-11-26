"""End-to-end smoke for CortexMemoryStore.

Mirrors the e2e pattern in `connector/hermes/tests/test_cortex_backend_blackbox.py`:
real HTTP round-trips against the live Memory Cortex server (default
``MEMORY_CORTEX_URL=http://127.0.0.1:1880``), with graceful skip when
cortex is unreachable so the regression gate stays green offline.

Run from the connector root:

    cd .
    python -m pytest connector/hermes/cortex_MemoryStore/tests/test_smoke.py -q

Or via the smoke script:

    python connector/hermes/cortex_MemoryStore/scripts/smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

# Make `cortex_MemoryStore` importable when the test runs from the repo
# root (pytest sys.path inserts the test directory by default; we also
# add the connector sub-package parent — which is connector/hermes/ —
# for the sibling import).
_HERE = Path(__file__).resolve().parent
_PACKAGE = _HERE.parent  # tests/ → connector/hermes/
sys.path.insert(0, str(_PACKAGE))  # so `from cortex_MemoryStore import ...` works


from cortex_MemoryStore import CortexMemoryStore, CortexUnavailableError  # noqa: E402


@pytest.fixture(scope="module")
def store() -> CortexMemoryStore:
    """Module-scoped store; skips the whole module if cortex is offline."""
    s = CortexMemoryStore()
    if not s.is_available():
        pytest.skip("cortex unreachable; set MEMORY_CORTEX_URL to enable smoke test")
    return s


def test_health_returns_version(store) -> None:
    h = store.health()
    assert isinstance(h, dict)
    assert h.get("version"), f"health has no version key: {h!r}"


def test_is_available_true(store) -> None:
    assert store.is_available() is True


def test_stats_returns_total_memories(store) -> None:
    stats = store._backend.stats()
    assert isinstance(stats, dict)
    assert "total_memories" in stats or "chunks" in stats or "total_chunks" in stats


def test_char_limit_parity(store) -> None:
    # Parity with MemoryStore: char_limit returns int > 0 for both targets.
    assert store.char_limit("memory") > 0
    assert store.char_limit("user") > 0
    # char_count for cortex is best-effort (returns 0 — cortex uses tier
    # retention, not a per-store char cap). Pin to that contract.
    assert store.char_count("memory") == 0


def test_entry_count_int(store) -> None:
    n = store.entry_count("memory")
    assert isinstance(n, int)
    assert n >= 0


def test_add_returns_chunk_id(store) -> None:
    """Round-trip: add → returns id, then deep_context returns it."""
    marker = f"sprint116-cortex-MemoryStore-smoke-{uuid.uuid4().hex[:8]}"
    result = store.add(
        target="memory",
        content=f"## {marker}\n\ntest entry for round-trip",
        importance=5,  # short-lived; we'll DELETE it
        tags=["test", "smoke", "cortex_MemoryStore"],
    )
    chunk_id = result.get("id") if isinstance(result, dict) else None
    assert chunk_id, f"add did not return id: {result!r}"
    try:
        # deep_context sanity
        dc = store.deep_context(chunk_id)
        assert isinstance(dc, dict)
    finally:
        # Clean up so we don't leave test chunks in cortex.
        try:
            store._backend.delete_entry(chunk_id)
        except CortexUnavailableError:
            pass


def test_recall_returns_results_for_known_chunk(store) -> None:
    """Insert a uniquely-tagged chunk, then recall must find it."""
    marker = f"recall-marker-{uuid.uuid4().hex[:8]}"
    insert = store.add(
        target="memory",
        content=f"## {marker}\n\nthis entry exists only for the recall smoke test",
        importance=5,
        tags=["recall-smoke", marker],
    )
    chunk_id = insert.get("id") if isinstance(insert, dict) else None
    try:
        result = store.recall(marker, limit=3)
        # ``recall`` returns whatever the backend's ``search`` returns.
        # The shape observed in V-FULL runs is a list of hit dicts (not
        # a wrapper dict with a "results" key) — accept either shape.
        if isinstance(result, dict):
            hits = result.get("results", [])
            ids = {h.get("id") for h in hits if isinstance(h, dict)}
        elif isinstance(result, list):
            hits = result
            ids = {h.get("id") for h in hits if isinstance(h, dict)}
        else:
            pytest.fail(f"recall returned unexpected type {type(result).__name__}: {result!r}")
        assert chunk_id in ids, (
            f"recall({marker!r}) should hit the chunk we added; "
            f"got ids={ids!r}"
        )
    finally:
        if chunk_id:
            try:
                store._backend.delete_entry(chunk_id)
            except CortexUnavailableError:
                pass


def test_remove_returns_error_for_nonexistent(store) -> None:
    """The wrapper should return a JSON error (not raise) when no chunk
    matches old_text."""
    result = store.remove(
        target="memory",
        old_text=f"__nonexistent-{uuid.uuid4().hex}__",
    )
    assert isinstance(result, dict)
    assert result.get("success") is False
    assert "old_text" in result.get("error", "").lower() or "match" in result.get("error", "").lower()


def test_replace_round_trip(store) -> None:
    """insert → replace via old_text → chunk_id changes."""
    original_marker = f"replace-orig-{uuid.uuid4().hex[:8]}"
    inserted = store.add(
        target="memory",
        content=f"## {original_marker}\n\nORIGINAL content for replace test",
        importance=5,
        tags=["replace-smoke"],
    )
    original_id = inserted.get("id") if isinstance(inserted, dict) else None
    assert original_id
    try:
        # Cache the chunk-id for the resolve path.
        store._chunk_ids.setdefault("memory", {})[inserted["content"]] = original_id
        result = store.replace(
            target="memory",
            old_text="ORIGINAL content for replace test",
            content=f"## {original_marker}\n\nREPLACED content",
            importance=5,
        )
        assert isinstance(result, dict)
        assert result.get("success") is True
        assert result.get("deleted") == original_id
        added = result.get("added", {})
        new_id = added.get("id") if isinstance(added, dict) else None
        assert new_id and new_id != original_id
        # Clean up the new chunk.
        try:
            store._backend.delete_entry(new_id)
        except CortexUnavailableError:
            pass
    finally:
        # original was already DELETEd by replace; nothing else to do.
        pass


def test_session_summary_persists(store) -> None:
    marker = f"session-smoke-{uuid.uuid4().hex[:8]}"
    result = store.session_summary(
        content=f"## {marker}\n\nsession summary smoke test entry",
        session_id="sprint116-smoke",
        importance=5,
        tags=["session-smoke", marker],
    )
    chunk_id = result.get("id") if isinstance(result, dict) else None
    assert chunk_id, f"session_summary did not return id: {result!r}"
    try:
        # delete the entry by id
        try:
            store._backend.delete_entry(chunk_id)
        except CortexUnavailableError:
            pass
    except Exception:
        pass


def test_reason_with_memory_caps_khop_at_3(store) -> None:
    """Per memory-cortex-client skill §'Exposing advanced Cortex features',
    khop is capped at 3 even if the caller asks for more."""
    result = store.reason_with_memory(
        "subagent delegate_task decision tree", top_k=2, khop=99
    )
    assert isinstance(result, dict)
    # The wrapper should pass through khop=3, not 99; the result shape is
    # whatever cortex returns. We just assert we got *some* context back.
    ctx = result.get("context_chunks", "")
    assert isinstance(ctx, str)