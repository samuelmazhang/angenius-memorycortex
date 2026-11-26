# Arcadia ↔ Cortex Recovery Runbook (V6.5.2, 2026-09-17)

> **One-stop recovery**: every patch + script + verification a fresh
> `A:\Application\Arcadia-Agent\Program\arcadia-agent\` checkout needs to
> fully wire up `CORTEX_TAKEOVER=1` writes.
>
> Estimated time: 5 min if `patches/0*.md` and `scripts/*.py` are already
> in this repo; 30-60 min if you have to fetch the connector first.

## TL;DR

```bash
# 1. Fetch the connector (one-time, ~5 min):
#    git clone https://github.com/.../memory-cortex  (or copy from E:)
#    cd memory-cortex/connector/arcadia

# 2. Sync connector source files into arcadia (3 files, ~30 s):
cp memory_tool.py          "$ARCADIA/tools/memory_tool.py"
cp cortex_memory_backend.py "$ARCADIA/tools/cortex_memory_backend.py"
python scripts/install_session_summarizer.py sync    # session_summarizer

# 3. Apply the V6.5.2 arcadia-side patches (the two bugs that broke
#    cortex writes for 33 days):
./venv/Scripts/python.exe scripts/apply_patches.py --dry-run
./venv/Scripts/python.exe scripts/apply_patches.py

# 4. Verify:
./venv/Scripts/python.exe scripts/verify_init_memory.py
./venv/Scripts/python.exe scripts/verify_cortex_takeover.py

# 5. Restart arcadia so the patched code is loaded into the running process.
#    (Patches touch .py modules; the running arcadia.exe has the old
#    code in memory until it exits and re-imports.)
```

After step 5, every `memory_tool(action='add')` from a arcadia LLM turn
hits the cortex server end-to-end. See `verify_cortex_takeover.py`
output for the post-condition check.

## What's broken without these patches

Two independent bugs together caused `cortex.total_memories` to freeze
at 2369 from 2026-08-04 to 2026-09-17 (33 days):

| Patch | Symptom |
|-------|---------|
| **#4 — `agent_init._init_memory`** | Every session prints `Memory provider 'memory-cortex' is selected but reports unavailable` and runs with `_memory_manager = None`. `arcadia memory status` is misleadingly green because that command path doesn't go through the broken init. Cortex never gets `sync_turn` writes. |
| **#5 — `MemoryStore.add` predicate + `_add_to_cortex` gate** | Even after fix #4 loads the provider, every `memory_tool(action='add')` falls into the local `.md` file path because `if self._is_cortex_unavailable():` is the inverted predicate (cortex-unreachable gate, instead of cortex-have-gate). The local 2,200-char limit then rejects the write with "Memory at 18,175/2,200 chars". |

**Both fixes are required.** Either alone leaves the system broken.

The previous (V6.3.1) `ARCADIA_INTERNAL_PATCHES.md` covered session_summaries +
hooks — those are still needed for the auto-summarizer path, but are
orthogonal to the cortex-takeover path.

## File layout

```
connector/arcadia/
├── README.md                              ← runbook index (this file)
├── VERSION                                ← 6.5.2
├── ARCADIA_INTERNAL_PATCHES.md             ← patches 1-6 (V6.3.1, hand-applied)
├── memory_tool.py                         ← synced into $ARCADIA/tools/
├── cortex_memory_backend.py               ← synced into $ARCADIA/tools/
├── session_summarizer.py                  ← synced via install_session_summarizer.py
├── cortex_MemoryStore.py                  ← (alternative local-only store; not used
│                                            in CORTEX_TAKEOVER=1 mode)
├── smoke_cortex_MemoryStore.py
├── tests/                                 ← pytest e2e suites (45 tests)
├── patches/                               ← reproducible patches (apply_patches.py)
│   ├── 04-agent_init-mem-config-fix.md    ← patch #4
│   └── 05-memory_tool-cortex-takeover-fix.md  ← patch #5
├── scripts/
│   ├── apply_patches.py                   ← parses patches/ + applies to arcadia
│   ├── verify_init_memory.py              ← regression for patch #4
│   ├── verify_cortex_takeover.py          ← regression for patch #5
│   ├── install_session_summarizer.py      ← session_summarizer sync
│   ├── cortex_maintenance.py              ← weekly/daily maintenance
│   ├── fix_migration_source.py            ← chunk source reclassification
│   └── migrate_md_to_cortex.py            ← one-shot .md → cortex
```

## Step-by-step recovery

### Pre-flight: confirm cortex is healthy

```bash
curl -s http://127.0.0.1:1880/health | python -m json.tool
# expected:
# {
#   "status": "healthy",
#   "version": "6.5.1",
#   "chunks": <N>,
#   ...
# }
```

If cortex is down, fix the server first (out of scope of this runbook;
see `cortex-fix-deploy` skill or the server deployment scripts).

### Step 1 — Sync connector source into arcadia

The connector **owns** `tools/memory_tool.py`, `tools/cortex_memory_backend.py`,
and `tools/session_summarizer.py`. These three files must be byte-identical
between `connector/arcadia/` and `$ARCADIA/tools/`.

```bash
ARCADIA=/a/Application/Arcadia-Agent/Program/arcadia-agent
REPO=.connector/arcadia

# 3 always-synced files (mtime-aware copy where it matters):
cp "$REPO/memory_tool.py"          "$ARCADIA/tools/memory_tool.py"
cp "$REPO/cortex_memory_backend.py" "$ARCADIA/tools/cortex_memory_backend.py"
"$ARCADIA/venv/Scripts/python.exe" "$REPO/scripts/install_session_summarizer.py" sync
```

If any of these three differ from the connector (sha256 mismatch),
the recovery is incomplete — re-sync before continuing.

### Step 2 — Apply the V6.5.2 arcadia-side patches

`ARCADIA_INTERNAL_PATCHES.md` patches 1-6 (V6.3.1) cover session_summaries +
hooks. **Those are hand-applied and already in place on the live machine.**
Patches 7 and 8 (V6.5.2, this runbook) are reproducible from
`patches/04-*.md` and `patches/05-*.md`.

```bash
cd "$ARCADIA"

# Dry-run first — both should report "already applied" if the live machine
# is up to date. If any report "FAIL: does not contain the 'Before' block",
# the file has drifted and the patch must be re-derived by hand.
./venv/Scripts/python.exe "$REPO/scripts/apply_patches.py" --dry-run

# Apply for real:
./venv/Scripts/python.exe "$REPO/scripts/apply_patches.py"
```

Expected output (live, post-fix):
```
Found 2 patch(es):
  - 04-agent_init-mem-config-fix.md
  - 05-memory_tool-cortex-takeover-fix.md

OK    04-agent_init-mem-config-fix.md: already applied (agent/agent_init.py)
OK    05-memory_tool-cortex-takeover-fix.md (block 1/2): already applied (tools/memory_tool.py)
OK    05-memory_tool-cortex-takeover-fix.md (block 2/2): already applied (tools/memory_tool.py)

Summary: 3 ok, 0 need attention, 0 skipped.
```

`apply_patches.py` is **idempotent and normalize-aware** (handles indentation
and whitespace differences between the markdown `Before` blocks and the
on-disk source). It refuses to apply if the `Before` block is not detected,
so a "FAIL: does not contain the 'Before' block" output means the file has
diverged — read the patch markdown, re-derive the diff manually, update the
markdown, and re-run.

### Step 3 — Verify

```bash
# Verify patch 4 (provider init). Expected output:
./venv/Scripts/python.exe "$REPO/scripts/verify_init_memory.py"
#
#   _memory_manager: <agent.memory_manager.MemoryManager object at ...>
#   providers: ['CortexMemoryProvider']
#   _memory_enabled: True
#   _user_profile_enabled: True
# PASS: Patch 04 is in place — memory-cortex provider activated.

# Verify patch 5 (cortex takeover end-to-end). Expected output:
./venv/Scripts/python.exe "$REPO/scripts/verify_cortex_takeover.py"
#
#   cortex chunks before: <N>
#   memory_tool result: {'success': True, 'done': True, ..., 'entry_count': 28, ...}
#   cortex chunks after:  <N+1>
#   delta: +1
# PASS: Patch 05 in place — memory_tool writes reach cortex end-to-end.
```

If either verifier prints `FAIL`, the patches are not in place — re-run
step 2 and inspect the error.

### Step 4 — Restart arcadia

`apply_patches.py` edits `.py` files on disk. The currently-running
`arcadia.exe` process still has the old code in memory (Python imports
are cached per-process). A new session, `/reset`, or process restart
will pick up the patched code.

If you can't restart immediately, the verifier scripts prove the patches
are correct on disk — every fresh arcadia process from this point forward
will run the patched code.

### Step 5 — Confirm cortex is receiving writes

After a few turns of normal arcadia use, watch `cortex.total_memories`
increment:

```bash
curl -s http://127.0.0.1:1880/health | python -m json.tool | grep chunks
# chunks: 2377, 2378, 2379, ...
```

If the number is frozen, check `arcadia logs/errors.log` for
`Memory provider plugin init failed` (patch #4 regressed) or
`Memory at ... chars` (patch #5 regressed).

## What if I need to roll back?

```bash
cd "$ARCADIA"

# Roll back the V6.5.2 patches by hand (apply_patches.py has no "revert"
# mode — by design, because the patches are non-trivial and a wrong revert
# is worse than a wrong apply):
#   - Patch #4: replace the modified `_init_memory` block in
#     agent/agent_init.py with the contents of `### Before` from
#     patches/04-agent_init-mem-config-fix.md.
#   - Patch #5: replace the modified `MemoryStore.add` and
#     `MemoryStore._add_to_cortex` methods with the contents of
#     `### Before` from patches/05-memory_tool-cortex-takeover-fix.md.

# Roll back the synced connector source:
git -C "$REPO" checkout -- memory_tool.py cortex_memory_backend.py
"$ARCADIA/venv/Scripts/python.exe" "$REPO/scripts/install_session_summarizer.py" sync

# The result: arcadia writes again go to .md files only (CORTEX_TAKEOVER=1
# is still set, but every write will be gated).
```

## Files in this runbook

- `patches/04-agent_init-mem-config-fix.md` — V6.5.2 patch #4 (single block).
- `patches/05-memory_tool-cortex-takeover-fix.md` — V6.5.2 patch #5 (two blocks).
- `scripts/apply_patches.py` — parser + normalize-aware applier.
- `scripts/verify_init_memory.py` — regression for patch #4.
- `scripts/verify_cortex_takeover.py` — regression for patch #5.
- `ARCADIA_INTERNAL_PATCHES.md` — patches 1-6 (V6.3.1) reference; hand-applied.
- `README.md` — connector overview (this runbook is the deployment appendix).

## Cross-references

- Skill: `memory-cortex-client` — when to use the connector / client side.
- Skill: `cortex-fix-deploy` — server-side patches (different scope).
- Connector `VERSION` — `6.5.2`.