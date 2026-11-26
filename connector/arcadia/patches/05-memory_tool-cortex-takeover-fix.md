# Patch 05 — `MemoryStore.add` cortex-branch predicate inversion (V6.5.2, 2026-09-17)

## What this fixes

`CORTEX_TAKEOVER=1` is set in `.env` and `MemoryStore(use_cortex=True)` is created
in `agent_init._init_memory`. Despite this, every call to `memory_tool(action='add')`
falls into the local `.md` path, and the in-session char-limit gate rejects the
write as soon as the snapshot is full. Cortex is **never** written through the
agentic path — `total_memories` on the cortex server stayed at 2369 for 33 days
(2026-08-04 → 2026-09-17).

## Root cause

In `tools/memory_tool.py::MemoryStore.add`, the cortex branch is gated by:

```python
if self._is_cortex_unavailable():
    try:
        return self._add_to_cortex(target, content)
```

`_is_cortex_unavailable()` returns True **only when cortex is down** — so the
cortex branch fires only when cortex is unreachable. When cortex is healthy
(`use_cortex=True`, `_is_cortex_unavailable()=False`), the code falls through
to `with self._file_lock(self._path_for(target)):` — the local .md file path.

**The predicate was inverted.** With `use_cortex=True`, the agent wanted
"go to cortex"; the code said "go to .md unless cortex is down".

Then the .md path's `if new_total > limit` gate rejects writes once the
in-memory snapshot is full, leaving 18,175 chars of local content and zero
new writes to cortex for a month.

The same `_add_to_cortex` method also runs the same char-limit gate
**unconditionally** (not just on the .md path), so even when the cortex branch
*was* reached, it would still reject writes with a misleading "Memory at
18,175/2,200 chars" error.

## Fix

Apply these two patches to `tools/memory_tool.py`:

### Patch A — `MemoryStore.add` predicate

**Before** (around line 1316):

```python
# === Cortex takeover: bypass .md file entirely ===
if self._is_cortex_unavailable():
    try:
        return self._add_to_cortex(target, content)
```

**After**:

```python
# === Cortex takeover: bypass .md file entirely ===
# V6.5.2 fix: the predicate was inverted. _is_cortex_unavailable() returns
# True ONLY when cortex itself is unreachable, so the old gate
# 'if _is_cortex_unavailable()' only fired the cortex path when cortex
# was DOWN — the exact opposite of what we want. With use_cortex=True
# the right branch is the cortex branch (skip the .md parity gate
# entirely). Fix: enter cortex path when use_cortex is on AND cortex
# is healthy. Otherwise fall through to the .md file path.
if self.use_cortex and not self._is_cortex_unavailable():
    try:
        return self._add_to_cortex(target, content)
```

### Patch B — `_add_to_cortex` char-limit gate

**Before** (around line 706):

```python
# Char limit check (kept for parity with file-based behavior; cortex
# itself doesn't have a hard cap).
entries = self._entries_for(target) + [content]
new_total = len(ENTRY_DELIMITER.join(entries))
limit = self._char_limit(target)
if new_total > limit:
    current = self._char_count(target)
    return self._consolidation_failure({
        "success": False,
        "error": (
            f"Memory at {current:,}/{limit:,} chars. "
            f"Adding this entry ({len(content)} chars) would exceed the limit. "
            f"Consolidate now: use 'replace' to merge overlapping entries into "
            f"shorter ones or 'remove' stale or less important entries (see "
            f"current_entries below), then retry this add — all in this turn."
        ),
        "current_entries": self._entries_for(target),
        "usage": f"{current:,}/{limit:,}",
    })
```

**After**:

```python
# Char limit check (only for native .md path; cortex itself has no hard cap).
# V6.5.2 fix: previously this check ran unconditionally, blocking writes
# to cortex whenever the in-memory snapshot was full even though cortex
# has plenty of room. Now we only enforce the local-file parity limit
# when use_cortex=False — cortex gets a clean pass-through.
if not self.use_cortex:
    entries = self._entries_for(target) + [content]
    new_total = len(ENTRY_DELIMITER.join(entries))
    limit = self._char_limit(target)
    if new_total > limit:
        current = self._char_count(target)
        return self._consolidation_failure({
            "success": False,
            "error": (
                f"Memory at {current:,}/{limit:,} chars. "
                f"Adding this entry ({len(content)} chars) would exceed the limit. "
                f"Consolidate now: use 'replace' to merge overlapping entries into "
                f"shorter ones or 'remove' stale or less important entries (see "
                f"current_entries below), then retry this add — all in this turn."
            ),
            "current_entries": self._entries_for(target),
            "usage": f"{current:,}/{limit:,}",
        })
```

## Why two patches, not one

Patch A redirects the `use_cortex=True` flow into the cortex branch.
Patch B removes the local-file parity check from inside the cortex branch.

Either fix alone is incomplete: without A the cortex branch is never reached
when cortex is up; without B the cortex branch still rejects writes based on
the local-file snapshot size even though cortex itself is unlimited.

## Verification

After applying both patches:

```bash
# Run a probe that goes through the full memory_tool dispatcher
./venv/Scripts/python.exe "scripts/verify_cortex_takeover.py"
```

Expected output before fix:
```
{"success": false, "error": "Memory at 18,175/2,200 chars. Adding this entry..."}
```

Expected output after fix:
```
{"success": true, "done": true, "target": "memory", "entry_count": 28,
 "message": "Entry added.", "note": "Write saved. ..."}
```

And verify the cortex server received the write:

```bash
curl -s http://127.0.0.1:1880/health | python3 -m json.tool
# chunks count should increment by 1 per successful add
```

## Cross-references

- Source: `tools/memory_tool.py::MemoryStore.add` (around line 1316) and
  `tools/memory_tool.py::MemoryStore._add_to_cortex` (around line 706).
- Verify-script: `scripts/verify_cortex_takeover.py` (in this directory).
- Apply-script: `scripts/apply_patches.py` (in this directory).
- Index: `ARCADIA_INTERNAL_PATCHES.md` patch #5.
- Skill index: `memory-cortex-client` skill — "How many memories do I have?"
  section gets refreshed every successful write from now on.