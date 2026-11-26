# OpenCode Connector for Cortex

Long-term semantic memory integration for **opencode agents**, backed by [Memory Cortex](https://github.com/Example-Group/memory-cortex).

> **Status:** V1.0.0 MVP. Connects opencode to a Cortex server via HTTP.

## What this connector provides

| File | Purpose |
|---|---|
| `cortex_client.py`         | Zero-deps HTTP client (stdlib `urllib` only) — 5 high-level actions |
| `memory_tool.py`           | Facade with input validation + unified `CortexError` — 7 actions |
| `opencode_cortex_loader.py`| Action schema (`get_cortex_tools`) + invoker dispatch (`build_invoker`) |
| `SKILL.md`                 | opencode skill manifest (YAML frontmatter + usage docs) |
| `demo_smoke.py`            | CLI demo that exercises 3 actions end-to-end |
| `scripts/smoke.py`         | CI-friendly smoke test (real server or `--mock`) |
| `tests/`                   | pytest suite — **50 tests** |

## Actions

| Action | HTTP route | Purpose |
|---|---|---|
| `remember`    | `POST /chunks` (+ working memory + extract) | Store a memory (CoALA auto ADD/UPDATE/MERGE) |
| `recall`      | `POST /search` (+ working memory context)  | Hybrid recall (vector + FTS5 + user_id filter) |
| `forget`      | `DELETE /chunks/{id}` or `POST /ttl/archive` or `POST /list`+loop | Delete/TTL/bulk delete |
| `search`      | `POST /search` (raw)                       | Backend-shaped response (total/results/rankings) |
| `audit_query` | `POST /audit/query`                        | Read cortex audit log (G7) |
| `introspect`  | `GET /health` + `/stats` + `/stats/ops`    | Self-aware metadata |
| `batch`       | serial loop                                | Batch remember/forget/search |

## Install

The connector is **vendored** under `connector/opencode/`. No `pip install` needed — just point your `sys.path`:

```python
import sys
sys.path.insert(0, '/path/to/memory-cortex')
from connector.opencode.opencode_cortex_loader import build_invoker
from connector.opencode.memory_tool import MemoryTool
```

Or run the demo directly:

```bash
python connector/opencode/demo_smoke.py --base-url http://localhost:1880
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `MEMORY_CORTEX_URL` | `http://localhost:1880` | Cortex server base URL |

## Quick start

```python
from connector.opencode.memory_tool import MemoryTool
from connector.opencode.opencode_cortex_loader import build_invoker

tool = MemoryTool(base_url='http://localhost:1880')
invoker = build_invoker(tool)

# Store
chunk = invoker['remember'](content='I prefer Python', tags='python,backend')

# Recall
hits = invoker['recall']('Python preference', top_k=3)

# Forget
invoker['forget'](chunk_id=chunk['id'])

# Audit
log = invoker['audit_query'](action='recall', limit=10)

# Introspect
info = invoker['introspect']()
```

## Action schema (for opencode agent dispatch)

```python
from connector.opencode.opencode_cortex_loader import get_cortex_tools
tools = get_cortex_tools()
# Returns list of {name, description, params: {param_name: {type, required, default, description}}}
```

## Tests

```bash
# Unit tests only (no server needed, 50 tests)
python -m pytest connector/opencode/tests/ -v

# Smoke vs mock backend (no server)
python connector/opencode/scripts/smoke.py --mock

# Smoke vs real cortex
python connector/opencode/scripts/smoke.py --base-url http://127.0.0.1:1880
```

## Layout

```
connector/opencode/
├── __init__.py                       # package marker
├── README.md                         # this file
├── SKILL.md                          # opencode skill manifest
├── VERSION                           # connector version (1.0.0)
├── cortex_client.py                  # HTTP client (urllib only)
├── memory_tool.py                    # facade + validation + CortexError
├── opencode_cortex_loader.py         # tool schema + invoker
├── demo_smoke.py                     # CLI demo
├── example_remember.py               # simple usage example
├── scripts/
│   └── smoke.py                      # CI smoke (real + mock)
└── tests/
    ├── test_cortex_client_smoke.py   # 16 tests
    ├── test_memory_tool_facade.py    # 19 tests
    ├── test_skill_loader.py          # 9 tests
    └── test_e2e_integration.py       # 6 tests (mock backend)
```

## Compatibility

| Connector version | Cortex server version | Status |
|---|---|---|
| **1.0.0** (this) | 5.3.9+ | MVP — 6 actions + batch |
| 2.0.0 (planned)  | 5.4.x | + session_summarizer, graph k-hop, procedures |

## Troubleshooting

### `CortexError: Connection refused`

The cortex server isn't reachable. Either:

- Run the cortex server: `bash server/scripts/run_docker_stack_unified.sh` (in the cortex repo)
- Set `MEMORY_CORTEX_URL` to the right host/port
- Use `--mock` for offline testing: `python scripts/smoke.py --mock`

### `CortexError: 404 not found`

The action called an endpoint the server doesn't have. Check the server version with `introspect()`:

```python
info = invoker['introspect']()
print(info['health']['version'])  # should be >= 5.3.9
```

### Tests fail with `ModuleNotFoundError: cortex_client`

Tests load modules via `importlib.util.spec_from_file_location` so they don't need to be installed as a package. If running from a different CWD, ensure you're inside the `memory-cortex` repo root.

## Why this exists (vs arcadia connector)

`connector/arcadia/` is a **personal patch set** for arcadia-agent (per user policy). This `connector/opencode/` is:

- **Mirror of arcadia structure** (cortex_memory_backend → memory_tool → tests)
- **Adapted for opencode's skill mechanism** (SKILL.md frontmatter + loader)
- **Independent code path** — won't conflict with the arcadia patch set
- **Reference template** for future opencode-ai partners

## See also

- `connector/arcadia/` — fully-featured arcadia integration (V6.3.1)
- `server/src/sdk/cortex_client.py` — full Python SDK (699 lines)
- `server/src/mcp_server/cortex_mcp.py` — MCP-protocol alternative