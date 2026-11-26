#!/bin/bash
# Cortex rollback script (ADR-069 D6)
# 用途: 把 memory_chunks / memory_conflict_log / working_memory 回滚到 cleanup 前的状态
# 用法: sudo bash server/scripts/rollback_cortex.sh [backup_path]
#
# 默认找最新 /tmp/cortex_backup_*.sql.gz; 找不到就报错退出
# 触发器 trg_chunks_autosourcekey 不会被回滚 (PG data-only dump 不含 trigger DDL)
# 如需删触发器, 手动: docker exec memory-cortex psql -U postgres -d postgres \
#                            -c "DROP TRIGGER IF EXISTS trg_chunks_autosourcekey ON memory_chunks"

set -eu

CONTAINER="memory-cortex"
DB="postgres"
DB_USER="postgres"
BACKUP="${1:-}"

# 1. 找 backup
if [ -z "$BACKUP" ]; then
    BACKUP=$(ls -t /tmp/cortex_backup_*.sql.gz 2>/dev/null | head -1)
fi
if [ -z "$BACKUP" ] || [ ! -f "$BACKUP" ]; then
    echo "[FAIL] no backup file found"
    echo "  expected: /tmp/cortex_backup_YYYYMMDD_HHMMSS.sql.gz"
    echo "  usage: $0 /path/to/backup.sql.gz"
    exit 1
fi
echo "[INFO] using backup: $BACKUP ($(du -h "$BACKUP" | cut -f1))"

# 2. 容器可达性
if ! sudo docker exec "$CONTAINER" true 2>/dev/null; then
    echo "[FAIL] container $CONTAINER unreachable"
    exit 1
fi

# 3. 推 backup 进容器
echo "[INFO] pushing backup into container..."
gunzip -c "$BACKUP" | sudo docker exec -i "$CONTAINER" cat > /tmp/rollback_input.sql
RESTORE_SIZE=$(wc -l < /tmp/rollback_input.sql)
echo "[INFO] restore SQL: $RESTORE_SIZE lines"

# 4. 预检: 确认 backup 包含我们关心的表
for tbl in memory_chunks working_memory memory_conflict_log; do
    if ! grep -q "COPY public.$tbl " /tmp/rollback_input.sql && ! grep -q "COPY $tbl " /tmp/rollback_input.sql; then
        echo "[WARN] backup does not contain COPY for $tbl (will be skipped)"
    fi
done

# 5. 跑回滚 (--clean --if-exists 先 TRUNCATE, 再 INSERT)
echo "[INFO] executing rollback (this may take ~30s for 2429 rows)..."
sudo docker exec -i "$CONTAINER" \
    psql -U "$DB_USER" -d "$DB" -v ON_ERROR_STOP=1 -f /tmp/rollback_input.sql 2>&1 | tail -20

# 6. 校验
echo
echo "[INFO] post-rollback stats:"
sudo docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB" -tA -c "
SELECT 'chunks=' || COUNT(*) FROM memory_chunks;
SELECT 'with_source_key=' || COUNT(*) FROM memory_chunks WHERE metadata ? 'source_key';
SELECT 'working_rows=' || COUNT(*) FROM working_memory;
SELECT 'conflict_log=' || COUNT(*) FROM memory_conflict_log;
SELECT 'archived=' || COUNT(*) FROM memory_chunks WHERE archived=1;
SELECT 'tier_permanent=' || COUNT(*) FROM memory_chunks WHERE tier='permanent';
"

echo
echo "[DONE] rollback complete"
echo "[NOTE] trigger trg_chunks_autosourcekey was NOT rolled back (data-only dump)"
echo "       if you want it gone: sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -c 'DROP TRIGGER IF EXISTS trg_chunks_autosourcekey ON memory_chunks'"

exit 0
