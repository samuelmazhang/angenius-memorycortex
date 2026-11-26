# Agent Integration Patches — hermes ↔ cortex

These are **personal patches** to `hermes-agent`'s `tools/` directory for
**personal use only** (not upstream PR).

## What's in here

| File | Purpose |
|---|---|
| `memory_tool.py` | `tools/memory_tool.py` — V6.3.1 agentic-recall memory tool (8 actions) |
| `cortex_memory_backend.py` | `tools/cortex_memory_backend.py` — cortex HTTP client (V6.3.1 graph/search/conflict methods) |
| `session_summarizer.py` | `tools/session_summarizer.py` — V6.3.1 session-end + progressive auto-summarizer (9b → cortex) |
| `HERMES_INTERNAL_PATCHES.md` | **NEW (V6.3.1)**: diffs for hermes_state_common.py / hermes_state.py / conversation_loop.py (3 SessionDB methods + 3 hooks + session_summaries schema) |
| `scripts/cortex_maintenance.py` | weekly/daily cortex maintenance (incl. `/graph/mine` backfill) |
| `scripts/install_session_summarizer.py` | sync `session_summarizer.py` + `cortex_memory_backend.py` into hermes (default path = UserData/hermes-agent/tools) |
| `tests/test_cortex_backend_blackbox.py` | end-to-end backend method sweep vs remote cortex (28 tests) |
| `tests/test_session_summarizer_blackbox.py` | end-to-end session_summarizer + create_session_summary (6 tests) |

## Current version: V6.3.1 (2026-08-18)

The connector version is **locked to the cortex server version** (see
`connector/VERSION`). The V6.3.x MCP-server entry in CHANGELOG.md is the
server-side refactor; this connector V6.3.1 is the corresponding
hermes-side alignment that ships:

1. **Cortex as sole source of truth for session summaries.** The local
   `session_summaries` table is a fast cache only — write order is
   `cortex.upsert_session_conflict → local.upsert_session_summary →
   local.update_session_summary_cortex_id`. Cortex failure short-circuits
   the local update (no fake-success).
2. **Source tag `hermes-session-summarizer`.** `upsert_session_conflict`
   uses this as the `source` parameter — server's `/stats/by-source`
   already partitions by it (see server OpenAPI).
3. **First formal landing of the hermes-internal patches** that previously
   lived only as "apply by hand against V5.3.7 release commit" guidance.
   See `HERMES_INTERNAL_PATCHES.md` for the full diff list.

