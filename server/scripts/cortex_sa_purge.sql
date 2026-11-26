-- Cortex 首次 SA 强制清理 (ADR-069 D3 续, 2026-08-06)
-- 一次性, 删所有 SA* 来源 (importance<7) 不论时间
-- 背景: SA 247 条 importance 实测全 6-8, 用 <6 阈值永远命中 0, 调到 <7 后清掉 191 条
-- 用法: docker exec -i memory-cortex psql -U postgres -d postgres -f /tmp/cortex_sa_purge.sql

\echo '==SA purge preview=='
SELECT source, COUNT(*) AS will_delete, ROUND(AVG(importance)::numeric, 2) AS avg_imp
  FROM memory_chunks
 WHERE source LIKE 'SA%' AND (importance IS NULL OR importance < 7)
 GROUP BY source ORDER BY will_delete DESC;

\echo
\echo '==executing=='
BEGIN;
WITH deleted AS (
  DELETE FROM memory_chunks
   WHERE source LIKE 'SA%' AND (importance IS NULL OR importance < 7)
  RETURNING 1
)
SELECT 'deleted=' || COUNT(*) FROM deleted;
COMMIT;

\echo
\echo '==remaining SA*=='
SELECT COALESCE(source,'(NULL)') AS source, COUNT(*) AS n
  FROM memory_chunks WHERE source LIKE 'SA%' GROUP BY source ORDER BY n DESC;

\echo
\echo '==final stats=='
SELECT 'total_chunks=' || COUNT(*) FROM memory_chunks;
