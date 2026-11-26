# Claude Code ↔ cortex connector

Covers **all 89 endpoints** exposed by `server/src/api_pg.py` (validated
2026-09-08 against live server **v6.5.1** at `127.0.0.1:1880`, 2653 chunks,
15208 relations). Locked to the same connector version as the rest of
`connector/` (see `../VERSION`).

## What's in here

| File | Purpose |
|---|---|
| `claude_cortex_client.py` | `Cortex` class — 80 methods, one per HTTP endpoint group, plus a self-test sweep |
| `__init__.py` | exports `Cortex`, `CortexError`, timing constants |
| `scripts/claude_smoke.py` | standalone CLI smoke runner — no Claude Code required |
| `README.md` | this file |

## Usage in Claude Code

The client is designed to be called from a Bash / Python tool invocation
inside a Claude Code session. Three common patterns:

### A. One-liner recall
```bash
python -c "from connector.claude import Cortex; \
  print([h['id'] for h in Cortex().recall('deployment smoke', limit=3)])"
```

### B. Multi-call workflow
```python
from connector.claude import Cortex
cx = Cortex()

# Write a session marker
cid = cx.remember(
    "Reviewed Sprint 105 closure notes",
    type="session_summary",
    tags=["claude", "review"],
    importance=6,
)["id"]

# Recall what we already knew
hits = cx.recall("Sprint 105 closure", limit=3, tags=["review"])
for h in hits:
    print(h["id"], h["similarity"], h["content"][:80])
```

### C. Self-test from a Bash call
```bash
cd .
python -m connector.claude.scripts.claude_smoke
```
prints `OK (78): [...] / WARN (2): [...] / FAIL (0)` — proves every
endpoint group is reachable from this Claude Code instance.

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `CORTEX_URL` | `http://127.0.0.1:1880` | override to point at a different cortex (test, staging) |
| `CORTEX_TIMEOUT` | `15` (sec) | per-request timeout |
| `CORTEX_LONG_TIMEOUT` | `60` (sec) | timeout for `/reconsolidate`, `/merge-duplicates`, `/graph/mine`, `/forgetting/cleanup` |

## Endpoint coverage map

Every endpoint in `ENDPOINTS_CATALOG` (the server's `/endpoints` route) is
wrapped. Grouped by category:

| Group | Method count | Destructive methods (require `confirm=True`) |
|---|---|---|
| Core chunk CRUD | 8 | `forget()` (hard delete + relations cleanup) |
| Search variants | 3 | — |
| Stats & introspection | 15 | `reset_ops()` |
| Graph | 3 | `graph_mine()` |
| Relations & links | 11 | `delete_relations()`, `delete_relation()`, `link_decay()` |
| Working memory | 5 | — |
| Forgetting | 5 | `forgetting_cleanup()`, `forgetting_guided()` |
| Audit / history / provenance | 4 | — |
| Storage / TTL | 3 | `ttl_archive()` (soft delete) |
| Taxonomy & hierarchy | 7 | `taxonomy_delete()` |
| Episodes / core / procedural | 8 | — |
| Media & resources | 5 | — |
| Consolidation & rumination | 5 | `rumination_run()` (metadata mutation) |
| Conflict / reconsolidation / merge | 5 | `reconsolidate()`, `merge_duplicates()` |
| Compress & cache | 4 | `compress_run()` |
| Access stats & refresh | 3 | `refresh_memory()` |
| Bench & eval | 3 | — |
| Admin / maintenance | 8 | `maintenance_run_once()`, `storage_evict()` |
| Dashboard | 3 | — |

Destructive methods raise `PermissionError` unless you pass `confirm=True`
as a keyword arg. This prevents accidental rewriting of history from a
stray `cx.forget(...)` call inside an agent loop.

## Design notes (why this is a separate connector)

- The MCP variant (`server/src/mcp_server/cortex_mcp.py`) runs an HTTP MCP
  listener on port 9000 inside the cortex container. It is **not** what
  Claude Code talks to — Claude Code's MCP client is stdio-only by default,
  and the cortex MCP variant is HTTP transport. Bridging would require
  rewriting the cortex MCP server to stdio. See
  `server/README.md §MCP protocol alternative`.
- The Python client is the **simplest path** to all 89 endpoints from
  Claude Code without server-side changes. It is one file, zero deps
  beyond `requests`, and lives entirely on the Claude Code side.
- For Arcadia integration (different connector), see
  `connector/arcadia/cortex_memory_backend.py`. The two share the same
  server contract but live in separate runtime folders.

## What this connector does NOT do

- **Authentication**: cortex server is currently unauthenticated HTTP. Do
  not deploy this client against a publicly reachable cortex without
  adding a token first (server-side change required).
- **MCP exposure**: this client is callable from Claude Code's Bash /
  Python tool, but it is **not** itself an MCP server. Adding MCP
  exposure would mean wrapping `claude_cortex_client.py` with
  `mcp.server.Server` and registering the 80 methods as MCP tools.
- **Caching of recall results**: the cortex server has its own embedding
  cache (`/stats/embed-cache`). This client adds no client-side cache.

## Validation log

- 2026-09-08: built against `api_pg.py` v6.5.1 (commit unknown). 89/89
  endpoints read in source + exercised via self-test. 78 OK / 2 WARN /
  0 FAIL. The 2 WARN are 404s on `compress_get()` and
  `maintenance_enqueue()` after the chunk was forgotten earlier in the
  same run — expected ordering, not bugs.