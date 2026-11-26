#!/bin/bash
# Cortex daily maintenance wrapper (2026-08-06)
# 调用方: hermes cronjob (no_agent=true, schedule='0 3 * * *')
# 行为: 跑 maintenance_daily.sql, 解析 key=value 输出
#   - 一切正常且删除量小: 不输出 (沉默, watchdog pattern)
#   - 删除量异常大 或 失败: 输出 [ALERT] 前缀, cron 自动通知
#
# 退出码:
#   0 = 全部成功 (无论是否触发告警)
#   1 = SQL 失败 / 容器不可达 (cron 当作失败通知)

set -u

CONTAINER="memory-cortex"
DB="postgres"
DB_USER="postgres"
SQL_FILE="/tmp/maintenance_daily.sql"   # 容器内路径
HOST_SQL="$(dirname "$(readlink -f "$0")")/maintenance_daily.sql"

# 阈值 (可在未来走 ADR 调整)
SA_ALERT_THRESHOLD=50         # 单次 SA 删除 >50 视为异常
WORKING_ALERT_THRESHOLD=20    # 单次 working_memory 删除 >20 视为异常
ARCHIVE_ALERT_THRESHOLD=200   # 单次 archiving >200 视为异常

# 容器可达性快速检查
if ! sudo docker exec "$CONTAINER" true 2>/dev/null; then
    echo "[ALERT] container $CONTAINER unreachable"
    exit 1
fi

# 推 SQL 进容器 (覆盖, 幂等)
sudo docker cp "$HOST_SQL" "$CONTAINER:$SQL_FILE" >/dev/null 2>&1

# 跑 SQL, 解析输出
RAW=$(sudo docker exec "$CONTAINER" \
    psql -U "$DB_USER" -d "$DB" -v ON_ERROR_STOP=1 -tA -f "$SQL_FILE" 2>&1) || {
    echo "[ALERT] SQL execution failed:"
    echo "$RAW"
    exit 1
}

# 抽 key=value
get_kv() { echo "$RAW" | grep -E "^$1=" | head -1 | cut -d= -f2; }

SA_DEL=$(get_kv sa_chunks_deleted)
WORKING_DEL=$(get_kv working_memory_deleted)
ARCHIVED=$(get_kv archived_low_importance)
PROMOTED=$(get_kv tier_permanent_promoted)
TOTAL=$(get_kv metric:total_chunks)
WITH_KEY=$(get_kv metric:with_source_key)
PERMANENT=$(get_kv metric:tier_permanent)
DB_SIZE=$(get_kv metric:db_size_mb)

ALERTS=()
[ "${SA_DEL:-0}" -gt "$SA_ALERT_THRESHOLD" ]      && ALERTS+=("SA deletions $SA_DEL > $SA_ALERT_THRESHOLD")
[ "${WORKING_DEL:-0}" -gt "$WORKING_ALERT_THRESHOLD" ] && ALERTS+=("working_memory deletions $WORKING_DEL > $WORKING_ALERT_THRESHOLD")
[ "${ARCHIVED:-0}" -gt "$ARCHIVE_ALERT_THRESHOLD" ] && ALERTS+=("archived $ARCHIVED > $ARCHIVE_ALERT_THRESHOLD")

if [ ${#ALERTS[@]} -gt 0 ]; then
    echo "[ALERT] cortex maintenance异常: ${ALERTS[*]}"
    echo "metrics: total=$TOTAL with_source_key=$WITH_KEY permanent=$PERMANENT db_size=${DB_SIZE}MB"
    echo "deleted: SA=$SA_DEL working=$WORKING_DEL archived=$ARCHIVED promoted=$PROMOTED"
    exit 0  # 告警但不是错误, cron 当成功
fi

# 全部正常: 沉默 (no_agent=true + 空 stdout = 沉默)
exit 0
