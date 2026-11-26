-- Migration: v6.5.0 Phase 2 — write-time maintenance queue
-- Date: 2026-09-01
-- Spec: ad-hoc design (write-time enqueue + background worker, ADR-075-aligned)
--
-- ADR-075 删除了 time-driven cron 后, 维护完全靠手工 endpoint.
-- 本 migration 给 ConsolidationScheduler (#A.3) 加一个持久化 PG 队列:
--   1. 主路径 INSERT memory_chunks 后, 后台线程把 chunk_id 写进 queue
--   2. 单 worker 线程 SELECT ... FOR UPDATE SKIP LOCKED 拉一条处理
--   3. extract + resolve_conflicts 完成后写 status='done'
--   4. retry_count 达上限 (5) → status='dead', 通过 /admin/maintenance/dead 看
--
-- 设计理由:
--   - SKIP LOCKED 让 PG 自己分配, 不需要外部锁
--   - lease_until 让 worker 崩溃后 30s 内其他 worker 接管 (此版本单 worker, 留扩展)
--   - ON CONFLICT (chunk_id) DO NOTHING 让同一 chunk 多次 enqueue 是幂等的
--   - 不阻塞主路径: 内存 buffer + 后台 flush_thread (200ms 一次)
--
-- 索引策略:
--   - PK (id) BIGSERIAL 自动
--   - (status, enqueued_at) 让 worker 拉 "pending 且最早" 的
--   - (chunk_id) UNIQUE 防重复入队
--   - 部分索引 WHERE status='pending' 让 worker 扫得少

CREATE TABLE IF NOT EXISTS memory_maintenance_queue (
    id              BIGSERIAL PRIMARY KEY,
    chunk_id        TEXT NOT NULL REFERENCES memory_chunks(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'done', 'dead')),
    retry_count     INTEGER NOT NULL DEFAULT 0,
    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    leased_at       TIMESTAMPTZ,
    lease_until     TIMESTAMPTZ,
    worker_id       TEXT,
    last_error      TEXT,
    completed_at    TIMESTAMPTZ,
    CONSTRAINT memory_maintenance_queue_chunk_id_unique UNIQUE (chunk_id)
);

-- Worker 拉取索引: (status, enqueued_at) 让 pending 中最早的先出
CREATE INDEX IF NOT EXISTS memory_maintenance_queue_pending_idx
    ON memory_maintenance_queue (enqueued_at)
    WHERE status = 'pending';

-- 监控 / 死信查询索引
CREATE INDEX IF NOT EXISTS memory_maintenance_queue_status_idx
    ON memory_maintenance_queue (status, enqueued_at DESC);

-- Lease 过期回收索引: worker 重启后扫 lease_until < now() 的 processing
CREATE INDEX IF NOT EXISTS memory_maintenance_queue_lease_idx
    ON memory_maintenance_queue (lease_until)
    WHERE status = 'processing';

COMMENT ON TABLE memory_maintenance_queue IS
    'v6.5.0 P2: write-time enqueue + background worker processing queue. ADR-075-aligned (content-driven, no time-driven cron).';

COMMENT ON COLUMN memory_maintenance_queue.status IS
    'pending (waiting) / processing (leased by worker) / done (success) / dead (retry exhausted, see /admin/maintenance/dead)';

COMMENT ON COLUMN memory_maintenance_queue.lease_until IS
    'Worker lease expiry. If lease_until < NOW() and status=processing, the row is reclaimable. Default 30s.';

COMMENT ON COLUMN memory_maintenance_queue.worker_id IS
    'Hostname:pid of the worker that leased this row. Useful for forensic log lines.';

-- Down migration (commented for reference; do not run unless rolling back):
--   DROP TABLE IF EXISTS memory_maintenance_queue CASCADE;