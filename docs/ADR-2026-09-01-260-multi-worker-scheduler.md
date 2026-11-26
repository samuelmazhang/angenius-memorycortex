# ADR-260: P2 scheduler 多 worker 模式

- Status: Accepted
- Date: 2026-09-01
- Author: Hermes (subagent dispatch) + 天行健 (决策)
- Depends on: v6.5.0 P2 (server/src/consolidate/scheduler.py)

## 背景 (Context)

P2 scheduler 默认单 worker 模式: 一个 memory-cortex 容器跑一个
ConsolidationScheduler 实例, 一个 flush thread + 一个 worker thread, 通过
`memory_maintenance_queue` PG 表的 `SELECT ... FOR UPDATE SKIP LOCKED`
claim 行。当前的 SKIP LOCKED 实现已经为多 worker 准备就绪 -- 这是 PG
层面的并发原语, scheduler 本身不需要改。

但**多 worker 部署的具体操作**之前没有文档, 也没人知道哪些 env knobs
需要调, 哪些 PG 监控需要看。

## 决策 (Decision)

### 调度器对多 worker 的支持

`server/src/consolidate/scheduler.py` 已经支持任意数量的并发 worker, 通过:

1. **PG 行级锁**: `SELECT id FROM memory_maintenance_queue
   WHERE status='pending' AND (lease_until IS NULL OR lease_until < NOW())
   ORDER BY enqueued_at ASC FOR UPDATE SKIP LOCKED LIMIT 1` (api_pg.py:3895)

   SKIP LOCKED 让两个 worker 永远不会同时处理同一条 chunk。PG 9.x+
   默认行为。

2. **Lease 机制**: claim 时设 `lease_until = NOW() + 30s`。如果 worker 在
   30s 内没 mark_done / mark_failed, 行回到 `pending` 状态, 另一个
   worker 可以 claim 走。`worker_id` (host:pid) 记录到行内做 forensic。

3. **Idempotent enqueue**: `INSERT ... ON CONFLICT (chunk_id) DO NOTHING`,
   同一 chunk 被多 worker (或 N 次 enqueue) 不会产生重复行。

4. **去重 in-memory buffer**: `enqueue_chunk_id` 在 `self._buffer` 里
   用 `if chunk_id in self._buffer` 短路返回, 避免双写。但 **跨 worker 不去重**
   (每个 worker 进程自己的 buffer) -- 靠 PG 的 ON CONFLICT 兜底。

### 多 worker 部署 (horizontal scale)

#### 选项 A: 同 host 多 container (开发 / 小规模)

每个 container 端口不同 (1880, 1881, 1882), 共享同一个 PG volume:

```bash
# Worker 1 (现有)
sudo docker run -d --name memory-cortex-1 --restart unless-stopped \
  -p 1880:1880 -v memory-cortex-data:/var/lib/postgresql/data \
  -v /opt/cortex-deploy/work/server/src:/app/src:ro \
  -v /opt/cortex-deploy/work/server/docker/unified/schema.sql:/opt/cortex/schema.sql:ro \
  -v /opt/cortex-deploy/work/server/docker/unified/entrypoint.sh:/usr/local/bin/entrypoint.sh:ro \
  -v /root/.local/share/cortex-models:/root/.hermes/plugins/memory-cortex/models:ro \
  memory-cortex-stack:6.5.0

# Worker 2 (新加)
sudo docker run -d --name memory-cortex-2 --restart unless-stopped \
  -p 1881:1880 -v memory-cortex-data:/var/lib/postgresql/data \
  -v /opt/cortex-deploy/work/server/src:/app/src:ro \
  -v /opt/cortex-deploy/work/server/docker/unified/schema.sql:/opt/cortex/schema.sql:ro \
  -v /opt/cortex-deploy/work/server/docker/unified/entrypoint.sh:/usr/local/bin/entrypoint.sh:ro \
  -v /root/.local/share/cortex-models:/root/.hermes/plugins/memory-cortex/models:ro \
  memory-cortex-stack:6.5.0
```