The local hermes-agent has its **own `tools/memory_tool.py`** (upstream, git-clean)
and a **locally-added `tools/cortex_memory_backend.py`** (untracked). The two are
independently crafted — **do not overwrite one with the other wholesale** (they
reference methods the other doesn't know). Apply changes per-file, per-function.

### memory_tool.py — V5.3 agentic recall

Upgraded from disk-only `MemoryStore` (actions `add/replace/remove`) to the
8-action agentic version:

| action | behavior |
|---|---|
| `add` / `replace` / `remove` / (batch `operations`) | write path; cortex hooks fire on each chunk write (extract_facts, auto_link, audit) |
| `recall` | agentic retrieval: hybrid search + graph k-hop → compact recall pack |
| `save_procedure` / `get_procedure` | persist/retrieve reusable procedures |
| `deep_context` | full provenance/history/relations for a chunk |
| `audit_query` | read the cortex audit log |

Plus: **active recall injection** into the system-prompt snapshot at session boot
(`_load_from_cortex`), and a **dynamic schema** (`_memory_dynamic_schema`) that
pulls live cortex capability endpoints (30s cache, static fallback).

### cortex_memory_backend.py — V5.3 graph/search methods

Adds (beyond the V4.4 surface):
- `search(query, *, include_related=True)` — 3-way RRF hybrid search default on
- `search_hybrid(top_k)` — fixed param (server uses `limit`, not `top_k`)
- `reason_with_memory(..., khop=2)` — GraphRAG multi-hop subgraph in context
- `graph_khop(memory_id, k, ...)`, `graph_mine(...)` — server `/graph/khop` + `/graph/mine`
- `auto_link_new_chunk(...)` — auto-link on every chunk write (graph keeps growing)
- `create_session_summary(...)` — V5.3.7 session-summary write (see below)

### session_summarizer.py — V5.3.7 session-end + progressive auto-summarizer

A separate hermes-side module that **auto-extracts long-lived knowledge from
conversation history** so the next session can start with relevant context.

**Why it exists.** Before V5.3.7, every new session started from a blank slate
— the agent had no memory of "what we did last time" beyond what's in the
explicit `MEMORY.md` / `USER.md` notes (which the agent has to manually maintain).
With 45,549 raw messages in `state.db` and 22,428 tool outputs (49% of corpus),
the agent has rich conversational history but can't reuse it across sessions.

**What it does.**
- Watches `SessionDB.end_session()` and `SessionDB.append_message()` (the
  hermes-side hook points) for triggers.
- On `end_session`: summarizes the entire conversation via 9b (LM Studio,
  `chat_template_kwargs={"enable_thinking": False}` to avoid max_tokens lock).
- Progressive: every `PROGRESSIVE_THRESHOLD` (default 50) assistant messages,
  re-summarize so a crashed session still has a fresh summary to inject next time.
- Writes the summary to **both**:
  1. `session_summaries` table in `state.db` (local SQLite cache, fast)
  2. cortex via `create_session_summary()` (long_term tier, 180 days, cross-device)
- On the **next session's first turn**, `agent/conversation_loop.py` injects
  the user's most recent N=5 summaries into the system prompt.

**Why 9b (not main model).** 9b has 88% accuracy on structured-output tasks
(per LocalGate V4 evaluation), ~3.1s per session-end summary. Main model
(MiniMax-M3) is too expensive per session. 9b is dedicated to "structured
extraction from text" — fits the task.

**Connector vs hermes-internal.** `session_summarizer.py` lives in this
connector repo (alongside `cortex_memory_backend.py`) because it depends on
`create_session_summary()`. The hermes-side hooks (`end_session`,
`append_message`, `conversation_loop`) live in **hermes-agent's own tree** —
they're not connector patches and shouldn't be versioned here. To re-apply them
on a fresh hermes install, follow the diff against upstream
`hermes_state.py` / `hermes_state_common.py` / `agent/conversation_loop.py`
in the V5.3.7 release commit.

**Disable**: set env var `HERMES_DISABLE_SESSION_SUMMARY=1` (the hermes-side
hooks check this before firing).

## How the pieces sync (this machine)

The repo `connector/hermes/*.py` is the **source of truth**. The code hermes
actually runs lives in the hermes-agent checkout. On this machine the running
hermes is at `A:\Application\Hermes-Agent\Program\hermes-agent\.`

After editing a connector file in the repo, **sync it into the hermes tools dir**:

```bash
REPO=/e/MyDevelop/AnGenius-Agent/memory-cortex
HERMES=/a/Application/Hermes-Agent/Program/hermes-agent/tools

cp "$REPO/connector/hermes/memory_tool.py"          "$HERMES/memory_tool.py"
cp "$REPO/connector/hermes/cortex_memory_backend.py" "$HERMES/cortex_memory_backend.py"

# V5.3.7+ — auto-summarizer sync (uses install script for mtime-aware copy)
python "$REPO/connector/hermes/scripts/install_session_summarizer.py" sync

# Verify all files are in sync
python "$REPO/connector/hermes/scripts/install_session_summarizer.py" check
```

The running hermes picks these up on its next session start (no service restart).

### VM does NOT host the connector

The cortex **server** (`server/src/api_pg.py`) deploys via
`/opt/cortex-deploy/cortex-deploy.sh` on the VM (127.0.0.1). The **connector**
(`memory_tool.py` / `cortex_memory_backend.py`) is hermes-side code that lives
**only on the machine running hermes** (this machine), NOT on the VM. Do not try
to deploy the connector files to the VM.

## Running the end-to-end tests

Requires the hermes venv (has `tools.registry`, `tools.memory_tool`, and their
deps) and a reachable cortex (default `http://127.0.0.1:1880`).

```bash
cd /a/Application/Hermes-Agent/Program/hermes-agent

# (a) memory_tool 8-action e2e (isolated temp memdir, isolated read-safety)
./venv/Scripts/python.exe -m pytest \
    ".connector\hermes\tests\test_memory_tool_e2e.py" \
    -p no:cacheprovider --import-mode=importlib -q

# (b) backend black-box vs remote cortex (broad method-surface coverage)
./venv/Scripts/python.exe -m pytest \
    ".connector\hermes\tests\test_cortex_backend_blackbox.py" \
    -p no:cacheprovider --import-mode=importlib -q
```

(`--import-mode=importlib` avoids the repo root `__init__.py` being imported as a
package; use a Windows path, not an MSYS `/e/...` path.)

- **(a)** `test_memory_tool_e2e.py` — 17 tests: the 8 `memory_tool` actions +
  registry/schema + upstream unreadable-file safety invariants.
- **(b)** `test_cortex_backend_blackbox.py` — 28 tests: calls the local
  `tools.cortex_memory_backend` methods against the **remote** cortex server and
  asserts return shapes. Catches connector↔server contract drift (wrong response
  keys, missing methods, param name mismatches) — e.g. this suite caught
  `create_hierarchy_node` reading `id` while the server returns `node_id`, and a
  missing `recommend_endpoints`.

Both use a monkeypatched/isolated store so they never write to real hermes
memory.

> **Pitfall**: `get_memory_dir()` resolves via `get_hermes_home()/memories` — it
> does NOT read a `HERMES_MEMORY_DIR` env var. To isolate, monkeypatch
> `tools.memory_tool.get_memory_dir` (as the e2e test does), else you'd write to
> real hermes memory.

## How to revert

```bash
cd /a/Application/Hermes-Agent/Program/hermes-agent
git checkout origin/main -- tools/memory_tool.py          # restores upstream memory_tool
# cortex_memory_backend.py is untracked (locally added) — remove it to drop cortex routing
rm tools/cortex_memory_backend.py
```

## Why not upstream PR?

- These are **personal config patches** for a single user's deployment
- Upstream hermes-agent has its own memory architecture (CoALA + Zep-style)
- We don't want to introduce a competing integration upstream
- Per upstream AGENTS.md: third-party products integrated into the core tree are
  not merged upstream

## Validation history

- **V4.4 (2026-07-27)**: 12/13 features PASS (learn_procedure LLM timeout expected)
- **V5.3 (2026-08-02)**: e2e 8-action sweep green; hermes pytest
  `tests/tools/test_memory_tool.py` 37/37 (after porting upstream read-safety
  invariants), +schema +import_fallback 43/43.
- **V5.3.7 (2026-08-10)**: session_summarizer end-to-end 6/6 PASS
  (create_session_summary basic + tier=long_term/short_term + format helpers +
  JSON parse robust + real 9b→cortex round-trip + async fire-and-forget).

## Version / changelog

All connector changes are versioned in the repo root `CHANGELOG.md` with the
cortex server. The relevant versions are **V5.3.3** (connector surface),
**V5.3.5** (memory_tool upgrade + 4 latent-bug fixes), **V5.3.6** (read-safety
invariants), **V6.5.2** (hermes-side patch #4 — agent_init `_init_memory`
builtin-memory init).

## Deployment patches

Hermes-side patches live in `patches/` and are applied via
`scripts/apply_patches.py`. Re-run that script on any fresh hermes install.

| # | Patch | Target | What |
|---|-------|--------|------|
| 1-3 | `HERMES_INTERNAL_PATCHES.md` (V6.3.1) | `hermes_state_common.py`, `hermes_state.py`, `agent/conversation_loop.py` | session_summaries table + 3 SessionDB methods + end_session / append_message / first-turn prior-summary hooks |
| 4 | `patches/04-agent_init-mem-config-fix.md` (V6.5.2) | `agent/agent_init.py::_init_memory` | pre-bind `mem_config` + replace missing `get_builtin_memory_config`/`get_builtin_memory_store_flags` with `load_config()` reads — without this, **every session prints `Memory provider 'memory-cortex' is selected but reports unavailable` and runs with `_memory_manager = None`** |
| 5 | `patches/05-memory_tool-cortex-takeover-fix.md` (V6.5.2) | `tools/memory_tool.py::MemoryStore.add` + `MemoryStore._add_to_cortex` | **fix inverted cortex-branch predicate** (`if _is_cortex_unavailable()` → `if use_cortex and not _is_cortex_unavailable()`) + **move char-limit parity gate into the .md path only** — without this, every `memory_tool(action='add')` falls into the .md file path even when `use_cortex=True`, and the local-file char limit blocks writes to cortex. Cortex server's `total_memories` was frozen at 2369 for 33 days (2026-08-04 → 2026-09-17) because of this bug.

Apply all patches:

```bash
# Run from hermes venv
./venv/Scripts/python.exe ".connector/hermes/scripts/apply_patches.py"
```

Verify a fresh install after restart:

```bash
# Verify patches 1-3 (V6.3.1 hermes_state_* / conversation_loop) — these have
# to be applied by hand; see HERMES_INTERNAL_PATCHES.md sections 1-6.

# Verify patch 4 (V6.5.2 agent_init _init_memory)
./venv/Scripts/python.exe ".connector/hermes/scripts/verify_init_memory.py"
# expected: "PASS: Patch 04 is in place — memory-cortex provider activated."

# Verify patch 5 (V6.5.2 memory_tool cortex takeover)
./venv/Scripts/python.exe ".connector/hermes/scripts/verify_cortex_takeover.py"
# expected: "PASS: Patch 05 in place — memory_tool writes reach cortex end-to-end."
#           and "delta: +1 chunk per add"
```

Both `apply_patches.py` and the verify scripts are idempotent — re-running
on an already-patched hermes prints `OK ... already applied` / `PASS` and
exits 0.
