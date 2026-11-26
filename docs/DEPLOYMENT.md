# Cortex v6.5.0 — Deployment Guide

> **Date**: 2026-08-23
> **Phase**: 1+2 COMPLETE (12/12 directions)
> **Status**: Ready for A/B 灰度 deployment

---

## 1. 概述

Cortex v6.5.0 在 v6.3.1 基础上做了 **12 个方向**的改进:

### Phase 1 (9/9 done)
1. **#2 召回公式修正** — imp 5%→15%, composite score 重新平衡
2. **#A.1 LLM-extracted fact 抽取层** — Mem0-style extract() + ADR-068
3. **#3 Nomic v1.5 embedding 升级** — embedding 模型升级 (mpnet → nomic-v1.5)
4. **#4 LOCOMO 1986 在线评测 harness** — 1986 case 评估
5. **#5 HNSW m=24 调优** — ANN index 调参
6. **#A.2 conflict_resolve SUPERSEDE/DEDUPE** — ADR-068 conflict 处理
7. **#A.3 ConsolidationScheduler** — 每 100 chunks 触发反思
8. **#B graph index + HippoRAG PPR** — graph 邻居扩展
9. **#C summary compaction** — 长对话压缩

### Phase 2 (3/3 done)
10. **NEW3 LLM-fact 质量监控** — fact hallucination rate 监控
11. **#6 FastAPI OpenTelemetry** — observability
12. **#9 RAPTOR 递归摘要层** — 多层 cluster + recursive summarization

**总测试**: 235/235 PASS, 0 回归

---

## 2. 数据库迁移

### 2.1 新增 migrations (按顺序跑)

```bash
# Phase 1
psql -f server/src/migrations/2026_08_23_v6_5_0_nomic_v15.sql
psql -f server/src/migrations/2026_08_23_hnsw_m24.sql
psql -f server/src/migrations/2026_08_23_graph_indexes.sql

# Phase 2 (有,但实际改动小)
# - NEW3 不需 schema 改动 (in-memory analyzer)
# - #6 OpenTelemetry 不需 schema 改动 (env vars + middleware)
# - #9 RAPTOR 不需 schema 改动 (in-memory tree)
```

### 2.2 schema 改动汇总

- `memory_chunks`:
  - 加 `model_version VARCHAR(32) DEFAULT 'mpnet' NOT NULL` (旧 chunks 保留 mpnet)
  - embedding column 升级 `vector(384) → vector(768)` (兼容 Nomic v1.5)
  - 加 4 composite btree indexes (PPR query)
  - 加 `memory_chunks_model_version_idx`

