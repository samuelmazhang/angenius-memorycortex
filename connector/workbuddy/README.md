# WorkBuddy Connector for Cortex

Long-term semantic memory integration for **WorkBuddy**, backed by the same
Memory Cortex service the Hermes agent uses.

## Status

V1.0.0. WorkBuddy consumes Cortex through its native **MCP server** (Streamable
HTTP), so no Python client is vendored — the MCP tool surface is the contract.

## What this connector provides

| File | Purpose |
|---|---|
| `SKILL.md`            | WorkBuddy skill manifest + memory behavior contract |
| `README.md`           | This file |
| `VERSION`             | Connector version |
| `scripts/smoke.py`    | Standalone MCP handshake + read/write smoke test |

## Transport

| Item | Value |
|---|---|
| MCP server | `memory-cortex` (registered in `~/.workbuddy/mcp.json`) |
| URL | `http://127.0.0.1:1880/mcp/` |
| Protocol | Streamable HTTP / JSON-RPC 2.0, protocolVersion `2024-11-05` |
| Required header | `Accept: application/json, text/event-stream` (else HTTP 406) |
| Auth | None (server is unauthenticated; keep it on a trusted LAN) |

## Install (already applied on this machine)

1. `mcp.json` entry added:
   ```json
   "memory-cortex": {
     "type": "http",
     "url": "http://127.0.0.1:1880/mcp/",
     "headers": { "Accept": "application/json, text/event-stream" },
     "description": "..."
   }
   ```
2. Skill symlink: `~/.workbuddy/skills/cortex-memory` → this directory.

Both take effect after WorkBuddy restart (new session).

## Tools used

6 friendly aliases out of 104: `cortex_remember`, `cortex_recall`,
`cortex_get`, `cortex_forget`, `cortex_conflict_upsert`, `cortex_introspect`.

`user_id` is omitted by default so WorkBuddy shares the global memory with
Hermes. See `SKILL.md` for the full contract.

## Verify

```bash
python connector/workbuddy/scripts/smoke.py
```

## Compatibility

| Connector | Cortex server |
|---|---|
| 1.0.0 | 6.6.0+ |

## Design notes

- Mirrors `connector/opencode/` and `connector/claude/` structure but uses
  Cortex's built-in MCP surface instead of a bundled HTTP client, because
  WorkBuddy has a first-class MCP client and the built-in
  `MemoryRecall`/`MemoryUpdate` tools are disabled in favor of Cortex.
- No `user_id` forced: Hermes reads/writes the global namespace, so WorkBuddy
  must do the same to actually share memory.
