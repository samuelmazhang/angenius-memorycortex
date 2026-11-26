## [6.6.0 direction #1] - 2026-10-19

Adds the **Cross-Encoder 精排 (rerank) stage** to `/search` — the one stage
the hybrid retrieval pipeline was missing. Before this, `/search` was 粗排
only: vector + lexical + tag channels fused by RRF, then re-weighted by
`_three_factor()`. Reranking can only re-order what recall already produced,
so it cannot rescue a chunk that fell below the recall cut — but within the
candidate set it is strictly more informative than dual-tower cosine, because
query and passage tokens attend to each other at every layer.

### New

- `server/src/rerank.py` — standalone ONNX Cross-Encoder module (no psycopg2
  dependency, mirroring `memory_taxonomy.py` so it is unit-testable without a DB).
  `rerank_scores()` returns `{chunk_id: sigmoid(logit)}`; `rerank_ids()` returns
  a blended order. Lazy session loading, thread-safe double-checked init.
- `POST /search` gains `rerank: bool = False` and `rerank_top_k: int = 30`
  (`ge=1, le=100`).
- Each result row gains `ce_score` (null when rerank did not run).
- `OPS_COUNTERS`: `rerank_applied`, `rerank_skipped`.
- `/search` audit details: `rerank_requested` / `rerank_effective` /
  `rerank_scored_n` — without these you cannot tell "never asked" from
  "asked but the model isn't installed".

### Changed

- `_three_factor()` gains an optional 6th param `ce` and adds `ce_term`
  (capped at **0.30**) to the composite. When `ce is None` the term is exactly
  0, so with rerank off the ranking is bit-identical to 6.5.1 — no silent
  drift for existing clients. The five 6.5.0 weights are untouched.

### Design notes worth keeping

- **Scores, not re-ordering.** `results.sort(key=composite_score)` runs after
  recall, so re-ordering the `candidates` list would be silently discarded.
  The CE signal has to enter as a term in `composite_score` to survive.
  `test_rerank_wiring_v6_6.py` pins this with an explicit
  `assertNotIn("candidates = _rerank_ids(")`.
- **Quantized-first model resolution.** `onnx_file()` prefers
  `model_quantized.onnx` (266 MB) over `model.onnx` (1.06 GB) — 4x smaller for
  CPU inference. Measured on this host: int8 loads in ~3s and scores 3
  candidates in 0.9s.
- **Fail-open everywhere.** Missing model, missing onnxruntime, score-count
  mismatch → recall order returned unchanged, logged at WARNING, search never
  500s.
- **`MEMORY_RERANK_MODEL_PATH` is exclusive** when set (no fallback to default
  candidate paths), so pointing it at a wrong directory to test degradation
  actually tests degradation.

### Env / ops

- `MEMORY_FEATURE_RERANK=1` — master switch (default off; double-gated with
  the per-request `rerank` flag).
- `MEMORY_RERANK_MODEL_PATH` — model snapshot dir (exclusive when set).
- `MEMORY_RERANK_MAX_SEQ_LEN` (128) / `MEMORY_RERANK_MAX_DOC_LEN` (512).
- Model: `BAAI/bge-reranker-base` ONNX export.

### Tests

- `server/tests/test_rerank_v6_6.py` (12) — unit: flag, fail-open, top_k
  truncation, alpha blending (pure-CE / pure-RRF / balanced), score mismatch.
- `server/tests/test_rerank_wiring_v6_6.py` (17) — wiring contract in
  `api_pg.py` by source inspection (double gate, no-reorder invariant, weight
  preservation, counters, audit fields, version stamp).
- Suite: 325 passed. The 10 failures in `test_consolidate_v6_5.py` /
  `test_otel_v6_5.py` are **pre-existing** — verified by `git stash`-ing this
  change and re-running the same two files (identical 10 failures).

### NOT deployed

Live service at 127.0.0.1:1880 still reports 6.5.1. Deploy needs SSH access
to the VM, which this host does not have (`Permission denied (publickey,password)`).
To ship it: `cortex-deploy.sh` on 8.2, and copy the model snapshot into the
bind-mounted `models/` path.

## [6.5.1 connector phase 2+3] - 2026-09-10

Closes the remaining 31-endpoint gap from the V6.5.1 server (server has
111 ops; connector V6.3.1 covered 78, phase 1 brought it to 80, this
commit brings it to ~104). Remaining gap after this commit: HTML/UI
endpoints (`/`, `/dashboard`, `/dashboard/html`) which aren't relevant
to connector workflows.

### V6.5.0 P2 write-time scheduler (ADR-258) — 12 methods

- `get_maintenance_stats()` — `GET /admin/maintenance/stats`
- `get_maintenance_dead(limit=50)` — `GET /admin/maintenance/dead`
- `enqueue_maintenance(chunk_id, force=False)` — `POST /admin/maintenance/enqueue/{id}`
- `flush_maintenance()` — `POST /admin/maintenance/flush`
- `run_maintenance_once()` — `POST /admin/maintenance/run-once`
- `admin_deprecate_by_type(type, *, dry_run=True, limit=1000)` — V6.1.0 governance
- `get_relations_batch(seed_ids, *, depth=2, max_per_node=8)` — ADR-263 multi-hop batch
- `patch_chunk(chunk_id, **fields)` — partial update
- `batch_create_chunks(items)` — one HTTP round-trip for N chunks
- `conflict_scan(*, source_key, type_filter, limit=100)` — preview conflicts
- `conflict_log(*, user_id, source_key, action, limit=50)` — audit chain
- `link_decay(*, decay_rate, source_id)` — time-decay strength
- `run_forgetting_guided(*, force=False)` — manual full sweep
- `get_ttl_archived(*, limit=100)` — read TTL-archived chunks
- `storage_evict(*, max_count, ratio)` — manual cold-evict
- `get_working_memory_key(session_id, key)` — single-key short path
- `delete_relations(memory_id)` + `delete_specific_relation(memory_id, target_id)`
- `get_chunk_with_embedding(chunk_id, *, include_embedding=False)`
- `get_graph_sample(*, limit, type_filter, category)` — corpus inspection
- `get_mode()` — server mode/feature flags
- `list_endpoints()` — capability discovery

### V6.1.0 governance + V6.5.1 observability — 9 methods

- `stats_by_deprecated(*, limit=50)`
- `stats_by_source(*, source=None)`
- `stats_by_type()`
- `stats_by_importance()`
- `stats_by_status()`
- `stats_embed_cache()`
- `stats_health()`
- `stats_ops()`
- `get_metrics()` — Prometheus text format (22 cortex_* metrics)

### _request enhancement

- New `raw=False` kwarg: when `True`, returns `resp.text` instead of
  `resp.json()`. Required for `get_metrics()` since the Prometheus
  exposition format is not JSON. Defaults to `False` (JSON) so existing
  call sites are unaffected.

### Tests

- `TestV650P2Scheduler` (12 tests): every new P2 + stats method exercised
  against the live V6.5.1 server at `127.0.0.1:1880`.
- `TestStatsBreakdown` (8 tests): the V6.1.0 + V6.5.1 stats-by-* methods.

### Verified

- 74 passed, 5 skipped in 59s (LM Studio 9b unreachable, expected).
- Up from 54 passed before phase 1, 54 after phase 1 → 74 with this
  commit. +20 new tests, 0 regression.

## [6.5.1 connector] - 2026-09-10

Hermes connector upgrade: V6.3.1 → V6.5.1 (server already on 6.5.1 since
2026-09-01). Driven by gap analysis of `http://127.0.0.1:1880/openapi.json`
(111 ops) vs the V6.3.1 connector surface (80 unique HTTP calls → 78%
coverage). Two calls were **404/405-ing every invocation**:

### BREAKING-FIX: server contract drift in V6.5.0