- 新表:
  - `memory_graph_communities`
  - `memory_graph_ppr`
  - `memory_compressed` (from #C)
  - `memory_compressed_summary` (NEW3)
  - `memory_conflict_log` (from #A.2)
  - `memory_extracted_facts` (from #A.1)

---

## 3. 环境变量

### 3.1 必设 (生产)

```bash
# Embedding model 选择
export EMBEDDING_MODEL=nomic-v1.5          # 默认: nomic-v1.5 (Phase 1)

# HNSW tuning
export HNSW_M=24                          # 默认: 24
export HNSW_EF_SEARCH=120                 # 默认: 120
# export HNSW_EF_CONSTRUCTION=200          # 已在 schema 写死

# Embedding model path — *必设*. container 启动时若未设会 crash (RuntimeError).
# 指向 model.safetensors 所在的目录(含 config.json, modules.json, 1_Pooling/, tokenizer.json 等)
export EMBEDDING_MODEL_PATH=/root/.arcadia/plugins/memory-cortex/models/nomic-embed-text-v1.5

# Nomic v1.5 model
export NOMIC_API_KEY=                     # 如果用 nomic 包 API,需要 API key
                                          # 否则用 sentence-transformers (本地)

# Consolidation pipeline (#A)
export CONSOLIDATION_BATCH_SIZE=100       # 默认: 100 chunks 触发
export CONSOLIDATION_INTERVAL_SEC=60       # 默认: 60 秒
```

### 3.2 可选 (Phase 2)

```bash
# OpenTelemetry (#6)
export OTEL_ENABLED=false                 # 默认 false, A/B 灰度时设 true
export OTEL_EXPORTER_OTLP_ENDPOINT=       # 默认: localhost:4317
export OTEL_SERVICE_NAME=cortex-v6.5.0
export OTEL_TRACES_SAMPLER=parentbased_traceidratio
export OTEL_TRACES_SAMPLER_ARG=1.0

# RAPTOR (#9)
export RAPTOR_MAX_DEPTH=3                 # 默认: 3
export RAPTOR_CLUSTER_THRESHOLD=0.7       # 默认: 0.7

# Graph PPR (#B)
export MEMORY_FEATURE_GRAPH_PPR=true       # 默认: true (Phase 1 已开)
export GRAPH_PPR_ALPHA=0.85               # damping factor
export GRAPH_PPR_TOP_K=10                 # 返回 top-k 邻居

# Fact quality (NEW3)
export FACT_QUALITY_LOW_THRESHOLD=0.5     # fact_confidence < 0.5 标记 low-quality
```

### 3.3 Nomic v1.5 模型本地部署(v6.5.0 #3, 2026-08-23)

`get_nomic_model()` 在容器内**不能**访问 huggingface.co。生产部署必须用**本地 HuggingFace snapshot** + `sentence-transformers`。详细决策见 `ADR-2026-08-23-077-nomic-v1.5-local-snapshot-deploy.md`。

**一次性的模型部署** (每个 VM, ~520 MB 下载 + 8 秒 SCP):

```bash
# 1) 本机下载最小必要文件 (~523 MB,跳过 1.6 GB 的 onnx/* 冗余)
#    使用 HF 国内镜像(因 container 网络无法访问 hf.co)
HF_ENDPOINT=https://hf-mirror.com python -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='nomic-ai/nomic-embed-text-v1.5',
    local_dir='/tmp/nomic-embed-text-v1.5-staging',
    allow_patterns=['config.json', 'modules.json', '1_Pooling/config.json',
                    'config_sentence_transformers.json', 'sentence_bert_config.json',
                    'model.safetensors', 'tokenizer.json', 'tokenizer_config.json',
                    'special_tokens_map.json', 'vocab.txt'],
    ignore_patterns=['onnx/*', '*.onnx', '*.onnx_data', 'README.md', '.gitattributes'],
)
"

# 2) SCP 到 VM
scp -r /tmp/nomic-embed-text-v1.5-staging <user>@127.0.0.1:/tmp/

# 3) 放到 bind-mount 路径 (与 MiniLM 一起)
ssh <user>@127.0.0.1 '
  sudo mv /tmp/nomic-embed-text-v1.5-staging /root/.local/share/cortex-models/nomic-embed-text-v1.5
  # bind-mount 8.2 容器内可见路径:
  #   /root/.local/share/cortex-models/  ->  /root/.arcadia/plugins/memory-cortex/models/
'

# 4) 容器内 pip 装 sentence-transformers (清华源)
ssh <user>@127.0.0.1 'sudo -n docker exec memory-cortex \
  pip install -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    huggingface_hub sentence-transformers'
# 注: 装的是 6.0.0 (含 torch 2.13.0)。容器内若没 torch/transformers 这步必须跑。

# 5) 设置 EMBEDDING_MODEL_PATH 环境变量,启动容器
ssh <user>@127.0.0.1 '
  sudo -n docker run -d --name memory-cortex \
    -p 1880:1880 \
    -v /opt/cortex-deploy/work/server/docker/unified/entrypoint.sh:/usr/local/bin/entrypoint.sh:ro \
    -v /opt/cortex-deploy/work/server/docker/unified/schema.sql:/opt/cortex/schema.sql:ro \
    -v /root/.local/share/cortex-models:/root/.arcadia/plugins/memory-cortex/models:ro \
    -v memory-cortex-data:/var/lib/postgresql/data \
    -v /opt/cortex-deploy/work/server/src:/app/src:ro \
    -e EMBEDDING_MODEL_PATH=/root/.arcadia/plugins/memory-cortex/models/nomic-embed-text-v1.5 \
    -e PG_USER=postgres -e PG_DB=postgres -e PG_PASSWORD=change_me \
    --restart unless-stopped \
    memory-cortex-stack:5.3.9
'

# 6) 验证
curl -s http://127.0.0.1:1880/health
# 期望 {"status":"healthy", "chunks": N, ...}
# 然后 POST /chunks,看 response.model_version == "nomic-v1.5"
```

**一次性 ETL re-embed** (一次性迁移所有老 mpnet chunks 到 nomic-v1.5, 2694 chunks ~10 分钟):

```bash
# 在容器内运行 ETL (用 /tmp/shim 绕过 etl_reembed.py 的 hardcoded import 路径)
ssh <user>@127.0.0.1 '
  sudo -n docker exec -e PYTHONPATH=/tmp/shim memory-cortex \
    bash -c "cd /app/src && python3 -m etl.etl_reembed \
      --target-model nomic-v1.5 --batch-size 50 --sleep-ms 20"
'
# 注意: ETL 脚本 hardcode `from server.src.api_pg import`,但容器内
# root 是 /app/src/。需要先在 /tmp/shim/server/src/ 建一个 fake server package
# shim (cp -r /app/src/* /tmp/shim/server/src/) 才能让 import 解析。
# 详细 shim 建法见 ADR-077 验证章节。
```

---

## 4. 部署流程

### 4.1 灰度 rollout (推荐)

| 阶段 | 流量 | 监控 | rollback 条件 |
|---|---|---|---|
| **Canary** | 1% (内部测试) | 24h 观察 logs + metrics | recall < baseline -10% |
| **Phase A** | 10% | 72h 看 LOCOMO F1/BLEU/EM | latency P95 > +50% |
| **Phase B** | 50% | 1 周, 收集 user feedback | 严重 bug report |
| **Phase C** | 100% | 持续监控 | — |

### 4.2 灰度开关 (per-feature)

| Feature | Feature flag | Default | 灰度顺序 |
|---|---|---|---|
| #2 recall formula | 写死在 server/src/api_pg.py | ✅ on | 1% → 100% (无 flag, 直接灰度整个 server) |
| #A.1 extract | `_apply_extraction_pipeline` flag | ❌ off | 10% → 50% → 100% (cost 控制) |
| #A.2 conflict_resolve | `_apply_conflict_resolution` flag | ❌ off | 跟 #A.1 同步 |
| #A.3 scheduler | `consolidation_scheduler_enabled` flag | ❌ off | 10% → 100% |
| #3 Nomic v1.5 | `EMBEDDING_MODEL` env | nomic-v1.5 | 10% → 100% (A/B grey embed model) |
| #5 HNSW m=24 | schema migration + ALTER INDEX | ✅ on | 1% → 100% |
| #B graph PPR | `MEMORY_FEATURE_GRAPH_PPR` | ✅ on | 1% → 100% |
| #C summary compaction | `_apply_compaction_pipeline` flag | ❌ off | 50% → 100% |
| NEW3 fact quality | `_apply_fact_quality_check` flag | ❌ off | 跟 #A.1 同步 |
| #6 OpenTelemetry | `OTEL_ENABLED` env | ❌ off | 100% (无功能影响,只 observability) |
| #9 RAPTOR | `_apply_raptor_query` flag | ❌ off | 50% → 100% |

### 4.3 灰度步骤 (per-feature)

```bash
# Step 1: 灰度前 baseline (1-2 周)
- 运行 LOCOMO harness #4: pytest server/tests/test_locomo_v6_5.py --tb=line
- 收集 baseline F1/BLEU/EM metrics
- 看 24h latency / error rate

# Step 2: Canary 灰度 (1%)
- 在 1% 流量 server 上部署 v6.5.0 (feature flag 全 on)
- 监控 logs 24h:
  - [errors] consolidation / extraction / conflict_resolve
  - [latency] P50 / P95 / P99 (/memory/search)
  - [recall] LOCOMO 跑 nightly F1
- 没问题 → Phase A

# Step 3: Phase A 灰度 (10%)
- 10% 流量
- 监控 72h:
  - [latency] P95 < +20% (vs v6.3.1)
  - [recall] F1 >= baseline -2%
  - [conflict_log] 错误率 < 5%
- 没问题 → Phase B

# Step 4: Phase B (50%)
- 50% 流量
- 监控 1 周:
  - [user feedback] 投诉量 < v6.3.1 baseline
  - [memory_chunks.model_version] mpnet → nomic-v1.5 migration 进度
  - [memory_graph_ppr] 命中 rate (>30% 表示 PPR 工作)
- 没问题 → Phase C

# Step 5: Phase C (100%)
- 全量
- 监控持续
```

---

## 5. 监控 + 报警

### 5.1 关键 metrics (OpenTelemetry #6)

| Metric | Type | 阈值 (报警) |
|---|---|---|
| `cortex.search.latency` | histogram | P95 > 500ms (5x baseline) |
| `cortex.search.recall_at_10` | gauge | < 0.7 (vs baseline 0.85) |
| `cortex.consolidation.queue_depth` | gauge | > 200 (backlog) |
| `cortex.consolidation.fact_extraction_rate` | gauge | < 0.5 (LLM 抽不出 fact) |
| `cortex.conflict.supersede_rate` | gauge | > 0.3 (30% facts superseded) |
| `cortex.consolidation.hallucination_rate` | gauge | > 0.1 (NEW3 监控) |
| `cortex.raptor.query_depth_avg` | gauge | > 5 (deep retrieval 太深) |
| `cortex.hnsw.ef_search_avg` | gauge | > 200 (slow queries) |

### 5.2 关键 logs (结构化 JSON)

```bash
# Search latency spike
grep '"event":"search_latency"' logs/*.log | jq '.duration_ms' | sort -rn | head

# Consolidation error rate
grep '"event":"consolidation_error"' logs/*.log | wc -l

# Graph PPR cache hit rate
grep '"event":"ppr_cache_hit"' logs/*.log | wc -l
grep '"event":"ppr_cache_miss"' logs/*.log | wc -l
```

---

## 6. Rollback 计划

### 6.1 代码回滚 (worst case)

```bash
# 1. 停 v6.5.0 服务
systemctl stop cortex-v6.5.0

# 2. 切回 v6.3.1 (last green)
git checkout v6.3.1 -- server/
systemctl start cortex-v6.3.1

# 3. 不需要回滚 schema (Phase 1+2 新表 / 新字段对 v6.3.1 是 forward-compat)
```

### 6.2 数据回滚 (rare)

```bash
# 如果 chunks 跑坏了 (Nomic embed 出问题)
UPDATE memory_chunks SET model_version = 'mpnet'
WHERE created_at > '2026-08-23' AND model_version = 'nomic-v1.5';

# 触发 ETL 重新 embed
python -m server.src.etl.etl_reembed --target-ratio 1.0 --source-model mpnet --target-model mpnet
```

---

## 7. 部署检查清单

- [ ] 数据库 migration 跑完
- [ ] 环境变量设好 (生产 + 测试)
- [ ] Feature flags 默认 off (canary 1% 后再逐步开)
- [ ] OpenTelemetry collector 接好 (Phase 2 #6)
- [ ] LOCOMO harness 接 CI (Phase 1 #4)
- [ ] Grafana dashboard 加 cortex metrics
- [ ] PagerDuty 报警接好
- [ ] 回滚 playbook 测试过
- [ ] SRE on-call 培训完
- [ ] user 通知准备好

---

## 8. 已知限制 + Phase 3 待评估

| 限制 | 影响 | 缓解 |
|---|---|---|
| #9 RAPTOR 不在 query path (默认 off) | search quality 未提升 | Phase 3 NEW1 cross-encoder rerank 互补 |
| #NEW3 fact quality 只监控不自动 retry | 需人工 review | 1-2 周后看 baseline 决定 |
| #10 PG RLS 未做 | 单用户风险 | Phase 3 优先 (12 周后) |
| #6 OpenTelemetry 默认 off | 无 tracing | Phase 2 rollout 时开启 |

---

**下一步**: 见 `ROLLOUT_PLAN.md` (Phase A/B/C 时间表 + rollback)。
