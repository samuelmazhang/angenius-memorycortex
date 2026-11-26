# Memory Cortex v6.7.0

Long-term semantic memory service for AI agents. The current runtime uses
PostgreSQL + pgvector, ONNX multilingual embeddings, and a FastAPI HTTP API.
The production entrypoint is `server/src/api_pg.py`; the old SQLite/Chroma V4
implementation is historical only.

> **Source of truth for version**: the `VERSION` file (`6.7.0`) and `/health`
> (`{"status":"healthy","version":"6.7.0",...}`). Older "5.x"/"6.6.x" strings in
> historical docs, image tags (e.g. `memory-cortex-stack:5.2.0`) and the
> standalone MCP entry `cortex_mcp.py` are legacy and not authoritative.

## Repository Layout

```
memory-cortex/                                      ← single canonical source
├── server/                                         ← Cortex service (V5 unified)
│   ├── src/
│   │   ├── api_pg.py                              # V5 PG-backed HTTP API (entry)
│   │   ├── config.py                              # runtime config + embedding model loader
│   │   ├── mcp_server/cortex_mcp.py               # alternative entry: MCP protocol
│   │   └── sdk/
│   │       ├── cortex_client.py                   # Python SDK (used by mcp_server)
│   │       └── openapi_baseline.json              # schema for e2e_smoke.py
│   ├── docker/
│   │   └── unified/                               # V5 single-container runtime
│   │       ├── Dockerfile                         # postgres + pgvector + python + cortex in one image
│   │       ├── entrypoint.sh                      # PG init + python startup
│   │       ├── schema.sql                         # pgvector schema (idempotent)
│   │       └── HOT-RELOAD.md                      # hot-reload workflow
│   ├── scripts/run_docker_stack_unified.sh        # V5 launcher: bind-mounts + container start
│   └── tests/e2e_smoke.py                         # 48-test E2E suite
├── connector/                                      # Outbound adapters to agent runtimes
│   ├── arcadia/                                     # arcadia-agent integration
│   ├── claude/                                     # Claude Code integration (recall/session-end hooks)
│   ├── opencode/                                   # OpenCode integration (loader + memory tool)
│   └── workbuddy/                                  # WorkBuddy integration (MCP + SKILL.md contract)
├── server/src/
│   ├── mcp_server/cortex_mcp_server.py             # production MCP server (FastMCP, 104 tools)
│   ├── rerank.py                                   # ONNX Cross-Encoder reranker (v6.6.0)
│   ├── consolidate/                                # extract / conflict / summary / raptor / scheduler
│   ├── graph/                                      # PPR / communities / graph index
│   └── observability/                              # OTel config / tracing / metrics
├── docs/
│   ├── 架构总览.md, 设计.md, 评估回路.md, V4.x路线图与实施状态.md, 文档索引.md
│   └── ADR-052 + ADR-063..266 (fix decisions) + ADR-MAP.md
└── CI/CD: .gitlab-ci.yml, .pre-commit-config.yaml, .githooks/
```

## Quick Start (Docker, recommended)

```bash
cd /path/to/memory-cortex
bash server/scripts/run_docker_stack_unified.sh
# → starts container with 5 bind mounts, waits for /health, applies schema.sql
curl http://localhost:1880/health                   # → healthy, 5.0.0
python3 server/tests/e2e_smoke.py                   # → 48/48 PASS
```

## Hot-reload development loop

```bash
cd /path/to/memory-cortex
git pull                                             # get latest code
docker restart memory-cortex                         # 10 sec, data preserved
python3 server/tests/e2e_smoke.py                    # verify 48/48
```

Edits to `server/src/api_pg.py` etc. take effect on container restart
(no image rebuild needed) — bind-mounts hot-update via docker restart.

## Running tests

The repo ships three test layers:

