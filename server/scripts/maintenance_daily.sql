-- Cortex 每日 maintenance (2026-08-06 起, 由 maintenance_daily.sh 调用)
-- 设计: 全部操作幂等, 可重复跑
-- 输出格式: 标准 key=value 行, bash 包装器解析用于告警阈值

\echo '==maintenance:start=='

-- ============================================================
-- P2: 清 SA 流水线临时摘要 (>24h + importance<6)
-- ADR-069 D3: cron 阈值 24h 保守运行, 首次强制清走 cortex_sa_purge.sql
-- ============================================================
WITH deleted AS (
  DELETE FROM memory_chunks
   WHERE source LIKE 'SA%'
     AND (importance IS NULL OR importance < 6)
     AND created_at < NOW() - INTERVAL '24 hours'
  RETURNING 1
)
SELECT 'sa_chunks_deleted=' || COUNT(*) FROM deleted;

-- ============================================================
-- P4: 清僵尸 working_memory (>2 天没动 + 测试/历史 session)
-- ============================================================
WITH deleted AS (
  DELETE FROM working_memory
   WHERE updated_at < NOW() - INTERVAL '2 days'
      OR session_id LIKE 'bb-bb-%'
      OR session_id LIKE 'test_%'
      OR session_id LIKE 'sess_%'
      OR session_id LIKE 'e2e-%'
      OR session_id = 'adhoctest-sid'
  RETURNING 1
)
SELECT 'working_memory_deleted=' || COUNT(*) FROM deleted;

-- ============================================================
-- 主动 forgetting 推动 (调用 cortex 自身的 /forgetting/evict)
-- 通过 PG 函数 + 简化逻辑实现: importance<=2 + 3个月没动 = 软删
-- ============================================================
WITH archived AS (
  UPDATE memory_chunks
     SET archived = 1,
         updated_at = NOW()
   WHERE archived = 0
     AND importance <= 2
     AND updated_at < NOW() - INTERVAL '90 days'
  RETURNING 1
)
SELECT 'archived_low_importance=' || COUNT(*) FROM archived;

-- ============================================================
-- P5 补齐: tier=permanent (importance>=9 但 tier 不对)
-- ============================================================
WITH promoted AS (
  UPDATE memory_chunks
     SET tier = 'permanent'
   WHERE importance >= 9 AND tier != 'permanent'
  RETURNING 1
)
SELECT 'tier_permanent_promoted=' || COUNT(*) FROM promoted;

-- ============================================================
-- 报告当前状态 (用于趋势监控)
-- ============================================================
SELECT 'metric:total_chunks=' || COUNT(*) FROM memory_chunks;
SELECT 'metric:with_source_key=' || COUNT(*) FROM memory_chunks WHERE metadata ? 'source_key';
SELECT 'metric:tier_permanent=' || COUNT(*) FROM memory_chunks WHERE tier='permanent';
SELECT 'metric:archived=' || COUNT(*) FROM memory_chunks WHERE archived=1;
SELECT 'metric:working_rows=' || COUNT(*) FROM working_memory;
SELECT 'metric:db_size_mb=' || ROUND(pg_database_size('postgres')/1024.0/1024.0, 1);

\echo '==maintenance:done=='