1. **`POST /links/auto-link` → 405 Method Not Allowed**
   - Used by `cortex_memory_backend.auto_link_from_search(query, ...)`
   - The endpoint was removed in cortex V6.5.0: auto-linking is now the
     P2 scheduler's job (`memory_maintenance_queue` +
     `/admin/maintenance/run-once`; see server CHANGELOG [6.5.0 Phase 2
     #NEW3b] + ADR-258).
   - **Fix**: rewrite `auto_link_from_search` to do the search, then call
     `create_link(source_id, target_id, link_type='related',
     source='auto_from_search')` per pair (mirrors `auto_link_new_chunk`'s
     pattern). Returns local count of links created (no longer relies on
     server-side `{'created': N}` response).

2. **`GET /relations/temporal/{src}/{tgt}/{rel}/bi-temporal` → 404 Not Found**
   - Used by `cortex_memory_backend.get_relations_bitemporal(source_id, target_id, relation_type)`
   - Server V6.5.1 replaced with `GET /relations/temporal/{src}` — returns
     ALL temporal relations for the source; client filters locally.
   - **Fix**: rewrite `get_relations_bitemporal(source_id=None, *,
     target_id=None, relation_type=None, limit=None, **_unused)` to use the
     new endpoint + post-filter. Backward-compat: also accepts
     `memory_id=` (legacy kwarg name) as alias for `source_id=`, plus
     `limit=` now honored (previously silently ignored).

### Added tests

- `test_auto_link_from_search` — verifies the rewritten client-side pair-wise
  builder returns an int (not 405) and stays within `top_pairs` cap.
- `test_get_relations_bitemporal` — verifies both the new positional
  signature and the legacy `memory_id=` + `limit=` kwargs work.

### Deferred to follow-up commits

- V6.5.0 P2 scheduler endpoints: `/admin/maintenance/{dead,stats,enqueue,flush,run-once}`,
  `/relations/batch`, `PATCH /chunks/{X}`, `POST /chunks/batch`, `/conflict/scan`,
  `/conflict/log`, `/links/decay`, `/forgetting/guided`, `/ttl/archived`.
- V6.1.0 governance: `/admin/deprecate-by-type`, `/stats/by-source`,
  `/stats/by-deprecated`, `/stats/by-importance`, `/stats/by-status`,
  `/stats/by-type`.
- V6.5.1 observability: `/metrics` (Prometheus 22 metrics), `/stats/embed-cache`,
  `/stats/health`, `/stats/ops`, `/stats/ops/reset`.

### Verified

- `openapi.json` queried against `http://127.0.0.1:1880` — 111 ops total
- Server V6.5.1 alive, 2625 chunks / 14875 relations / 2603 vectors
- `curl POST /links/auto-link` → 405 (path removed)
- `curl GET /relations/temporal/src/tgt/rel/bi-temporal` → 404 (path removed)
- `curl GET /relations/temporal/{src}` → 200 (new shape working)
- blackbox tests pass locally before push (commit-level)

## [6.5.1] - 2026-09-01

Patch release over v6.5.0. No breaking API changes -- just code we already
had but hadn't stamped a version on yet, plus a few build fixes that
were landed after the v6.5.0 tag.

Build / deployment
------------------
- venv-baked dependencies: business pip packages (fastapi, fastmcp 2.x,
  opentelemetry-*, onnxruntime, etc.) now install at image build time
  into /opt/cortex-venv instead of being required on the host. Image
  becomes self-contained; the only mutable parts are the bind-mounted
  source tree at /app/src and the PG volume.
- China-friendly apt mirrors: debian base+security routed through
  mirrors.ustc.edu.cn + mirrors.aliyun.com (fallback), PostgreSQL pgdg
  through mirrors.aliyun.com/postgresql/, pip through
  pypi.tuna.tsinghua.edu.cn. Default repos were slow / unreachable
  from the 8.2 lab; build time dropped from >15 min (hung) to ~5 min.
- bootstrap wget+gnupg via default repos before the China mirror swap,
  so the mirror URLs can be written to .sources files without losing
  the tools that need to fetch them.
- Dockerfile # syntax=docker/dockerfile:1.4 + cache mount for
  /root/.cache/pip so the wheel cache survives across rebuilds.
- /app/src bind-mounted read-only from /opt/cortex-deploy/work/server/src
  on the host, so `git pull` is enough to update source -- no image
  rebuild needed for code-only changes.

Observability
-------------
- New GET /metrics endpoint (Prometheus text format, no new pip
  dependency) exposes 22 cortex_* metrics covering scheduler state,
  queue counts (pending/processing/done/dead), lease/flush/poll
  intervals, and total chunks. Scrape interval target: 15s.
- ADR-258 deprecates /opt/cortex-deploy/cortex-memory-maintenance.sh;
  the v6.5.0 P2 worker is a strict superset (real-time vs daily cron,
  PG-persisted vs in-process, dead-letter vs silent failure).
- ADR-260 documents the multi-worker horizontal scale pattern: the PG
  queue's SELECT FOR UPDATE SKIP LOCKED already provides the row-level
  coordination needed for N workers; the env knobs (flush_interval,
  worker_poll, lease_seconds, max_retries) are listed for tuning.

Verified
---------
- /health returns version="6.5.1"
- POST /chunks still end-to-end through the P2 worker (enqueue ->
  flush -> claim -> extract -> resolve -> mark_done within ~2s)
- /admin/maintenance/stats reports the new worker_id
- /metrics scrape returns 22 metrics, no 500

## [6.5.0 Phase 2 #NEW3b — write-time maintenance scheduler] - 2026-09-01

Wire the Phase 1 #A.3 ConsolidationScheduler into the live write path. Replaces
the "manual /forgetting/guided + /forgetting/cleanup" cron-only workflow with
an in-process background worker.

What's new
----------
- **memory_maintenance_queue** PG table (chunk_id, status, retry_count, lease)
- ConsolidationScheduler v6.5.0 P2:
    * write-time enqueue: ``POST /chunks`` → in-memory buffer (200ms flush)
    * single worker thread drains queue via SELECT ... FOR UPDATE SKIP LOCKED
    * 30s lease + max_retries=5 (default) → status='dead' for poison chunks
- New endpoints: GET /admin/maintenance/stats, GET /admin/maintenance/dead,
  POST /admin/maintenance/run-once|enqueue/{id}|flush
- 35/35 scheduler unit tests pass (15 Phase 1 + 12 Phase 2 + 8 boundary)
- cortex-deploy.sh now deploys consolidate/* + incremental migrations

Backward compat
----------------
- Legacy Phase 1 (in-memory deque + threshold poll) still works (unchanged API)
- /forgetting/guided + /forgetting/cleanup endpoints untouched

Operational notes
-----------------
- CORTEX_SCHED_ENABLED=0 disables (falls back to Phase 1 behavior)
- CORTEX_SCHED_STATE_PATH=/tmp/cortex_scheduler_state.json persists counters


## [6.5.0 Phase 2 #6] - 2026-08-23 (FastAPI OpenTelemetry, ROI 7)

按 PRD §11.1 / roadmap §11.1 把 Phase 1 推迟的 OpenTelemetry 落地。
新增 `server/src/observability/` 包（仿 `server/src/graph/` /
`server/src/consolidate/` 风格），FastAPI 接入分布式 tracing + HTTP
metrics。预期效果：debug 吞吐 +30%。

**触发条件 (per #6 brief)**
| 条件 | 行为 |
|---|---|
| `OTEL_ENABLED=false`（默认） | `setup_observability(app)` 短路返回。FastAPI app 不被动；无 middleware、无 per-request overhead。 |
| `OTEL_ENABLED=true` 且 SDK 可导入 | 装 tracer + meter provider + 40 行 ASGI middleware，每请求一条 span + counter/histogram。 |
| `OTEL_ENABLED=true` 但 SDK 缺失 | 启动时 WARNING 一次并保持 no-op，不抛异常（不破坏 slim venv）。 |

**Server 变更 (server/src/)**
- 新包 `observability/`（5 模块）：
  - `config.py` — `OtelConfig` dataclass + `load_otel_config()`（无 OTel 依赖）
  - `tracing.py` — `TracerProvider` + exporter 工厂（console / otlp / memory / none）+ no-op fallback + `start_span()`
  - `metrics.py` — `MeterProvider` + 3 个 instrument（request count / duration histogram / active gauge）
  - `instrumentation.py` — `setup_observability(app)` 一键接入 FastAPI；不引入 `opentelemetry-instrumentation-fastapi`（避免版本锁冲突 + 重复 span）
  - `__init__.py` + `CHANGELOG.md`
- `api_pg.py` — CORS middleware 后调 `setup_observability(app, service_version="6.5.0")`，整段 `try/except` 包裹（observability 永不破坏 app 启动）
- `config.py` — `CortexConfig` 加 7 个 OTel 字段（mirror env vars）
- `.env.example` — 加 OTel env vars 注释段

**关键设计约束 (设计维度而非行数维度)**
- **零依赖**：`opentelemetry-*` SDK 不在 requirements.txt。所有 import 包在 capability probe 内，slim venv 上现有 116 测试零回归。
- **零开销 default-off**：`OTEL_ENABLED=false` 时 middleware 不安装，per-request 无任何检查。
- **一键接入**：`setup_observability(app)` 一个函数搞定 tracing + metrics + middleware + shutdown hooks；幂等。
- **Exporter 可插拔**：`console` / `otlp` / `memory` (测试) / `none`。OTLP 不可用自动 fallback 到 console。
- **默认 OTLP endpoint**：`http://localhost:4317`（OpenTelemetry Collector gRPC 默认端口）。
- **健康检查路径排除**：`/health` `/metrics` `/static` `/favicon.ico` 默认不出 span，避免噪音 + 节省 collector 配额。

**Excluded URLs & Headers**
- `OTEL_EXCLUDED_URLS=/health,/metrics,/static,/favicon.ico`（默认）
- `OTEL_CAPTURE_HEADERS=false`（默认 — PII 风险）
- `OTEL_SAMPLE_RATIO=1.0`（默认 always-on，可降采样）

**不在 #6 范围（推迟到 follow-up）**
- `opentelemetry-instrumentation-psycopg2` / `-httpx` 自动 instrumentation（需版本锁）
- structured log → trace_id 桥接（Phase 2 stretch）
- 路径感知 sampling（高流量时再加）

**D 风格诚实归因**
- 没装 OTel SDK 也能跑完全部 142 个现有测试（capability probe 正确 no-op）。
- 测试用 `OTEL_EXPORTER=memory` + InMemorySpanExporter + InMemoryMetricReader，避免外部 collector 依赖。
- 故意没引入 `opentelemetry-instrumentation-fastapi`：版本错配会导致重复 span；手写 40 行 middleware 简单可控。

---

## [6.1.0] - 2026-08-10 (knowledge-center governance + data governance)

按 user 路线图决策: cortex 直接当知识中心, 不另建. 数据治理优先.

**Server 变更** (server/src/api_pg.py):
- 加 ALLOWED_TYPES whitelist (general/knowledge/preference/skill/fact/
  project/session_summary/tool_experience/environment-fingerprint/test/context)
- 加 _validate_type() 在 /chunks POST + /chunks/{id} POST (拒绝 invalid type)
- 加 /admin/deprecate-by-type (V6.1.0 governance, 批量标 is_deprecated)
- 加 /stats/by-source (按 source 字段统计, 来源可视化)
- 加 /stats/by-deprecated (按 is_deprecated 字段统计, 治理可视化)
- VERSION 6.0.1 -> 6.1.0
- app = FastAPI(version="6.1.0")

**Connector 变更** (connector/hermes/cortex_memory_backend.py):
- 加 ALLOWED_TYPES mirror (client-side fast-fail before round-trip)
- 加 _validate_type() helper
- create_session_summary: 强制 user_id 必填 (KB per-user isolation)
- upsert_session_conflict: 强制 user_id + source_key 必填
- version: 5.3.8 -> 5.3.9

**Step A 实施** (部署后执行):
- /admin/deprecate-by-type {"type": "recovered", "dry_run": true} 先看影响
- 583 条 recovered 批量标 deprecated (cron job 噪声)
- 后续审计通过 /chunks/{id} POST 重新激活

**Step E 跳过**:
- 抽样 10 条 session_summary 看是否跟 general 重叠 — 95% 是 verify/probe 测试
  残留, 真实生产 session_summary 太少, 去重 ROI 低. 跳过.

**验证** (部署后):
- pytest 14/14 PASS (无回归)
- curl /admin/deprecate-by-type dry_run (返回 583)
- curl /stats/by-type (返回 11 个类型, recovered 不变但 deprecated 字段=583)
- curl /stats/by-source (按 source 分布)
- 写入新 chunk with invalid type (应返回 400)

**D 风格诚实归因**:
- Step E 跳过理由: 数据规模不够 (session_summary 95% 是 verify 残留, 真实生产
  数据 1-2 条). 真生产数据要等 session_summarizer 跑几周才有意义.
- Step D (audit variant) 已在 V5.3.7+ 实现 (list_session_summaries_for_user_include_superseded),
  本轮无需新增.

---

## [6.0.1] - 2026-08-10 (V5.4 conflict-resolve 真实施 + BIGINT 修复)

## [6.0.1] - 2026-08-10 (V5.4 conflict-resolve 真实施 + BIGINT 修复)

按 recall 测试发现: time-decay + recency 需要写入碰撞解决 + version chain.
调研发现自家 ADR-068 (V5.4 conflict-resolve) 设计完整, 但 server 端
`_resolve_conflict_for_chunk` 跟 `/consolidate/run` 用 `f"hist_{uuid}"` 写
`memory_history.id` (BIGINT 列), 触发 SQL syntax error, SUPERSEDE 路径
从来不工作 — 旧事实从不被 deprecated.

修复: server 端 2 处用 `int(time.time() * 1e6) % 2000000000` 替代 string,
真因 BIGINT 列不兼容.

**Server 变更** (`server/src/api_pg.py`):
- line 2166 (conflict/upsert): `f"hist_{uuid.uuid4().hex[:12]}"` → `int(time.time()*1e6) % 2000000000`
- line 2499 (consolidate merge): 同样修法
- app = FastAPI(version="6.0.1") (title 同步)
- VERSION 文件升 6.0.0 → 6.0.1

**Connector 变更** (`connector/hermes/`):
- `cortex_memory_backend.py`:
  - 加 `upsert_session_conflict()` (包 /conflict/upsert, source_key 桶)
  - 加 `list_session_summaries_for_user_include_superseded()` (audit 变体)
  - 修 `list_session_summaries_for_user`: 默认过滤 is_deprecated=true
  - version: 5.3.7 → **5.3.8** (新 connector API)
- `session_summarizer.py`:
  - `summarize_session()` 写入 cortex 改用 `upsert_session_conflict`
  - source_key = `f"session_summary:{user_id}:{session_id}"` (per session+user)
  - 同 session 多次摘要自动 SUPERSEDE 形成 version chain
  - 结果加 `cortex_decision` 字段 (action, old_chunk_id, reason)
  - version: 5.3.7 → **5.3.8** (行为变更)
- `tests/test_session_summarizer_blackbox.py`:
  - 加 TestConflictResolve (4 tests: source_key 必填 / 1st=noop 2nd=supersede /
    superseded 过滤 / summarize_session 走 conflict 路径)

**验证**: pytest 14/14 PASS (post-deploy, 0 SKIPPED).

**部署**:
- /opt/cortex-deploy/work git pull fast-forward 876a974 → 71ffd8a → 71ffd8a+1 (version bump)
- sudo docker restart memory-cortex (10s, bind-mount /app/src 自动重读)
- /health 返回 version=6.0.1, /conflict/upsert 真 supersede, /conflict/log
  查到新 entry.

**D 风格诚实归因**:
- 上一轮 commit `71ffd8a` 是代码 push 但**没有 bump 版本号**,部署后 /health
  还是 6.0.0 (代码是新代码但 version 字段没动), 验证版本要看 commit hash
  不能看 /health. 本次 commit 补版本号.
- ADR-068 (V5.4 conflict-resolve) 之前**从来不工作**,不是新引入的 bug.

---

## [5.3.8] - 2026-08-10 (conflict-resolve connector wrapper)

详见 [6.0.1] 条目 — connector/hermes/ 的 session_summarizer + cortex_memory_backend
新增 upsert_session_conflict 等 API, 配合 server [6.0.1] 修复一起发布.

---

## [5.3.7] - 2026-08-10 (session_summarizer + auto-extract session context)

### V5.3.7 — 跨会话自动摘要 (9b → cortex, 写入 + 注入双轨)

按用户 a→b→c 模式 2 实施: hermes 的 45,549 条历史消息 (22,428 tool 输出 = 49%) 是知识金矿,但每个新 session 起步空白。auto-summarizer 让"上次的对话要点"自动出现在下次 session 的 system prompt 里。

**新增模块**:
- `connector/hermes/session_summarizer.py` (12 KB, 9b 摘要 + cortex 推送 + 本地 SQLite 缓存)
- `connector/hermes/scripts/install_session_summarizer.py` (mtime-aware sync)
- `connector/hermes/tests/test_session_summarizer_blackbox.py` (6 tests e2e)

**新增 connector API**:
- `cortex_memory_backend.create_session_summary(content, *, importance=7, tags, session_id, source, extractor, trigger)` — 写 `session_summary` type chunk,带 retry + circuit breaker。importance=7 默认进 long_term (180 天)。

**触发机制** (hermes 端,不在本仓库,但需要 hermes-agent 同步改动):
- `SessionDB.end_session()` 末尾 fire-and-forget `summarize_session_async()` — 仅在真结束 (rowcount>0) 时触发,避免 no-op 浪费 9b 调用。
- `SessionDB.append_message()` 渐进式: 每 50 条 assistant 消息重跑一次,崩溃也不漏。
- `agent/conversation_loop.py::_restore_or_build_system_prompt` 在 `stored_state == "missing"` 时注入 prior_summaries 到 system_prompt (避免 prefix-cache 抖动)。

**双轨写入**:
1. 本地 `state.db.session_summaries` 表 (新加) — 快速、离线可用
2. cortex V6 long_term — 跨设备、180 天过期

**Schema 变更** (向后兼容):
- `state.db` 加 `session_summaries` 表 (10 列 + 2 indexes),由 `SessionDB._init_schema` 自动 CREATE IF NOT EXISTS 创建。

**验证**: 6/6 PASS (V5.3.7 pytest e2e + V3 hermes-verify 4/4 PASS) — 真实 9b 抽取 2 facts,真实 cortex 写入 `mem_xxx`,回环 search 命中。

**禁用**: 环境变量 `HERMES_DISABLE_SESSION_SUMMARY=1`。

**ADR 累计**: V5.3.7 不需要新 ADR,完全在 V5.3 现有架构内 (cortex chunk + hermes SQLite 双轨)。

---

## [6.0.0] - 2026-08-06 (ADR-067 V6.0 完整合入 + ADR-068/069 收尾 + M8 taxonomy + 070/071/072)

### V6.0 — 激活扩散 + 工作记忆 + 巩固系统（设计意图）与 ADR-067 完整实施（实施基线）

按 ADR-067 全 10 模块（M1-M10）+ ADR-068 冲突解决 + ADR-069 数据治理收尾。

**新增模块**:
- `server/src/memory_taxonomy.py` (独立模块, 可单元测试, 不依赖 psycopg2)
- `server/tests/test_taxonomy_boost.py` (18 个测试, 全部通过)
- `server/tests/__init__.py`

**ADR-067 → Accepted**:
- M1 access_stats 计数 + M2 forgetting + M3 metadata_store + M9 search 热度项
- M4 reconsolidate 关联衰减 + M5 rumination 矛盾检测
- M6 eval/feedback + M7 storage/evict + M8 taxonomy boost (本轮)

**ADR-068 V5.4 conflict-resolve 已上线**:
- schema 加 4 列 + memory_conflict_log 表
- /conflict/upsert + /conflict/scan + /conflict/log 端点

**ADR-069 数据治理 + WebUI 修复 + cron 守护已上线**:
- WebUI bind-mount + API 自动探测 3 级覆盖
- 4 件套: SA 清 + working_memory 清 + tier=permanent + autosourcekey 触发器
- cron `cortex-data-cleanup` 04:30 沉默 watchdog

**新 ADR (本轮新增)**:
- ADR-070: cron 告警通道 (telegram + heartbeat + 沉默降级链)
- ADR-071: connector/hermes/cortex_api.py 死代码清理 (V2 时代 import 失败, 89KB 删除)
- ADR-072: WebUI E2E (Playwright, 4 个 MVP test)

**回滚脚本**: `server/scripts/rollback_cortex.sh` (commit aa16495)

---

---

## [5.4.0] - 2026-08-05 (ADR-068 冲突解决 + 周期更新)

### V5.4 conflict-resolve: 新事实优先旧事实,周期更新

按用户原话实施 (ADR-068): "新信息跟老信息冲突时,要优先更新老记忆,如果不能合并就删除旧的相关内容,添加新的内容. 根据新的决策优先旧的,如果确定是事实,新的事实要覆盖就的内容."

**新增能力 (3 endpoint + 1 审计表)**:

- `POST /conflict/upsert` — write-path 主入口。Body 必填 `content` + `source_key`,选填 `user_id`/`importance`/`confidence`/`auto_resolve`。命中同 `(user_id, source_key)` 桶内旧事实时自动决策 SUPERSEDE 或 MARK_ONLY。
- `POST /conflict/scan` — periodic 入口。Body 选填 `lookback_days`(默认 7) / `dry_run` / `max_buckets`。兜底扫多版本桶,处理异步竞态 + 手工 SQL 写入。
- `GET /conflict/log` — audit 查询。Query 选填 `user_id` / `source_key` / `action` / `limit`。

**Schema 变更** (向后兼容,旧记录零影响):

- `memory_chunks` 加 4 列: `is_deprecated BOOLEAN` / `deprecated_at TIMESTAMPTZ` / `superseded_by TEXT` + `metadata->>'source_key'` (jsonb 表达式,无新物理列)
- 新增 `memory_conflict_log` 表: `user_id` / `source_key` / `old_chunk_id` / `new_chunk_id` / `action` / `confidence` / `decided_by`
- 新增 3 个 GIN/B-tree 索引: `memory_chunks_deprecated_idx` / `memory_chunks_source_key_idx` / `memory_conflict_log_user_idx` / `memory_conflict_log_action_idx`

**决策矩阵**:
- 新 `confidence >= 0.7` AND 新 `importance >= 旧` → **SUPERSEDE**: 旧 chunk `is_deprecated=TRUE` + 写 `memory_history` + 写 `memory_conflict_log`
- 否则 → **MARK_ONLY**: 旧 chunk metadata 标 `challenged_by`,不删
- 桶内无旧事实 → NOOP

**周期任务接入**:
- `connector/hermes/scripts/cortex_maintenance.py daily()` 加 2 步: `POST /conflict/scan` + `GET /conflict/log?limit=50`
- 复用现有 hermes cron 调度 (daily/weekly/hourly),无需新增 scheduler

**新增文件**: `docs/ADR-2026-08-05-068-cortex-conflict-resolve.md`
**修改文件**:
- `server/docker/unified/schema.sql` (新增 4 列 + 1 表 + 3 索引)
- `server/src/api_pg.py` (新增 ~330 行: `_resolve_conflict_for_chunk` + 3 endpoint)
- `connector/hermes/scripts/cortex_maintenance.py` (daily 加 2 步)
- `docs/ADR-MAP.md` (登记 ADR-068)

**手动迁移命令** (旧库升级):
```sql
ALTER TABLE memory_chunks
  ADD COLUMN IF NOT EXISTS is_deprecated BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS deprecated_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS superseded_by TEXT;
```
新表 `memory_conflict_log` 由 entrypoint 自动 CREATE。

---

## [5.3.9] - 2026-08-04 (ADR-067 系列 5 个 Patch)

### 热度 + 推翻机制完整实施

按 ADR-067 主方案 (`docs/ADR-2026-08-04-067-cortex-heat-reinforce-v6.md`),
把设计文档中描述但未实现的 5 个 stub 全部改为真实实现, 激活完整热度机制.

### Patch-1: search→access_stats 热补丁

**现象**: `/search` 命中 chunk 后没写入 `access_stats` → `access_count` 永远是 0
→ `_relevance_scores()` 永远返回 0 → `composite` 热度项 `0.1*rlv` 无效
→ `forgetting/guided` 永远不升级 tier.

**根因**: `/memories/{id}/refresh` 端点存在, 但 `search_memory` 主路径没调用它.

**修法**: 在 search 返回前, 对命中 chunk 写 access_stats, 失败不影响主路径.

**E2E 验证** (本地 18880 + 远程 127.0.0.1:5432):
- baseline: `short=2070, medium=0, long=0` (热度机制死锁)
- 30 次 search 后 forgetting/guided: `checked=10, upgraded=9`
- 终态: `short=608, medium=1386, long=76` (热度机制工作)
- access_stats search-type: `0 → 130`

### Patch-2: eval/feedback 真实实现

按 `docs/评估回路.md §四` 把 stub `{status:"received"}` 改为:
- 写入 `access_stats` (`access_type='eval_feedback'`)
- 写入 `memory_access_audit` (`judge_method` + score 详情)
- 返回 `updated`/`total` 计数

### Patch-3: storage/evict 真实实现

按 `docs/设计.md §10.2` 把 stub 改为末位淘汰:
- 保护规则: `tier='long_term' OR importance>=8` 不淘汰 (并集)
- 排序键: `access_stats` 30 天访问次数 (越低越冷门)
- 写 `forgetting_log` (`action='evict', reason='low_relevance'`)
- 然后 DELETE

### Patch-4: reconsolidate 真实实现

按 `docs/设计.md §2.1` 把 stub 改为关联治理:
- 关联强度阶梯衰减: `strength *= 0.95` (RELATION_STRENGTH_DECAY)
- 共同召回升级: `co_occurrence_count >= 3 → strength += 0.1` (上限 1.0)
- 弱关联清理: `strength < 0.1 → DELETE`

### Patch-5: rumination/run 真实实现

按 `docs/设计.md §4` 把 stub 改为矛盾检测 + 碎片合并:
- 矛盾检测: 最近 N 天两两相似 > 0.75 → metadata 打 `potential_contradiction_with`
- 碎片合并: `importance < 4 AND LENGTH(content) < 200` → 合并到更高 importance 的同 tag chunk
- 写 `memory_history` (`action='merge'`)

### 兼容性

- 全部纯加性改动, 不破坏现有 90 个 endpoint
- 每个 Patch 失败均 try/except 包住, 不影响主路径
- `git revert` 可逐个回滚 (5 个 commit 各自独立)
# Changelog

Memory Cortex 版本演进记录。版本号遵循 [SemVer](https://semver.org/)。
当前版本见 [VERSION](./VERSION) 文件。

---

## [5.3.0] - 2026-08-02

### 检索质量跃升：真混合检索 + 图谱回填 + GraphRAG 多跳推理

V5.2 的 `/search` 实为纯向量检索（GIN tsvector 索引建了但没进搜索路径），
文档宣称的"BM25+向量 RRF"名存实亡。本次把检索能力真正补齐，并让图谱
从极度稀疏（110/1956 relations）长成可用于多跳推理的子图。

#### 混合检索（P0-1）：/search 三路 RRF 融合

- **向量**（cosine, HNSW 加速）
- **字面**（`ts_rank` over GIN tsvector；用 `_build_lexical_query` 生成
  OR 语义词条，替代 `plainto_tsquery` 的 AND 语义——后者多词查询几乎 0 命中）
- **tag**（独立召回通道 + 结果硬过滤：用户指定 tag 时，带该 tag 的记录
  即使语义弱相关也精确召回，最终结果只保留带任一请求 tag 的 chunk）

RRF (k=60) 融合后 `boost_relevance` 综合评分：
`composite = 0.6×rrf_norm + 0.4×sim_effective + 0.05×(imp/10) + 0.1×recency + 0.1×relevance`。
- rrf_norm：多路都能命中 → 晋升（RRF 主导）
- sim_effective：字面精确命中时 sim 至少按 0.5 计，避免"向量 sim=0(未进向量候选)"
  的系统性低估——专有名词/路径的记忆优先浮现
- `relevance` 由 `access_stats` 访问频次推导（V5 PG 无 V2 的 relevance_score 列）
实证平衡：字面精确(sim0) > 不相关语义近似；但 sim0.8+ 真相关仍主导。
（排序权重经 V5.3.1 → V5.3.2 两轮迭代定型，详见下。）

返回新增字段：`hybrid`、`rankings`（各路命中数）、`composite_score`、`rrf_score`、`relevance_score`。
`hybrid=false` 保留纯向量降级路径。

**坑**：psycopg2 参数占位顺序极易错位——SQL 里 `%s` 按文本顺序绑定，
混用 `embedding <=> %s::vector`（SELECT）与 where 过滤参数、ORDER BY 时，必须
按 `[emb, where_params..., emb, top]` 顺序传参；字面查询是从 `[lq, lq, where_params..., lq, top]`。
tag 数组匹配用 `jsonb_array_elements_text`（`?|` 只匹配 jsonb 键，不匹配数组值）。

#### 图谱回填（P0-3）：POST /graph/mine

V5 PG 迁移后 `memory_relations` 停止生长，110/1956 稀疏到无法支撑图谱推理。
`/graph/mine` 用三种纯离线启发式批量挖掘关系：
- **similar**：向量相似度 ≥ 阈值（默认 0.72）的两两建链
- **tag-shared**：同一 tag 组内两两建链（strength 0.6）
- **same-source**：同来源 chunk 弱链（strength 0.4）

幂等（`ON CONFLICT (memory_id_1, memory_id_2) DO UPDATE` 累加强度），
后台线程（`asyncio.Lock` 防重入）。实测：**110 → 1394 relations**。

#### GraphRAG 多跳推理（P1）：GET /graph/khop + 邻居门控

- **`/graph/khop`**：从 seed chunk 出发 BFS 沿 relations 扩展 k 层（strength 阈值
  过滤弱链），返回每跳节点，供 agent 重组跨 chunk 多步推理上下文。
- **`include_related` 门控**：`_expand_graph_neighbors_filtered` 在 1-hop 邻居基础上
  按"邻居与查询向量相似度 ≥ 0.5"二次过滤，避免弱链（BootstrapBlazor 组件文档簇）
  把无关噪声带进 recall pack。

#### 其它

- `e2e_smoke.py` 适配 V5.2.21 的 body-driven `/working/set` 签名（旧测试还在用
  query-params）。48 测试中 47 业务通过（唯一失败是容器内无 docker CLI 检查容器状态，
  属环境限制）。

**坑**：graph_khop 里 SQL 混用 Python `% (placeholders)` 格式化与 psycopg2 `%s`
会因 format 参数不足报 `TypeError: not enough arguments`——统一用 f-string
注入 placeholders，保留 `%s` 作为 psycopg2 参数。

---

## [5.3.8] - 2026-08-02

### 本地 connector 黑盒测试（扩大端到端范围）+ 修复 2 个 connector↔server 契约 bug

新增 `connector/hermes/tests/test_cortex_backend_blackbox.py`（**28 测试**）：
不只测 memory_tool 的 8 个动作，而是把本地 `tools.cortex_memory_backend` 的
**backend 层公开方法**对**远程 cortex 服务端**黑盒调用并断言返回形状。覆盖：
health/stats/metrics/recommend、search/search_hybrid/search_tts/reason_with_memory、
graph_khop/graph_mine、relations/links、working memory、procedures、audit、
hierarchy、media/resource、forgetting/storage/rumination、compress/consolidate、
core profile、deep_context。

黑盒测试**抓到 3 个 connector↔server 契约 bug**（此前只在真实会话静默失效）：

1. **`create_hierarchy_node` 读取错误返回键**：connector 读 `r.get('id')`，但
   服务端 `/hierarchy` 返回 `{"node_id": ...}` → 每次调用静默返回 None。
   改为 `node_id or id`。
2. **backend 缺 `recommend_endpoints`**：`memory_tool._memory_dynamic_schema`
   （Direction A 动态 schema）调用它，但本地 backend 没有该方法 → 动态 schema
   每次回退静态。补上（GET /endpoints/recommend/{scenario}）。
3. **`log_audit` 恒返回 0**：服务端 `/audit/log` 返回 `{"status":"logged"}`
   （audit 行 id 是 SERIAL，不回显），connector 读 `r.get('audit_id',0)` → 永远 0。
   原测试用 `is not None` 断言掩盖了它（0 也非 None）。改为 `status:"logged"` → 1；
   黑盒测试收紧为 `assert aid`（真值）以真正抓回这类契约漂移。

另修正 2 个测试假设：`get_relations` 返回 List（非 dict）；`add_resource` 用
`url`（非 `content`）。

**验证**：黑盒 28 测试全绿（修复前 5 失败）；与原 `test_memory_tool_e2e.py`
（17 测试）合计 45 全绿。

**坑**：黑盒测试跑的是**本地** `tools/cortex_memory_backend.py`（不是 repo 的），
改 connector 服务端契约时两边都要修（repo 是源头，本机实际加载的是 tools/ 那份）。

---

## [5.3.7] - 2026-08-02

### connector 测试固化 + README 同步说明

- 新增 `connector/hermes/tests/test_memory_tool_e2e.py`：端到端 8 动作测试
  （add/replace/remove/batch + recall/save_procedure/get_procedure/deep_context/
  audit_query + registry/schema + 上游读安全不变量）。用 hermes venv 直接跑，
  monkeypatch `get_memory_dir` 隔离到临时目录。**17 用例全绿**。
  （`--import-mode=importlib` 避免 repo 根 `__init__.py` 被当包导入。）
- `connector/hermes/README.md` 重写：记录本机 hermes（`A:\...\hermes-agent`）
  与 repo 的同步方式、VM 只跑服务端不跑 connector、8 动作清单、跑测试命令、
  revert 方法、`get_memory_dir` 不读 env 的坑。

**坑**：pytest 在 Windows 下用 MSYS `/e/...` 路径找不到文件，要用 Windows 路径；
`--import-mode=importlib` 是必须的（memory-cortex 根有 `__init__.py`）。

---

## [5.3.6] - 2026-08-02

### 补回 hermes upstream 的磁盘读安全不变量（修复 3 个 pytest 失败）

升级到 recall 版 memory_tool 后，运行 hermes 自带的 `tests/tools/test_memory_tool.py`
发现 `TestUnreadableFileDoesNotWipeMemory` 3 个失败——recall 版是从较早的
hermes 快照衍生的，**丢失了上游后续加的磁盘读安全加固**：

- 文件存在但损坏（invalid UTF-8 / IO error）时，`add`/`replace`/`remove`
  必须**拒绝写入并保留原字节**，而不是当"空 store"覆盖掉（会清空用户记忆）。
- 漂移检测必须**复用同一次已读的 raw**（一次变更 = 一次读），避免第二次读失败
  被当成"无漂移"从而从旧视图重写。

修复（保持 recall 版结构，只补安全不变量）：
- 新加 `_read_raw_checked(path)->(raw, read_ok)` / `_read_entries_checked` /
  `_parse_entries`：`read_ok=False` 仅当文件存在但读不了（invalid UTF-8 也算）。
- `_reload_target` 用一次 `_read_raw_checked` 读 raw，把同一 raw 传给
  `_detect_external_drift(raw)` 与解析（单读不变量）；读不到则抛
  `_UnreadableMemoryError`。
- `add`/`replace`/`remove`/`apply_batch` 捕获该异常 → 返回 `_read_failed_error`，
  拒绝写入、不动磁盘。
- `_detect_external_drift(target, raw)` 改为接收已读 raw，不再自行重读。
- `_read_file` 保留给只读调用者（load_from_disk）降级为 []。

**验证**：hermes 自带 pytest `tests/tools/test_memory_tool.py`（37 测试）全绿——
此前 34/37（3 个读安全失败），修复后全过；含 `add`/`replace`/`remove` 在损坏文件上
拒绝且字节保留、`replace` 恰好读一次。另 `test_memory_tool_schema.py` +
`test_memory_tool_import_fallback.py` + agent 记忆测试 45 全绿；recall（cortex
混合+图谱）功能测试仍通过。

**坑**：ad-hoc 验证 memory 工具时，`get_memory_dir()` 用 `get_hermes_home()/memories`
不读 `HERMES_MEMORY_DIR` 环境变量——隔离测试要 monkeypatch `get_memory_dir`（pytest
fixture 就是这样做的），否则会写到真实 hermes 记忆目录。

---

## [5.3.5] - 2026-08-02

### connector memory_tool 升级到 agentic recall 版本 + 修复 4 个潜在生产 bug

本机 hermes 的 `tools/memory_tool.py` 是独立旧实现（MemoryStore，action 仅
add/replace/remove，无 recall）。替换为 repo connector 的 V5.3 recall 版本
（action 扩展为 8 个：add/replace/remove/**recall/save_procedure/get_procedure/
deep_context/audit_query**，含主动回忆注入 + 动态 schema）。

替换过程发现并修复 **4 个潜在生产 bug**（此前 memory_tool() 从未被端到端
触发，故未暴露）：

1. **缩进破坏 → UnboundLocalError: result**：approval gate + 整个单动作分发块
   （add/replace/remove/recall/.../else）被错误嵌套在 `if action == "remove"`
   内部（8 空格），导致一切非 remove 动作落到 `return json.dumps(result)` 时
   `result` 未定义。整体 dedent 回函数体级（4 空格）。
2. **`self._importance_for_tier` 不存在**：`_add_to_cortex` 调方法版，但
   `_importance_for_tier` 是模块级函数 → AttributeError。改模块级调用。
3. **`save_procedure` 目标名冲突**：schema 说"name via target"，但前置的
   target 校验（限 memory/user）把自定义 procedure 名挡掉。改为仅对
   `save_procedure` 放开（其余动作仍强制 memory/user）。
4. **本地 backend 缺 `auto_link_new_chunk`**：新 memory_tool 每次写 chunk 会
   auto-link（Direction 2），但本机 `cortex_memory_backend.py` 是独立版本，
   没有该方法 → AttributeError。补上（自包含，用 search_hybrid+create_link）。

**验证**（hermes venv + live cortex 8.2）：8 个 action 全绿——ADD/REPLACE/
REMOVE/BATCH（cortex write hooks 触发，audit 记录）、RECALL（混合+图谱k-hop
hits）、SAVE_PROCEDURE→GET_PROCEDURE（往返可查）、DEEP_CONTEXT、AUDIT_QUERY、
未知 action 正确报错。target 校验对 save_procedure 放开、对 add 仍强制。

**坑**：
- 本机 hermes `tools/memory_tool.py` 是 upstream 未修改（git clean），
  `cortex_memory_backend.py` 是 untracked 本地添加——两者独立定制，改 connector
  要分别 patch，别用整文件覆盖（有互相不认识的方法）。
- `memory_tool()` 这类工具函数必须 end-to-end 跑一次才知道缩进/引用是否真对，
  只看 ast.parse 不够。

---

## [5.3.4] - 2026-08-02

### 修复：/search 无视 limit（始终返回 10）

connector 配合验证时发现：V5.3 RRF 重写把 candidates + 图谱邻居拼进 results
后**没有收敛回 `req.limit`**，导致 `POST /search?limit=3` 和 `GET /search/hybrid?limit=3`
都返回 ≥10 条（connector 传的 top_k=3 形同虚设）。

修复：`search_memory` 在 composite 重排后 `results = results[:req.limit]`。
connector 的 `search(limit=n)` / `search_hybrid(top_k=n)` 现在真正返回 n 条。
实测 limit=3→3、limit=5→5。

**教训**：功能验证别只看 HTTP 200，要断言 `limit` 等参数真被尊重——RRF 融合
天然会扩展候选/邻居，收敛到请求大小是最后一步，最容易漏。

---

## [5.3.3] - 2026-08-02

### connector 配合：让服务端检索/图谱增强真正对 agent 生效（hermes ↔ cortex）

服务端 V5.3.0-5.3.2 的三路 RRF + 图谱回填 + k-hop 已可用，但 connector 侧
（`tools/cortex_memory_backend.py` + `memory_tool.py`）的旧调用把新能力挡在门外。
本次补齐 connector 层，让 hermes agent 实际受益：

- **`CortexMemoryBackend.search()` 默认 `include_related=True`**：正常 search 现会
  沿图谱邻居扩展并做相关度门控（服务端 `_expand_graph_neighbors_filtered`），
  recall 不再只是单点语义相似。`reason_with_memory` / session 启动主动召回自动继承。
- **`reason_with_memory()` 集成 GraphRAG k-hop**：新增 `khop` 参数（默认 2），对 top 命中
  调 `/graph/khop` 做多跳子图遍历，跨 chunk 多步推理节点并入 `context_chunks`，
  返回新增 `graph_subgraph` 字段。此前多跳能力仅暴露 HTTP 端点，agent 用不上。
- **`memory_tool` recall action**：`graph_context` 之外新增 `graph_khop` 字段透传
  多跳子图；返回 `graph_khop_nodes` 计数，agent 可感知跨 chunk 推理上下文。
- **`search_hybrid()` 修复参数名错配**：服务端 `/search/hybrid` 参数是 `limit`，
  旧代码传 `top_k` 被 FastAPI 忽略、静默回退 limit=10。现显式传 `limit`。
- **`CortexMemoryBackend.graph_mine()`**：封装启发式图谱回填，接入 weekly 深度维护
  （`cortex_maintenance.py weekly()`，7b 步）让图谱随 chunks 累积持续生长。

**验证**：三文件语法 OK；backend 模块干净导入，新方法齐全；服务端契约
（include_related 扩展、/graph/khop nodes、/graph/mine 回填 1948 relations、
search_hybrid limit 修复）全部 200 通过。

**坑**：connector 有两套实现（`cortex_api.py` SDK 与 `cortex_memory_backend.py`），
hermes 实际用后者（`memory_tool` 里 `from tools import cortex_memory_backend as _cmb`）；
改 connector 前先确认走的是哪个，别改错文件。

---

## [5.3.2] - 2026-08-02

### 排序定型：字面精确命中 sim floor + RRF 主导

在 8.2 实测中发现 V5.3.0/5.3.1 的 composite 仍被**向量相似度主导**，导致：
- 用户搜 `pyenvs virtualenv`，含 "pyenvs" 的字面精确命中的记忆（#本机 pyenvs 目录）
  反而排在**不相关的** BootstrapBlazor 组件文档（纯向量近似）之后。

根因：字面命中的记录往往**没进向量通道候选**（`sim_by_id` 缺失 → sim=0），
而 `composite` 里 `0.65×sim` 让它们系统性垫底。

修复：
```python
composite = 0.6 * rrf_norm + 0.4 * sim_effective + 0.05*(imp/10) + 0.1*recency + 0.1*relevance
sim_effective = max(sim, 0.5) if lex_hit else sim   # 字面精确 → sim 至少 0.5
```
- **rrf_norm 权重提升为主（0.6）**：多路都能命中 → 晋升
- **字面命中的 sim floor = 0.5**：专有名词/路径记忆（字面精确但向量没进候选）
  不再被 sim=0 低估——查询词一字不差出现在内容里本身代表相当相关性

实证平衡（离线 + 线上 8.2 均验证）：
- 字面精确(sim0) > 不相关语义近似(sim0.5)  → pyenvs 排到 BootstrapBlazor 前 ✅
- 而 sim0.8+ 的真相关仍主导，字面 floor 不失控 ✅

**坑**：字面加成不能做成固定 bonus（如 +0.25）——会压过 sim0.9 的高相关真命中；
必须做成"sim 下限"(floor) 而非加分，让语义相关度保持同量级竞争。

---

## [5.3.1] - 2026-08-02

### 排序修正：把 RRF 多路命中信号引入 composite

V5.3.0 首版 `composite` 是 `1.0×similarity`（纯向量），RRF 分没参与最终排序，
导致字面精确命中/多路命中相对纯向量近似**没有排序优势**。

修复：`composite = 0.65×sim + 0.35×rrf_norm_old`，把 RRF 分（编码"多路都能命中"）
归一化后接入排序。经边界验证调整权重，避免：
- 权重过高 → 双路 sim0.5 压过 sim0.85 真相关（过度）
- 权重过低 → 双路 vs 单路的 sim 微弱差也体现不出 RRF 优势

→ 后经 V5.3.2 进一步定型为 `0.6×rrf_norm + 0.4×sim_effective`。

---

## [5.2.23] - 2026-08-01

### 修复 procedural memory 创建 + 搜索 (D5 收尾)

V5.2.22 的 D5 接线有两个服务端缺口，导致 `search_procedures` 一直返回 0 条：

- `ProceduralCreate` 模型只接受 `trigger_pattern`/`action_recipe`，但 connector 实际发
  `{name, trigger, action, description}`——所以 trigger/action 被丢弃，存的 description
  退化成仅 name，ILIKE 搜索匹配不到。新增 `trigger`/`action` 字段 + `_effective_description()`。
- `GET /procedural/search` 只接受 `?q=`，但 connector 发 `?trigger=`。现在两者都接受，
  WHERE 子句用有效 needle（原来用恒空的 q）。

现在 `learn_procedure → search_procedures` 往返可查。

**坑**：`crontab -l` 在管道子 shell 里空输出会导致 `(crontab -l; echo ...) | crontab -`
静默装进一个**空 crontab**。一律用 临时文件 + `sudo crontab -u <user> /tmp/file` 安装，
再用 `sudo crontab -u <user> -l` 验证。
（详见 skill `cortex-fix-deploy` 的 "Scheduled memory maintenance" 节。）

---

## [5.2.22] - 2026-08-01

### Agentic recall + 图谱挖掘 + procedural memory (D1/D2/D3/D4/D5)

agent 记忆体的 5 个加强方向全部落地（详见 docs/Agent-记忆体集成.md）：

- **D1 recall 回忆包**：memory_tool 新增 `recall` action——hybrid search + 图谱推理，
  返回只含相关簇的聚焦 recall pack，替代"全量 dump 所有 entries"（省 token + 提相关度）。
- **D2 关联挖掘**：**修复从没工作过的 auto-link 钩子**。旧钩子调
  `backend.search(content, top_k=3, min_score=0.7)` 不匹配真实签名、还从 list 上读
  `.get("chunks")`——每次都抛异常被吞，所以**图谱从未生长**（1953 chunks 仅 110 links）。
  新 `auto_link_new_chunk()` 用 hybrid search + 相似度门控；实测一次写入 0→3 links。
- **D3 主动回忆注入**：`_load_from_cortex` 不再丢弃 `reason_with_memory()` 结果，
  把 top hits 作为 "Memory Recall Context" 块追加到 system-prompt snapshot。agent
  启动即带跨会话的主动召回上下文，而非只靠完整 MEMORY/USER dump。
- **D4 引导遗忘**：新增 `POST /forgetting/guided`——读 access_stats(30 天窗口)，
  对 ≥3 次召回记录的 short/medium chunk 提升到下一 tier 并续 TTL（再巩固）。
  permanent (expires_at NULL) 不受影响。
- **D5 procedural memory**：memory_tool 新增 `save_procedure`（存可复用流程：
  trigger 走 old_text、action 走 content、name 走 target）与 `get_procedure`
  （按任务描述取最佳匹配流程）。backend 的 `learn_procedure` 之前从未暴露为
  agent action。

---

## [5.2.21] - 2026-08-01

### 启用跨轮对话记忆 (Direction B 收尾)

`POST /working/{sid}/append` 的 "B path"（role/content）此前 501 stub：`memory_conversations`
表从未在 schema.sql 里声明，`GET /working/{sid}/context` 返回写死的空 conversation。
于是 working memory 的"对话半区"完全缺失——session 能存 scratch KV，却无法复述自己的
跨轮对话。

- schema.sql：新增 `memory_conversations(session_id, seq, role, content, created_at)`
  + `(session_id, seq)` 索引。
- `working_append`：实现 `{role, content}` 路径——插入一轮 turn，seq = `MAX(seq)+1`
  保证稳定排序。
- `working_context`：返回持久化的 conversation（`ORDER BY seq`）与 working_memory 融合，
  替代硬编码 `[]`。

现在 hermes 可按 session 持久化 + 恢复 LLM/assistant 对话，打通 Direction B 的跨轮连续性。

---

## [5.2.20] - 2026-08-01

### Never-forget: permanent 降级后永久保留

V5.2.19 重算 tier+TTL 时有 gap：已是 permanent（expires_at NULL, imp≥8)的记录被降级到
较低 importance 时会得到一个新的短 TTL——在 vaulted 记忆上"复活"一个过期时间。
语义 bug：permanent 意为永远。

**修复**：一旦 expires_at 为 NULL（permanent），再降到任意非 permanent importance 都
保持 NULL——降级绝不复活 TTL。两个值都有限时取较晚者（绝不缩短）。

---

## [5.2.19] - 2026-08-01

### 修复 update_chunk 的 tier/TTL 不一致 (三层记忆完整性)

完整三层生命周期边界测试发现：`create_chunk` 对 importance∈{3,4,5,6,7,8} 正确设置
tier+expires_at；但 `update_chunk`（POST /chunks/{id}）只更新 importance，**从不重算
tier 或 expires_at**。于是把 chunk 从 short 升到 medium（importance 3→5）后仍是
tier=short_term + 7 天 TTL。

- 抽出共享 `_tier_for_importance()` helper（importance→(tier, expires_at) 唯一真源）。
- `create_chunk` 改调它（行为不变，DRY）。
- `update_chunk` 重算 tier+expires_at，且**只升不缩**：新 importance 蕴含的 TTL 若早于
  现有 expires_at 则保留现有（更晚）；升级到 permanent 清空 expires_at。

---

## [5.2.18] - 2026-08-01

### 修复 forgetting/cleanup 500（score_before/score_after 列不存在）

V5.2.17 开始让 create 设置 expires_at，终于使 `/forgetting/cleanup` 能真找到过期 chunk。
但 cleanup 对 forgetting_log 的 INSERT 引用了 schema 里不存在的 `score_before`/`score_after`
列（真实列：id, memory_id, action, reason, created_at）。于是第一次真正的清理运行就 500。

**修复**：从 INSERT 去掉这两个不存在的列。cleanup 现在能正确删除过期 chunk 并记录剔除。

---

## [5.2.17] - 2026-08-01

### Cortex forgetting 系统真正开始工作

- `create_chunk()` 从 importance 推导 tier + expires_at：
  - importance 1-3 → tier=short_term, expires_at = now + 7d
  - importance 4-5 → tier=medium_term, expires_at = now + 30d
  - importance 6-7 → tier=long_term, expires_at = now + 180d
  - importance 8-10 → tier=long_term, expires_at = NULL（permanent，永不过期）
  之前 expires_at 永远 NULL，`/forgetting/cleanup` 的 `WHERE expires_at < NOW()` 永无命中。
- `/forgetting/stats` 用真实 tier 列 `GROUP BY tier` 计算各层计数（之前写死
  `{long:0, med:0, short:total}`）。

配合 V5.2.16 的 connector tier→importance 映射，三层生命周期（short→medium→long→
permanent）端到端可用：hermes 用 tier=short 写会拿到 7 天 TTL，cleanup 真正能删。

---

## [5.2.16] - 2026-08-01

### Direction A/B/C — hermes<->cortex 集成改进

- **Direction A (dynamic schema)**：server `/endpoints/recommend` 新支持
  `connector_memory` scenario，返回 actions 映射；backend 新增 `recommend_endpoints()`
  自动发现；memory_tool 用 `dynamic_schema_overrides` 把 live cortex 能力注入
  LLM 可见的工具描述 + action enum（30s 缓存，cortex 不可达时回退静态 schema）。
- **Direction B (双向工作记忆)**：`_load_from_cortex` 读 `get_all_working_memory`
  恢复跨轮 scratch 状态，并写 session_anchor 使其可被发现。
- **Direction C (三层记忆)**：memory_tool 接受 tier 参数
  (short/medium/long/permanent → importance 3/5/7/9)；MEMORY_SCHEMA 声明 tier enum；
  boot 时触发 `run_forgetting_cleanup`（≤1/30min）清理 short-tier。

---

## [5.2.7] - 2026-07-28

### 删 `_docker-images/` + `scripts/`

- `_docker-images/`: 删. 只剩 1 个 README.md (1245 字节), 引用了已删的 `-clean` 子目录名.
  `.tar` 文件全部 gitignored (从未实际存在过), save/load 跨机器迁移脚本也已废 —
  V5.2.4 hot-reload 工作流永远从 Dockerfile 重建, 不需要 tar 传输.
- `scripts/`: 整目录删 (2 文件: save-docker-images.sh + load-docker-images.sh).
  V5.1 era 的跨机器传输工具, 现已被 V5.2.4 hot-reload (Dockerfile + bind-mount) 取代.

### 删后状态

- 仓库根目录仅剩 8 项: `.dockerignore`, `.env.example`, `.git/`, `.githooks/`,
  `.gitignore`, `.gitlab-ci.yml`, `.pre-commit-config.yaml`, `CHANGELOG.md`,
  `README.md`, `VERSION`
- 跟踪的文件数: 46 → **43** (-3)
- `docker load -i` 跨机器传输工作流: 删除. 替代方案: 拷贝 Dockerfile 到目标机,
  `docker build -t memory-cortex-stack:5.2.0 -f server/docker/unified/Dockerfile .`,
  再运行 `run_docker_stack_unified.sh`.

---

## [5.2.6] - 2026-07-28

### 目录清理 (cleanup)

- 移除 66 个 V4/V3 时代遗留文件 (sqlite/chroma 老代码、已合并的迁移脚本、过时的 docker-compose 配置)
- 单一源代码目录落地：仅 `./Example-Agent/memory-cortex/`
- 历史保留于 git，可通过 `git log --follow <file>` 找回

### Deleted (69 files, 21,462 lines)

- 顶层 V4 plugin stack: `__init__.py`, `plugin.yaml`, `plugin_package/`, `patches/`, `examples/`
- 顶层 V3 ops 脚本: `backup.sh`, `check_all_memories.py`, `deploy.sh`, `download_model.sh`,
  `export_memories.py`, `init_history_data.py`, `merge_import.py`,
  `migrate_hermes_memories.py`, `sync_local_memories.py`, `requirements.txt`
- `scripts/` 下 V3 ops (保留 save/load-docker-images.sh) → V5.2.7 全删
- `docs/` V2/V3 设计文档 + 旧 05-实施规划 + optimization/
- `server/src/` V4 sqlite/chroma 业务代码 (api.py, chroma_store.py, dashboard.py,
  hermes_memory.py, metadata_store.py, plugin.py, storage_budget.py, taxonomy.py,
  hooks/, memory_*.py)
- `server/scripts/` V5.0 split + 迁移脚本 (已完成的迁移)
- `server/docker/` V5.0 split Dockerfile + 4 旧 docker-compose 变体 + postgres-init/
- `server/docker/unified/e2e_smoke.py` (与 server/tests/e2e_smoke.py 完全重复)
- `server/tests/smoke.py` (legacy, e2e_smoke.py 覆盖)
### Kept (active V5 stack)

- `server/src/api_pg.py`, `config.py`, `__init__.py` — V5 主代码
- `server/src/mcp_server/cortex_mcp.py`, `sdk/cortex_client.py` — 备用 MCP 入口
- `server/src/sdk/openapi_baseline.json` — tests 用
- `server/docker/unified/{Dockerfile,entrypoint.sh,schema.sql,HOT-RELOAD.md}` — V5 单容器运行时
- `server/scripts/run_docker_stack_unified.sh` — V5 启动器
- `server/tests/e2e_smoke.py` — 48 测试
- `connector/{hermes,opencode}/` — 出口适配器参考实现
- 全部 git/CI 配置 (`.gitlab-ci.yml`, `.pre-commit-config.yaml`, `.githooks/`)

---

## [Unreleased] - 2026-07-04

### 集成交付 — agent builtin memory 让 cortex 完全接管

**位置**: [`backups/2026-07-04-cortex-takeover/`](./backups/2026-07-04-cortex-takeover/README.md)

把 hermes-agent builtin `MemoryStore`(tools/memory_tool.py)从写 `~/.hermes/memories/{MEMORY,USER}.md`,改成完全走 cortex HTTP backend。完整实施包 = README + 1 个新模块 (`cortex_memory_backend.py`) + 2 个 diff (`memory_tool.py.diff` / `test_memory_tool.py.diff`),其他 agent 可 `cp -r` 整目录过去直接 apply。

**验证**: 6/6 PASS — `use_cortex=True` / `load_from_disk 87+80 条` / `.md 永不创建` / char_limit 行为正确 / 83 个 hermes 自带 test 通过 / 重启后真 LLM 触发接管。

**配套 plugin 改动**: `plugin_package/memory_cortex/__init__.py` 完整实装 19 方法已交付(见 `backups/2026-07-04-full-provider-impl/`)。

---

## [4.2.0] - 2026-06-20

### 新增功能

#### 改进三: LLM-as-Judge 智能评估
- `src/hooks/02_memory_evaluation.py` 新增 `_llm_judge_score()` 函数
- 对 `relevance_score` 灰色区间 (0.3-0.7) 异步调用轻量 LLM 二次评估
- 综合算法：60% LLM + 40% regex
- `feedback` payload 加 `judge_method: "regex" | "llm_judge"` 标记
- 默认关闭 (`LLM_LLM_JUDGE_ENABLED=false`)，需要手动启用

**配置**:
- `LLM_LLM_JUDGE_ENABLED`: 是否启用 (默认 false)
- `LLM_LLM_JUDGE_URL`: llama-server OpenAI 兼容地址 (默认 http://localhost:8080/v1)
- `LLM_LLM_JUDGE_MODEL`: 模型名 (默认 MiniMax2-M3)

#### 改进五: 层级标签 Taxonomy
- 新增 `src/taxonomy.py` 模块
- SQLite `tags_hierarchy` 表 (child_tag, parent_tag, level)
- 父标签命中时子标签 `relevance_score` × 1.2 加权
- 5 个 API 端点:
  - `POST /taxonomy/relations` 添加层级
  - `DELETE /taxonomy/relations/{child}` 删除
  - `GET /taxonomy/relations` 列出所有
  - `GET /taxonomy/ancestors/{child}` 递归找祖先
  - `GET /taxonomy/children/{parent}` 找子标签

#### 改进六: 存储预算管理 + 主动遗忘
- 新增 `src/storage_budget.py` 模块
- `StorageBudgetService` 类: 监控 + 主动遗忘
- 3 个 API 端点:
  - `GET /storage/health` 容量状态 + 建议
  - `POST /storage/enforce` 强制检查 + 主动遗忘
  - `POST /storage/evict` 手动触发淘汰 (ratio 0.01-0.5)
- 保护规则: `tier="long_term"` 或 `importance >= 8` 不淘汰
- 淘汰策略: 按 `relevance_score` 升序，淘汰末位 5%

### 修改

- `src/api.py`: +6 个 V4.2 端点 + 新增 `StorageBudgetService` 和 `TaxonomyService` 实例
- `src/chroma_store.py`: 暴露 `persist_dir` 属性供存储预算模块计算磁盘占用
- `VERSION`: 4.1.0 → 4.2.0

### 端到端验证 (2026-06-20 16:56)

| 测试 | 结果 |
|------|------|
| `GET /storage/health` | ✅ 1318/50000 (2.64%), healthy |
| `POST /taxonomy/relations` (4 条) | ✅ 创建 4 条层级关系 |
| `GET /taxonomy/ancestors/fastapi` | ✅ 返回 `[python, backend]` |
| `POST /storage/evict?ratio=0.01` | ✅ protected 1259/1305 |
| LLM judge 函数 (Python 调用) | ✅ 返回 1.0 |
| `POST /eval/feedback` 接受 `judge_method` | ✅ updated: 1 |

---

## [4.1.0] - 2026-06-20

### 新增功能

#### CLI/Gateway 记忆可视化（用户可见）
- `00_memory_inject.py`：新增 `display` JSON 字段，返回召回摘要
  - 格式：`"📚 记忆召回: 3条 (2条 heuristic / 1条 knowledge)"`
- `turn_context.py`：新增 `display` 字段提取逻辑，调用 `tool_progress_callback` 发送 Gateway
- `00_tool_memory_persist.py`：新增 `display` JSON 字段，返回沉淀摘要
  - 格式：`"✅ 💾 工具经验沉淀: read_file (成功) | 已沉淀为经验"`

#### Post-LLM 经验抽象（Heuristic Extraction）
- `memory_rumination.py`：新增 `abstract_heuristics()` 方法
- `POST /rumination/abstract-heuristics`：从 `experience` 类型 chunk 提取 `heuristic` 规则
- `memory_service.search()`：`type=heuristic` 搜索结果 1.5x 权重提升
- `HeuristicMeta` 模型：`rule_text`/`when`/`action` 字段

### Bug 修复

#### 记忆注入字段名错误（重要）
- `00_memory_inject.py` 返回值从 `{"system_prompt": ...}` 修正为 `{"context": ...}`
- `turn_context.py` 只识别 `context` 字段，原 bug 导致**记忆从未真正注入 LLM**
- 修复后记忆正确注入用户消息

#### PMT `.format()` JSON 占位符误解析
- `HEURISTIC_PROMPT` 中的 JSON 示例 `{rule_text}` 被 Python `.format()` 误识别
- 修复：`{field}` → `{{field}}`（所有 JSON 示例字段双花括号 escape）

### 技术细节
- `model_tools._emit_post_tool_call_hook`：返回值类型 `None` → `Optional[Dict]`，返回 `display` 字段
- `tool_executor._emit_terminal_post_tool_call`：捕获 hook 返回值并调用 `tool_progress_callback`
- 显示机制：`tool_progress` 模式 `all`（默认开启），显示给 CLI + Telegram 用户

---

## [4.0.0] - 2026-06-20

### 新增功能

#### 双轨迹编码 (Dual-Trace Encoding)
- `MemoryChunk` / `MemoryChunkCreate` 新增 `source_context` 字段
- 向量化前拼接场景：`"在讨论 [QuantPlatform 升级任务] 时，用户确认 P0 拍板"`
- `chroma_store._enrich_content()` 场景拼接
- `chroma_store.add_chunk()` / `reindex()` 使用 enriched text 向量化
- `chroma_store.search()` 返回 `_original_content`（展示用）+ `enriched_content`（调试用）
- `metadata_store.create()` / `delete()` 同步 FTS5 索引

#### 混合检索 RRF 融合
- `metadata_store` 新增 `memory_chunks_fts` FTS5 虚拟表
- `metadata_store.fts5_search()` BM25 关键词检索（SQLite `bm25(table, weight)` 位置参数）
- `memory_service.search()` RRF 融合（K=60）：BM25 rank + 向量 distance rank 加权合并
- FTS5 在 `create()`/`delete()` 时实时同步；存量 1298 条已回填

### 技术细节
- SQLite FTS5 bm25 语法：`bm25(table, weight)` 位置参数，非 named parameter
- bm25 返回负数（越负越相关），RRF 前取反转为正数
- 调试字段（生产可移除）：`_rrf_score`、`_bm25_rank`、`_source_context`

### 改进方向（后续迭代）

| 方向 | 优先级 | 状态 |
|------|--------|------|
| 经验抽象（Heuristic） | 🟠 P1 | 规划 |
| 存储预算管理 | 🟠 P1 | 规划 |
| 层级标签（Taxonomy） | 🟡 P2 | 规划 |
| LLM-as-Judge 评估 | 🟡 P2 | 规划 |

---

## [3.0.0] - 2026-06-20

### 重大变更 (本期重构)

#### 配置层 (R1-B)
- **重构** `src/config.py`:删除未使用的 `Settings(pydantic_settings)` 死代码,统一为 `CortexConfig` 单一配置类
- **修复** `memory_rumination.py:598` 的 `from .config import CortexConfig` ImportError(原代码引用不存在的类名)
- **修复** `api.py` L31 双斜杠硬编码默认值 `"./models//root/.cache/..."` bug
- **严格化** `EMBEDDING_MODEL_PATH` 校验:空值启动时直接抛 `RuntimeError`,强制 .env 配置

#### 服务层 (R1-A, R1-D)
- **同步** hooks 到 `~/.hermes/hooks/`:补齐 v2.1 修复版 `02_memory_evaluation.py`,覆盖旧版 `00_memory_inject`(TIMEOUT 5s→15s)
- **新增** `scripts/watchdog.sh`:端口 + 进程 + HTTP 三重健康检查,失败 3 次自动告警
- **新增** `scripts/backup.sh`:真实路径(原 `~/.hermes_plugin/memory_cortex/` 已不存在)
- **新增** `tests/smoke.py`:5 个核心 API 烟雾测试(health / stats / chunks CRUD / search / dashboard)

#### API 层 (R1-C, R2-H, R3-M)
- **启用** FastAPI 自动 `/docs` (Swagger UI) + `/redoc` + `/openapi.json`
- **统一** 版本号:VERSION 文件作为单一来源,`plugin.yaml` / `app.version` / `health.version` 一致 = "3.0.0"
- **修复** `GET /chunks/{id}` 在 pydantic v2 + 旧 json_encoders 下的 500 错误(重启后自动解决)

#### 文档 (R2-G)
- **重写** `README.md`:从 124 行(路径错乱 + systemdt 误导)→ 280 行(真实部署 + 完整 API 表 40+ 端点 + 架构图)
- **新增** `CHANGELOG.md` (本文件)
- **新增** `.env.example` 真实配置(原 5 行示例与 .env 67 行完全不同)

---

## [2.1] - 2026-06-20 (修复补丁)

### 修复

- **P0** `metadata_store._init_db()`: `duplicate column name: tier` 错误(SQLite 3.45.1 不支持 `ADD COLUMN IF NOT EXISTS`,改 try/except)
- **P0** `api.py`: `uvicorn reload=True` 硬编码导致 180s 启动超时,改为环境变量
- **P0** `/forgetting/stats` 500: `MemoryForgettingService` 缺 `get_forgetting_stats()` 方法
- **P0** `AUTO_FORGING_ENABLED` 拼写错误 → `AUTO_FORGETTING_ENABLED`
- **P1** `00_memory_inject.py` TIMEOUT 5s → 15s(Chroma + embedding 负载高时超时)
- **P1** 注入失败静默丢弃 → 写日志到 `~/.hermes/logs/memory_inject.log`
- **P1** tool_experience 硬丢弃 → 场景过滤(debugging 上下文才召回)
- **P1** `relevance_score` 外部访问驱动 → 新增 `02_memory_evaluation.py` 自评回路
- **P2** short-cmd(`cd x` 等)相似度仅 0.26 → query 扩展 + 阈值降到 0.35

### 详细记录

- `references/2026-05-01-embedding-fix-complete-record.md`
- `references/embedding-null-fix.md`
- `references/chroma-sqlite-sync-accident-20260526.md`

---

## [2.0] - 2026-04 ~ 2026-05

### 新增

- 三层记忆 (短/中/长) + 艾宾浩斯遗忘曲线
- 记忆再巩固 (reconsolidation):关联、合并、升级
- 记忆可视化看板 (HTML dashboard)
- 反刍机制:session 回顾 + 矛盾检测 + 碎片合并

### 模型迁移

- `paraphrase-multilingual-MiniLM-L12-v2` (384 维)
  → `paraphrase-multilingual-mpnet-base-v2` (768 维)
- 中文技术术语召回距离: 0.40 → 0.27 (显著提升)
- 迁移步骤: `references/embedding-migration-mpnet.md`

---

## [1.0] - 2026-04

- 初始版本:CRUD + Chroma + SQLite 双存储
- 兼容 Hermes 原生记忆协议
- 语义搜索 + 标签/类型/重要性过滤

---

## 待办 (Backlog)

> 以下功能在 docs/Memory-Cortex-大脑皮层设计文档.md 中提及但**未实现或正在重做**:
> - V2.2:计划中,具体功能待定
> - V2.3:计划中,具体功能待定
> - V3.0 原计划的知识图谱/推理/共享功能:已删除(2026-06-20),实际数据 0 调用

任何新功能开发前,先更新本文档 + VERSION,再写代码。
## [6.3.1] - 2026-08-18 (connector ↔ cortex version alignment + hermes internal patches)

按 user 决策: 把 connector 版本对齐到 cortex server 当前版本 (6.3.1),
并把所有 hermes 端已部署的补丁回写到 connector 仓, 让
`.connector\hermes\` 重新成为
单一 source of truth. 同时明确双写策略: **cortex 是 sole source of
truth, 本地 `session_summaries` 表是 fast cache**.

**版本对齐**:
- `connector/VERSION` 从无到 `6.3.1` (跟 `/opt/cortex-deploy` 跑的服务端一致)
- `connector/hermes/README.md` 头部 `Current version` 从 V5.3.7 → V6.3.1
- `connector/hermes/cortex_memory_backend.py` 内 V5.3.0+/V5.3.2/... 注释保留
  (历史 footprint), 但 module docstring + upsert_session_conflict 强化为 V6.3.1
- `connector/hermes/session_summarizer.py` docstring 标注 V6.3.1 + 双写策略

**Hermes 内部 patches** (第一次正式落地, 历史 README 只说"apply by hand"):
1. **`hermes_state_common.py`**: `session_summaries` 表 + 2 个索引
   (idx_session_summaries_session, idx_session_summaries_user_created)
   - 表 schema 跟 cortex 端的 session_summary chunks **不冲突**
     (本地是关系表, cortex 是 vector store)
   - `UNIQUE(session_id, source)` 保证同 source 重写不重复
2. **`hermes_state.py` SessionDB 新增 3 个方法**:
   - `upsert_session_summary(session_id, summary_text, short, parsed, source, trigger)`
     → `Optional[int]` (FK race 返 None)
   - `update_session_summary_cortex_id(row_id, cortex_chunk_id)`
     → cortex 写成功后回填本地 cache
   - `list_session_summaries(user_id, limit=5)`
     → 下个 session 首轮注入用, 按 created_at DESC 排序
3. **`hermes_state.py` SessionDB 内部 hooks**:
   - `end_session()` 末尾: fire-and-forget `summarize_session_async(self, session_id, source="end")`
   - `append_message()` 末尾: `_maybe_progressive_summary(session_id)` 检查 assistant
     消息数是否到 PROGRESSIVE_THRESHOLD (默认 50) 倍数
4. **`agent/conversation_loop.py` `_restore_or_build_system_prompt`**
   首轮 fresh-build 之前: 拉最近 N=5 条 prior summaries 拼进 system_message,
   让 builder 自然吸收 (continuation 路径不动, prefix cache 不命中只在首轮)

完整 diff 在 connector 仓新文件 `HERMES_INTERNAL_PATCHES.md`.

**双写策略 - cortex 是 sole source of truth**:
- 写顺序: cortex 先 (`upsert_session_conflict` → 返回 chunk_id)
  → 本地表再 (`upsert_session_summary` + `update_session_summary_cortex_id`)
- cortex 失败: 本地 cache 跳过更新, **不假装成功**, 调用方看到 cortex_id=None
- 本地失败: cortex 已经写了, 调用方不感知 (本地 cache miss 是允许的)
- 读取: 优先用本地 cache (fast index), cache miss 时回退 cortex search
  (`/list?source=hermes-session-summarizer&target={user_id}`)
- TTL/tier: `SUMMARY_IMPORTANCE=7` 自动 long_term (180d), 跟 server V6.1.0
  governance 一致

**Source 标签强化 (V6.1.0 governance 对接)**:
- `upsert_session_conflict` 的 `source` 参数固定传 `"hermes-session-summarizer"`
- 服务端 `/stats/by-source` 已经按这个标签分组 (见 server OpenAPI 注释)
- 之前的 `source="user"` 是错的, 治理后改为 `source="hermes-session-summarizer"`
  + `user_id` 字段保留作 per-user 隔离 (KB per-user isolation, V5.3.9+ 强制)

**install_session_summarizer.py 默认路径修正**:
- 默认 `HERMES_TOOLS_DIR` 从 `A:\Application\Hermes-Agent\Program\hermes-agent\tools`
  (过时, 不是运行实例) → `A:\Application\Hermes-Agent\UserData\hermes-agent\tools`
  (本机真实跑 hermes 的 venv 所在)
- 仍支持 `HERMES_TOOLS_DIR` env override

**端到端验证** (本机 hermes venv):
- `test_memory_tool_e2e.py`: 17/17 PASS
- `test_cortex_backend_blackbox.py`: 28/28 PASS (V6.3.1 server 端点全兼容)
- `test_session_summarizer_blackbox.py`: 10 PASS + 2 SKIP (LM Studio 9b 不可达时预期 skip)
- SessionDB smoke (FK race, UPSERT idempotent, list 排序): 7/7 PASS
- end_session 钩子 smoke (async fire, threshold gate, kill switch): 3/3 PASS

**回滚**:
```bash
cd /a/Application/Hermes-Agent/UserData/hermes-agent
git checkout -- hermes_state.py hermes_state_common.py agent/conversation_loop.py tools/memory_tool.py
rm tools/cortex_memory_backend.py tools/session_summarizer.py
# state.db 的 session_summaries 表留下无害 (无引用)
```

---

## [6.3.x] - 2026-08-17 (MCP server: streamable-http via fastmcp 3.x)

**MCP 部署** (server/src/mcp_server/cortex_mcp_server.py):
- 容器 `memory-cortex-stack:5.3.9` 实际跑的是 fastmcp 3.4.7,但旧 cortex_mcp_server.py 是 2.x 风格(手写 _MCPASGIBridge + StreamableHTTPSessionManager + _session_manager 属性)→ 启动失败 `AttributeError: 'FastMCP' object has no attribute '_session_manager'`, /health 502, /mcp 全 000.
- 重构: 用 fastmcp 3.x 的 `mcp.http_app(path='/', transport='streamable-http')` 返回 StarletteWithLifespan,`app.mount('/mcp', sub_app)` 挂载. 删了手写 bridge.
- Cached `_MCP_HTTP_APP` + `_MCP_HTTP_LIFESPAN` module-level singleton,避免重复 `http_app()` 调用导致多个 session manager.
- `get_mcp_lifespan()` 现在返回 `sub_app.router.lifespan_context` (fastmcp 3.x 真实 lifespan),被 api_pg.py 的 `_combined_lifespan` 调.
- Tool handler 重写以匹配实际 SDK 方法:
  - `_CORTEX_CLIENT.remember()` (type, importance, tags) — 不是 `create_chunk()`/`chunk_type=`
  - `_CORTEX_CLIENT.recall(query, top_k=...)` — 不是 `search()`,且不支持 `min_importance` kwarg
  - `_CORTEX_CLIENT.forget(chunk_id)` — 不是 `delete_chunk()`
  - `_CORTEX_CLIENT.introspect()` — 不是 `health()`
  - `_CORTEX_CLIENT.get_chunk(chunk_id)` — kwarg 是 `chunk_id`,不是 `memory_id`
- `cortex_recall` over-fetch + post-filter `min_importance` (SDK 不原生支持)
- `cortex_conflict_upsert` 把 `source_key` 和 `confidence` 编到 tags 里 (SDK `remember()` 不接受这两个 kwarg)
- 删了未用 `import uuid` (handler 不再手动生成 chunk_id)

**部署链**:
- HEAD `01a704c` → `41cda99` (http_app 重构) → `d55c7ce` (handler 调对 SDK) → `bc50435` (recall post-filter)
- VM `/opt/cortex-deploy/work` git pull → `/opt/memory-cortex/server/src/mcp_server/` 拷贝 → `docker restart memory-cortex`
- container `/app/src/mcp_server/cortex_mcp_server.py` 通过 bind-mount 立即生效 (ro 但 host 写就生效)

**端到端验证** (ssh 127.0.0.1 + python urllib):
- POST /mcp/ initialize → HTTP 200, 返回 serverInfo "memory-cortex 3.4.7" + 完整 capabilities
- POST /mcp/ notifications/initialized → HTTP 202
- POST /mcp/ tools/list → HTTP 200, 6 工具(cortex_remember/cortex_recall/cortex_forget/cortex_introspect/cortex_conflict_upsert/cortex_get),每个都有 fastmcp 生成的 inputSchema
- POST /mcp/ tools/call cortex_introspect → HTTP 200, 返回真实 cortex health+stats (chunks:2636 relations:929 vectors:2636)
- POST /mcp/ tools/call cortex_recall → HTTP 200, 3 个 ranked memory chunks via RRF hybrid (importance + similarity + composite_score)

**已知 307 redirect**: GET /mcp → /mcp/ (Starlette Mount 自动 trailing-slash 处理,客户端需用 /mcp/)
**已知坑**: SDK `recall()` 没 `min_importance` kwarg → handler over-fetch + 客户端 post-filter
**已知坑**: SDK `remember()` 没 `confidence` kwarg → handler 编到 tag
**已知坑**: 不删 `from mcp.server.streamable_http_manager import StreamableHTTPSessionManager` 死代码 (注释用,import 时间浪费但不影响功能)

