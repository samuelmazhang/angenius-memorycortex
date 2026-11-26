---
name: cortex-memory
description: "Long-term semantic memory for WorkBuddy, backed by the Memory Cortex service that is shared with the Hermes agent. Use when the user asks to remember or forget facts, recall prior context, or whenever prior knowledge could improve the answer. Trigger keywords: 记住, 记一下, 别忘了, 之前, 上次, 你知道, 记忆, remember, recall, what do you know, previously."
version: 1.0.0
---

# Cortex Memory — WorkBuddy Skill

Long-term semantic memory for WorkBuddy, backed by the shared **Memory Cortex**
service (the same instance Hermes agent uses). Memory persists across sessions
and is shared with Hermes by default.

## MCP server

Server name in `mcp.json`: `memory-cortex` (Streamable HTTP at
`http://127.0.0.1:1880/mcp/`, requires `Accept: application/json, text/event-stream`).

Friendly tools (6 of 104 exposed; prefer these):

| Tool | Purpose | Required args |
|---|---|---|
| `cortex_remember` | Store a memory chunk (CoALA auto ADD/UPDATE/MERGE) | `content` |
| `cortex_recall` | Hybrid semantic search (vector + FTS5) | `query` |
| `cortex_get` | Fetch one chunk with full metadata/history | `memory_id` |
| `cortex_forget` | Permanently delete a chunk | `chunk_id` |
| `cortex_conflict_upsert` | Insert or supersede by `source_key` (idempotent updates) | `content`, `source_key` |
| `cortex_introspect` | Health/version/totals | — |

Optional args on `cortex_remember`/`cortex_recall`: `type`, `tags`,
`importance` (1-10), `user_id`. `cortex_recall` also accepts `limit` and
`min_importance`.

## Behavior contract

### Proactive recall (do this without being asked)

- At the start of a task that could benefit from prior context, call
  `cortex_recall` with the task's key terms (`limit=5`, `min_importance=4`).
- For broad questions ("你知道什么" / "what do you know" / "之前" / "上次"),
  call `cortex_recall` with `limit=10`, `min_importance=0`.
- If recall returns nothing useful, proceed normally — never block on memory.

### Explicit save

- If the user says "记住:" / "remember:" / "记一下:" / "别忘了:", save the text
  with `cortex_remember(content=..., type="knowledge", importance=6,
  tags=["workbuddy","explicit-save"])`.
- After substantive work (a decision is made, a bug is fixed, a convention is
  established), proactively save a compact summary with
  `cortex_remember(content=..., type="decision|experience|knowledge",
  importance=5, tags=["workbuddy"])`.

### Idempotent updates (avoid duplicates)

- For facts that get revised (status, config values, current decisions), use
  `cortex_conflict_upsert` with a stable `source_key` (e.g.
  `workbuddy:project:<name>:status`) instead of repeated `cortex_remember`.

### user_id namespace

- **Default: omit `user_id`** — read/write the shared global memory that Hermes
  also uses. This is the intended behavior for "接上 Hermes 的 memorycortex".
- Only pass a fixed `user_id` (e.g. `workbuddy`) when the user explicitly asks
  to isolate WorkBuddy's memory from Hermes's.

## Reliability

- All tools are best-effort. If the Cortex server is unreachable or times out,
  continue the task without memory — never surface a raw MCP error to the user
  unless they specifically asked about memory.
- Do not fabricate recall results. Only present chunks actually returned by
  `cortex_recall`/`cortex_get`, quoting `content` and (if useful) `id`.

## Health check

Call `cortex_introspect()` to confirm the service is alive
(`health.status == "healthy"`). Server version should be >= 6.6.0.

## See also

- `connector/hermes/README.md` — the Hermes integration this mirrors.
- `connector/opencode/SKILL.md` — sibling skill contract.
- `server/src/mcp_server/cortex_mcp.py` — the MCP tool implementation.