| Layer | Command | Time | Purpose |
|---|---|---|---|
| Unit | `npm test` (= `pytest server/tests/`) | ~100s | All unit + integration tests, no external deps |
| v6.5.0 unit only | `npm run test:v65` | ~100s | The 12 v6.5.0 unit-test files (skip older) |
| LOCOMO eval | `npm run test:eval` | <1s | LOCOMO 1986 harness (mocked LLM judge) |
| E2E smoke | `python3 server/tests/e2e_smoke.py` | ~10s | Requires live server at `:1880` |

The **`server/tests/integration/`** directory holds B-layer (cross-module)
integration tests. They reuse fixtures from the unit-test files
(`FakeDB`, `_fact`, `_chunk`, `_two_clusters`) — see `_shared.py`. No
Postgres, no FastAPI, no LLM, no httpx. Run them via `npm test` (they
are auto-discovered under `server/tests/`).

To run only the integration tests:

```bash
python -m pytest server/tests/integration -v
```

Six scenarios cover:
- extract → conflict_resolve → quality (#A.2 + #NEW3)
- RAPTOR cluster locality + top_k truncation (#9)
- summarize_fn contract pinning (#C vs #9)
- OTel + RAPTOR coexistence (#6 + #9)
- 500-fact bulk pipeline scalability

## Quick Start (bare-metal, no docker)

```bash
PG_HOST=localhost PG_PORT=5432 python3 -m server.src.api_pg
# Container-less mode for local dev (assumes a local PG at :5432 with same schema)
```

## Connecting from arcadia-agent

arcadia-agent talks to cortex via `~/.arcadia/arcadia-agent/tools/cortex_memory_backend.py`
(simpler than `connector/arcadia/` which is kept as a reference template only).

Set `MEMORY_CORTEX_URL=http://localhost:1880` in `/root/.arcadia/.env` (or wherever the
env is loaded from). Restart arcadia-agent for the env change to take effect.

The `connector/arcadia/` directory contains the same logic for documentation/reference.

## Cross-machine image transport

To deploy to a new dev box, copy the repo and rebuild the image (V5.2.4+ flow;
no tar export/import needed):

```bash
# Target machine
cd /path/to/memory-cortex
docker build -t memory-cortex-stack:5.2.0 -f server/docker/unified/Dockerfile .
bash server/scripts/run_docker_stack_unified.sh
```

The Dockerfile + bind-mounts are self-contained — no image tarball transport.

## MCP protocol

Production MCP server is mounted on the main API at **`/mcp/`** (Streamable
HTTP, FastMCP 3.x) — 104 tools total: 6 friendly aliases plus 98 extended
tools. The aliases are `cortex_remember / cortex_recall / cortex_get /
cortex_forget / cortex_conflict_upsert / cortex_introspect`.

```bash
# Live endpoint (127.0.0.1:1880), requires the Accept header:
curl -s -X POST http://127.0.0.1:1880/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
# NOTE: omitting Accept returns HTTP 406.

# Standalone stdlib JSON-RPC MCP entry (legacy, version string 4.3.0):
docker exec memory-cortex python3 -m src.mcp_server.cortex_mcp --http 9000
```

Agent runtimes connect via `connector/<runtime>/`. WorkBuddy mounts
`connector/workbuddy/` as a skill and registers the `/mcp/` endpoint in
`~/.workbuddy/mcp.json`; see `connector/workbuddy/README.md`.

## Agent memory (arcadia ↔ cortex)

Cortex is now the full agent-memory layer, not just a KV store. Three-tier memory
(short/medium/long/permanent driven by importance → TTL), agentic `recall`,
auto graph mining (`auto_link`), active recall injection into the system prompt,
access-driven guided forgetting, and procedural (reusable-workflow) memory.
See [docs/Agent-记忆体集成.md](docs/Agent-记忆体集成.md) for the complete model,
agent-callable actions, lifecycle, and daily maintenance cron.

## Versioning

`VERSION` file = **6.7.0**. `server/src/api_pg.py` exposes the running version via
`/health` (currently `6.7.0`). The container image tag (`memory-cortex-stack:5.2.0`)
is a legacy artifact — the image ships no source; `api_pg.py` is copied in by
`cortex-deploy.sh` (see deployment section). The standalone `cortex_mcp.py`
still reports `4.3.0`; treat `VERSION` + `/health` as authoritative.

### 检索能力 (V6.6)

`POST /search` 是主检索入口（约 118 个 REST 路由之一），管线为：

1. **多路召回**：向量（pgvector cosine / HNSW）+ 字面（`ts_rank` + GIN）+ tag（JSONB）+ 可选 RAPTOR 摘要树。
2. **RRF 融合**：通道权重 vector 1.0 / lexical 0.25 / tag 1.0 / raptor 0.5。
3. **Cross-Encoder 精排**（可选，双闸门）：请求 `rerank=true` 且 `MEMORY_FEATURE_RERANK=1`；模型 `BAAI/bge-reranker-base` ONNX（量化版优先）。结果以 `ce_score` 注入最终打分，`rerank_off` 时排序与 6.5.1 完全一致（fail-open）。
4. **图谱信号**：`include_related` 触发 PPR 邻居扩展（`graph/` 模块，HippoRAG2 风格）；另有连通性打分项。
5. **最终打分** `_three_factor()`：rrf_norm / similarity / importance / recency / relevance / ce_score / raptor / graph 的加权和（未归一化，可 >1）。

Embedding 为双模型：`mpnet`（384 维，零填充到 vector(768)）与 `nomic-v1.5`
（768 维，默认新写入），按 `model_version` 隔离向量空间，避免混空间污染。
`GET /search/hybrid`、`GET /search/tts` 为兼容入口，二者都真实转发到 `search_memory()`，但只接受 `q`/`limit` 两个参数（会丢失 rerank/tag/model 等高级参数）；需要完整控制请用 `POST /search`。
详见 [CHANGELOG.md](./CHANGELOG.md) 6.5.x / 6.6.0 段。

## Files NOT here (deleted in V5.2.6 + V5.2.7 cleanup)

V4/V3-era code removed (sqlite/chroma legacy, V3 deployment scripts, V2/V3 design docs,
`_docker-images/` tar transport, V5.1 save/load-docker-images.sh).
Recover with `git log --follow <file>` if needed.


---

## Deployment to 8.2 (live container)

> **What this section covers:** how to push a code change from this repo
> to the running `memory-cortex` container on `127.0.0.1`, without
> rebuilding the Docker image. The container is bind-mounted via
> `docker cp` + `docker restart`, so the iteration loop is ~30s.

### Topology

```
┌─ developer box (Windows) ─────────────┐      ┌─ 127.0.0.1 (Linux) ──────────┐
│  this repo                            │      │                                   │
│  ├─ server/src/api_pg.py              │ git  │  /opt/cortex-deploy/work/        │
│  └─ docs/, CHANGELOG.md, ADR-*        │ push │   └─ mirrors this repo           │
│                                       ├─────►│                                   │
│                                       │      │  /opt/cortex-deploy/             │
│                                       │      │   ├─ work/   (git repo)          │
│                                       │      │   ├─ cortex-deploy.sh            │
│                                       │      │   └─ askpass-gitea.sh            │
└───────────────────────────────────────┘      │                                   │
                                                │  container: memory-cortex        │
                                                │   image: memory-cortex-stack:5.2.0│
                                                │   source: /app/src/api_pg.py      │
                                                │    (NOT bind-mounted; replaced    │
                                                │     by `docker cp` on each deploy)│
                                                │   model: /root/.arcadia/.../MiniLM │
                                                │    (NOT in image; copied once     │
                                                │     by `docker cp` bootstrap)     │
                                                │   data: PG (postgres in same      │
                                                │     container) — `/var/lib/...`   │
                                                │          docker volume            │
                                                └───────────────────────────────────┘
```

### Prerequisites (one-time)

| Step | Command | Notes |
|---|---|---|
| SSH to VM | `ssh <user>@127.0.0.1` | sudo is password-less for `<user>` |
| Bootstrap `work/` repo | `cd /opt/cortex-deploy && git clone http://<user>:<token>@127.0.0.1:16080/<user>/Memory-Cortex.git work` | Gitea token from user settings |
| Set work origin | `cd work && git remote set-url origin <Gitea HTTP URL>` | once per bootstrap |
| Bootstrap embed model | `docker cp <local MiniLM dir> memory-cortex:/root/.arcadia/plugins/memory-cortex/models/` | done once per VM |

### Day-to-day deploy (after prerequisites)

```bash
# 1. Local: commit + push
git add server/src/api_pg.py docs/
git commit -m "fix(cortex): ..."
git push origin main

# 2. Local: SSH to VM and run deploy
ssh <user>@127.0.0.1 '/opt/cortex-deploy/cortex-deploy.sh'

# 3. Watch the 5 stages
# === [1/5] git pull                          (fetch + reset --hard origin/main)
# === [2/5] backup + docker cp                (container file: .bak.<TS>)
# === [3/5] optionally apply schema.sql        (only if pass 'schema' arg)
# === [4/5] restart container                  (5-10s downtime)
# === [5/5] wait healthy + verify              (curl /health → 200)
```

### What the deploy script does (and why)

| Step | Action | Failure mode |
|---|---|---|
| `git pull --ff-only` in `/opt/cortex-deploy/work` | sync local mirror with Gitea | non-fast-forward → `git reset --hard origin/main` (loses local edits, never happens in practice) |
| Backup container file to `/app/src/api_pg.py.bak.<TS>` | keep N-1 versions inside container | none — `docker exec bash -c cp ...` is idempotent |
| `docker cp <new> memory-cortex:/app/src/api_pg.py` | replace running code WITHOUT rebuild | if the new code has a syntax error, container crashes → roll back via `docker cp <latest .bak>` |
| `docker exec ... chown root:root` | fix owner (SFTP default is 1000:1000) | not strictly required by Python, but matches image |
| `docker restart memory-cortex` | reload new code into Python process | 5-10s gap where /health is 502 |
| Wait healthy + smoke test (`/health` 200 + check ADR-067-Patch-N markers) | confirm deploy succeeded | if 60s timeout, immediately rollback to last `.bak` |

### Adding a feature to the deploy script

When the deploy script needs to copy additional files (not just `api_pg.py`),
edit `/opt/cortex-deploy/cortex-deploy.sh` step [2/5]:

```bash
# current: just api_pg.py
sudo docker cp "$WORK/server/src/api_pg.py" $CONTAINER:$CONTAINER_PATH

# add new file:
sudo docker cp "$WORK/server/src/<other>.py" $CONTAINER:/app/src/<other>.py
sudo docker exec $CONTAINER chown root:root /app/src/<other>.py
```

Schema changes go in step [3/5] — pass `schema` arg:

```bash
/opt/cortex-deploy/cortex-deploy.sh schema
# This also runs server/docker/unified/schema.sql via docker cp + psql -f.
```

### Rollback (if new code is broken)

```bash
ssh <user>@127.0.0.1 '
  set -e
  # List backups inside container
  BACKUP=$(docker exec memory-cortex ls -1t /app/src/api_pg.py.bak.* | head -1)
  echo "rolling back to: $BACKUP"
  docker exec memory-cortex cp "/app/src/$BACKUP" /app/src/api_pg.py
  docker exec memory-cortex chown root:root /app/src/api_pg.py
  docker restart memory-cortex
'
```

### Common pitfalls (validated 2026-08-04)

1. **Gitea token in URL gets eaten by shell** — embed token in `GITEA_URL=` constant, not inline `git clone`.
2. **HTTP vs HTTPS on Gitea port 16080** — `https://` triggers TLS handshake failure; use `http://` explicitly.
3. **Embedding model not in image** — v5.2.0 image does NOT include the ONNX model. First deploy on a fresh VM requires manual `docker cp` of `paraphrase-multilingual-MiniLM-L12-v2/` to `/root/.arcadia/plugins/memory-cortex/models/`.
4. **`/health` says healthy but `/search` returns 500** — the `get_embedding_model()` call runs lazily on first `/search`. If model file is missing, search dies. Always include `docker logs memory-cortex --tail 20` check after deploy.
5. **Container backup files persist across container restart** — `*.bak.*` files inside the container are NOT in the image layer, so they survive `docker restart` and even `docker stop/start`. They only disappear on `docker rm` + recreate.
6. **Old deploy script pointed to a non-existent GitLab repo** (`ExampleGroup/arcadia-memory-cortex.git`) — replaced in commit `977129a` with the Gitea URL. The version in `/opt/cortex-deploy/cortex-deploy.sh` after this commit is the working one.

### Smoke test (post-deploy, ~10s)

```bash
# On VM or local:
curl -s http://127.0.0.1:1880/health

# Verify all 5 patches landed (count occurrences of marker comments):
ssh <user>@127.0.0.1 'docker exec memory-cortex grep -c ADR-067-Patch-1 /app/src/api_pg.py'   # expect 2
ssh <user>@127.0.0.1 'docker exec memory-cortex grep -c ADR-067-Patch-2 /app/src/api_pg.py'   # expect 2
ssh <user>@127.0.0.1 'docker exec memory-cortex grep -c ADR-067-Patch-3 /app/src/api_pg.py'   # expect 2
ssh <user>@127.0.0.1 'docker exec memory-cortex grep -c ADR-067-Patch-4 /app/src/api_pg.py'   # expect 1
ssh <user>@127.0.0.1 'docker exec memory-cortex grep -c ADR-067-Patch-5 /app/src/api_pg.py'   # expect 1

# Quick end-to-end check (search writes access_stats = Patch-1 works):
BEFORE=$(ssh <user>@127.0.0.1 "docker exec memory-cortex psql -U postgres -d postgres -tA -c \"SELECT COUNT(*) FROM access_stats WHERE access_type='search'\"")
curl -s -X POST http://127.0.0.1:1880/search -H 'Content-Type: application/json'   -d '{"query":"deploy smoke","boost_relevance":true,"limit":5}' > /dev/null
AFTER=$(ssh <user>@127.0.0.1 "docker exec memory-cortex psql -U postgres -d postgres -tA -c \"SELECT COUNT(*) FROM access_stats WHERE access_type='search'\"")
# AFTER - BEFORE should be >= 5 (one insert per result returned)
```

### Where things live (paths cheat sheet)

| Host | Container | Purpose |
|---|---|---|
| `/opt/cortex-deploy/work/` | — | git working tree (mirror of Gitea main) |
| `/opt/cortex-deploy/cortex-deploy.sh` | — | deploy script (the one in this README) |
| `/opt/cortex-deploy/askpass-gitea.sh` | — | (optional) GIT_ASKPASS for token auth |
| `/opt/memory-cortex/server/src/api_pg.py` | — | **stale** — old deploy script copied here, ignored by container |
| — | `/app/src/api_pg.py` | the live code (real path inside container) |
| — | `/app/src/api_pg.py.bak.<TS>` | rollback targets (kept inside container) |
| — | `/root/.arcadia/.../MiniLM/` | ONNX model (one-time bootstrap) |

---

## Related docs

- `CHANGELOG.md` — version-by-version change log (see [5.3.9] for the 5 patches)
- `docs/ADR-2026-08-04-067-cortex-heat-reinforce-v6.md` — full design rationale
- `docs/ADR-2026-08-04-067-patch-1-search-heat-write.md` — minimal-fix ADR for Patch-1
- `docs/设计解读与差距审计-2026-08-04.md` — design-vs-code gap audit (2026-08-04)
- `docs/ADR-MAP.md` — ADR ↔ code cross-reference
