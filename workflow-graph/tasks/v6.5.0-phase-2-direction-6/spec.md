# Task Spec — v6.5.0 Phase 2 #6 FastAPI OpenTelemetry

**Scenario**: new-feature
**Complexity**: L (4 人天)
**Worktree**: `..worktrees/v6.5.0-phase-2-direction-6`
**Branch**: `v6.5.0/v6.5.0-phase-2-direction-6`
**mcp-graph node**: `node_8a0cbc4dcc9b`

---

## 功能点

必做:

1. **新建 `server/src/telemetry/__init__.py`** (空 package)
2. **新建 `server/src/telemetry/otel.py`** (~200 行)
   - `setup_tracing(app: FastAPI)` — 注册 OpenTelemetry middleware
   - 配置 OTLP exporter (env: `OTEL_EXPORTER_OTLP_ENDPOINT`)
   - 默认 Jaeger/OTLP-compatible endpoint
   - `setup_metrics(app)` — Prometheus + OpenTelemetry metrics

3. **改 `server/src/api_pg.py`** — instrumentation
   - 关键 endpoint traces (`/memory/search` / `/memory/add` / `/chunks/batch`)
   - 关键 metrics (search latency / insert rate / consolidation queue)

4. **新建 `server/src/telemetry/CHANGELOG.md`**

## 验收

- pytest `server/tests/test_telemetry_v6_5.py` (新建, 4 个 test):
  1. `test_setup_tracing_registers_middleware`
  2. `test_setup_metrics_exports_prometheus`
  3. `test_key_endpoints_have_traces`
  4. `test_metrics_emit_search_latency`

## 依赖

- `opentelemetry-api>=1.21.0`
- `opentelemetry-sdk>=1.21.0`
- `opentelemetry-instrumentation-fastapi>=0.42b0`
- `opentelemetry-exporter-otlp>=1.21.0`

## 约束

1. 只做 #6 范围, 不改 consolidation / graph / scheduler
2. **不要在 worktree 根创建 tests/** (P31)
3. spec 写功能不写行数
4. 默认 feature flag `OTEL_ENABLED=false` (production 灰度)
5. 不 commit (主 arcadia 自动)

## 完成标准

写 `.sprint/v6.5.0-phase-2-direction-6-report.md`