- **没有端口冲突**: 都 bind PG (容器内 5432, 内部网络), 外部只 expose 1880/1881
- **共享 PG 队列**: `memory_maintenance_queue` 自动被多 worker SKIP LOCKED claim
- **worker_id 不同**: 每个 container 的 hostname:pid 都不同, 写到 `worker_id` 列
- **stat endpoint 都可用**: 各自的 /admin/maintenance/stats 只反映本 worker 视角 (注意:
  是本 worker 拉的 PG 视图, 但因为是 SQL 查 PG 表, 实际是**全队列视图**, 多个
  worker 看到的数字一致)

#### 选项 B: 跨 host 多副本 (生产 / 规模)

k8s Deployment with `replicas: 3` + shared Postgres Cluster.

- 同样工作 (PG 队列是 share state, 多副本协调)
- 用 `worker_id` label 在 Prometheus scrape 区分 (e.g.
  `cortex_scheduler_buffered_count{worker_id="cortex-pod-7b3c-xyz"}`)

### 何时扩容

- **单 worker 默认够用**: 写入 < 100 chunks/sec, 单 worker 测速 ~5 chunks/sec 处理
  (含 extract + resolve_conflicts 跑 Postgres)
- **背压信号**: `cortex_queue_pending` 持续 > 100 即扩容

```promql
# 触发扩容
cortex_queue_pending > 100 for 5m
```

### 调优 knobs (env vars)

- `CORTEX_SCHED_FLUSH_INTERVAL` (default 0.2s) -- 写峰值高时调小 (0.05s)
- `CORTEX_SCHED_WORKER_POLL` (default 1.0s) -- worker 多了可以调大 (2-3s) 减少 PG 压力
- `CORTEX_SCHED_LEASE_SECONDS` (default 30s) -- worker 多了可以调小 (10s) 让
  crash 后的 row 更快被重 claim
- `CORTEX_SCHED_MAX_RETRIES` (default 5) -- 保持, poison chunk 不应该频繁重试
- `CORTEX_SCHED_ENABLED=0` -- 紧急时全停 (回滚到 Phase 1 行为)

### 监控 (Prometheus)

每个 worker export 自己的 /metrics:
- `cortex_scheduler_worker_id{worker_id="..."}` (label)
- `cortex_scheduler_running{worker_id="..."}`
- `cortex_queue_total{worker_id="..."}` (所有 worker 看到的数字一致)
- `cortex_queue_dead{worker_id="..."}` (任一 worker 处理后的结果)

PromQL 健康检查:
```promql
# 所有 worker 都在跑 (5m 窗口)
up{job="cortex"} == 0  # 任一 pod 挂了 alert

# P2 pipeline 健康
rate(cortex_scheduler_total_chunks_processed[5m]) > 0
cortex_queue_dead > 10  # 死信累积超过 10 告警
```

## 风险 (Risks)

1. **PG 连接数**: N 个 worker 每个开 ~2 PG 连接 (pg_pool minconn=2). 默认
   PG `max_connections=100`. N <= 40 安全. 超出需要调大 PG max_connections
   或用 pgbouncer.

2. **Lease 抖动**: 网络分区下 worker A 持 lease, 30s 后 B claim, A 恢复后
   试图 mark_done, 命中已 claim (新 worker B 的 worker_id 不匹配) -- 当前
   mark_done 不做 worker_id 校验, 可能出现 "done 覆盖 done" 的无害场景
   (幂等). 但生产应加 worker_id 校验.

3. **PG 死信积压**: 5 retries 后 status='dead', 但表里不删. 需要定期
   `/admin/maintenance/dead` 监控 + 人工清.

4. **Venv-baked image**: 扩 worker 意味着多个容器同时跑同一 image. 如果
   image 里 baked 的 venv 版本不一致, 行为会微妙不同. 用 v6.5.0 image tag
   锁死避免.

## 验收 (Verification)

- 启动 2 worker (`memory-cortex-1`, `memory-cortex-2`), 各自 /admin/maintenance/stats 显示
  不同 `worker_id` 但共享 queue_counts
- POST 1 chunk → 2 worker 中**任一**处理 (查 `memory_history` 表的 worker_id 字段)
- kill 1 worker, 30s 内另一个 worker claim 那条 stuck 在 processing 的行
- (5 分钟) 跑 100 chunks, 通过 `/metrics` 看两个 worker 都在 active
- (1 天) 跑 1000 chunks, dead = 0 (除非故意注入 poison chunk)