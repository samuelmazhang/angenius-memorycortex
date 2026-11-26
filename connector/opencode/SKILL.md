---
name: cortex-memory
description: Persist, recall, and manage long-term memories for opencode agents via the Memory Cortex service. Use when the user wants to remember facts, recall prior context, or audit stored memory.
version: 1.0.0
---

# Cortex Memory — OpenCode Agent Skill

Long-term semantic memory for opencode agents, backed by [Memory Cortex](https://github.com/AnGenius-Group/memory-cortex).

## What this skill provides

Six agent-callable actions plus a batch helper:

| Action | Purpose |
|---|---|
| `remember`    | Store a memory chunk (CoALA auto-decides ADD/UPDATE/MERGE) |
| `recall`      | Retrieve chunks relevant to a query (hybrid + optional working-memory context) |
| `forget`      | Delete / archive memories (by id, tag, TTL, user) |
| `search`      | Raw POST /search (full backend response: total/results/rankings) |
| `audit_query` | Read the cortex audit log |
| `introspect`  | Self-aware metadata: health, stats, ops counters |
| `batch`       | Serial batch of remember/forget/search |

## Prerequisites

1. Memory Cortex service running (default `http://localhost:1880`, override via `MEMORY_CORTEX_URL`).
2. opencode agent has access to this connector's directory.

```bash
# Optional: override default URL
export MEMORY_CORTEX_URL=http://127.0.0.1:1880
```

## Usage (Python)

```python
from connector.opencode.opencode_cortex_loader import build_invoker
from connector.opencode.memory_tool import MemoryTool

tool = MemoryTool(base_url='http://localhost:1880')
invoker = build_invoker(tool)

# Store
chunk = invoker['remember'](content='I prefer Python for backend', tags='python,backend')
# → {'id': 'chunk-abc123', ...}

# Recall
hits = invoker['recall']('backend preference', top_k=3)

# Forget
invoker['forget'](chunk_id='chunk-abc123')
```

## Usage (CLI demo)

```bash
python connector/opencode/demo_smoke.py --base-url http://localhost:1880
```

## Action schema (machine-readable)

```python
from connector.opencode.opencode_cortex_loader import get_cortex_tools
tools = get_cortex_tools()
# → list of {name, description, params: {param: {type, required, default, description}}}
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `MEMORY_CORTEX_URL` | `http://localhost:1880` | Cortex server base URL |

## Compatibility

| Connector version | Cortex server version | Notes |
|---|---|---|
| 1.0.0             | 5.3.9+               | MVP — 6 actions + batch |
| 2.0.0 (planned)   | 5.4.x                | + session_summarizer, graph k-hop, procedures |

## Files

- `cortex_client.py`         — zero-deps HTTP client (urllib only)
- `memory_tool.py`           — facade with input validation + CortexError
- `opencode_cortex_loader.py` — tool schema + invoker dispatch
- `demo_smoke.py`            — CLI demo
- `tests/`                   — pytest suite (35+ tests)

## License

Personal connector — follows `connector/hermes/` (which is itself personal patches).