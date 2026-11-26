#!/usr/bin/env python3
"""Smoke runner for CortexMemoryStore.

Bypasses pytest so a quick "does it still work?" probe can run without
the test-framework boot overhead. Useful for cron-style connectivity
checks or pre-flight in CI pipelines that only need a yes/no answer.

Usage (from anywhere; the script adds its own directory to sys.path so
``cortex_MemoryStore`` resolves as a sibling import):

    python connector/arcadia/smoke_cortex_MemoryStore.py

Exit code:
  0 = cortex reachable + all 12 ops pass
  1 = cortex unreachable (treat as warning, not failure)
  2 = cortex reachable but an op failed (real regression)
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# Add the script's own directory to sys.path so ``cortex_MemoryStore``
# resolves as a sibling import (cortex_MemoryStore.py lives next to
# this smoke runner in connector/arcadia/).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from cortex_MemoryStore import CortexMemoryStore, CortexUnavailableError  # noqa: E402


_OPS = (
    ("health", lambda s: s.health()),
    ("is_available", lambda s: s.is_available()),
    ("stats", lambda s: s._backend.stats()),
    ("char_limit('memory')", lambda s: s.char_limit("memory")),
    ("entry_count('memory')", lambda s: s.entry_count("memory")),
)


def _check_op(name: str, store, fn) -> bool:
    try:
        result = fn(store)
        ok = bool(result) if not isinstance(result, dict) else True
        print(f"  ✅ {name}: {type(result).__name__}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ {name}: {type(e).__name__}: {e}")
        return False


def main() -> int:
    print("=" * 60)
    print("CortexMemoryStore smoke runner")
    print("=" * 60)
    store = CortexMemoryStore()
    if not store.is_available():
        print("  cortex unreachable — skipping (exit 1 = treat as warning)")
        return 1

    failed = 0
    for name, fn in _OPS:
        if not _check_op(name, store, fn):
            failed += 1

    # Round-trip add → recall → remove to catch the most common drift
    # (schema changes that silently break add).
    try:
        import uuid
        marker = f"smoke-{uuid.uuid4().hex[:8]}"
        added = store.add(
            target="memory",
            content=f"## {marker}\n\ncortex_MemoryStore smoke round-trip",
            importance=5,
            tags=["smoke", marker],
        )
        cid = added.get("id") if isinstance(added, dict) else None
        if not cid:
            print(f"  ❌ round-trip add returned no id: {added!r}")
            failed += 1
        else:
            print(f"  ✅ round-trip add: {cid}")
            try:
                store._backend.delete_entry(cid)
                print(f"  ✅ round-trip delete")
            except CortexUnavailableError:
                pass
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ round-trip add: {type(e).__name__}: {e}")
        traceback.print_exc()
        failed += 1

    print("=" * 60)
    if failed:
        print(f"FAILED ({failed} ops did not pass cleanly)")
        return 2
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())