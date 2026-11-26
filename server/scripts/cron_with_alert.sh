#!/bin/bash
# Cortex cron alert channel (ADR-070)
# 用途: 包装任意 cortex 维护命令, 失败时通过 telegram 告警 + 写 heartbeat 文件
# 用法: bash server/scripts/cron_with_alert.sh "<command to run>"
# 例:   bash server/scripts/cron_with_alert.sh "bash server/scripts/maintenance_daily.sh"
#
# 行为:
#   - 正常完成 (exit 0, 无 [ALERT] 标记): 沉默
#   - 异常 (exit ≠ 0 或 stdout 含 [ALERT]): 发 telegram + 写 heartbeat warn 行
#
# 通道降级链 (按 D1 > D2 > D3 顺序):
#   D1: Telegram (hermes 通道)
#   D2: Heartbeat 文件 (/tmp/cortex_maintenance_heartbeat) — 本机可读
#   D3: 沉默 (no_agent watchdog 保留)
#
# 退出码:
#   0 = 包装的命令 exit 0
#   1 = 包装的命令 exit 非 0 (cron 当作失败)
#   2 = wrapper 自身失败 (telegram 失败 + heartbeat 失败)

set -u

CMD="${1:-}"
HEARTBEAT="/tmp/cortex_maintenance_heartbeat"
TIMESTAMP=$(date -u +%FT%TZ)
TELEGRAM_FAIL=0

if [ -z "$CMD" ]; then
    echo "[ALERT] cron_with_alert.sh: missing command argument"
    echo "last_run=${TIMESTAMP} status=error reason=missing_arg" > "$HEARTBEAT" 2>/dev/null || true
    exit 2
fi

# 跑命令, 抓 stdout/stderr
RAW=$(bash -c "$CMD" 2>&1) || CMD_RC=$?
CMD_RC=${CMD_RC:-0}

# 决定是否告警
NEED_ALERT=0
ALERT_REASON=""
if [ "$CMD_RC" -ne 0 ]; then
    NEED_ALERT=1
    ALERT_REASON="exit_code=${CMD_RC}"
elif echo "$RAW" | grep -q "\[ALERT\]"; then
    NEED_ALERT=1
    # 抽 [ALERT] 后第一行作为 reason
    ALERT_REASON=$(echo "$RAW" | grep "\[ALERT\]" | head -1 | sed 's/^\[ALERT\] //' | cut -c1-200)
fi

# 写 heartbeat (总是写, 用于 D2 通道 + 外部监控)
if [ "$NEED_ALERT" -eq 0 ]; then
    echo "last_run=${TIMESTAMP} status=ok" > "$HEARTBEAT" 2>/dev/null || {
        echo "[ALERT] cron_with_alert.sh: heartbeat write failed"
    }
    # 沉默 (D3 通道)
    exit 0
fi

# 异常: 走 D1 (telegram) + D2 (heartbeat warn)
ALERT_MSG="[CORTEX ALERT] ${TIMESTAMP} reason=${ALERT_REASON}"

# D1: Telegram (通过 hermes_telegram, 复用 Hermes adapter)
if command -v hermes_telegram >/dev/null 2>&1; then
    if ! echo "$ALERT_MSG" | hermes_telegram notify --channel admin >/dev/null 2>&1; then
        TELEGRAM_FAIL=1
        echo "[WRAPPER WARN] telegram notify failed, falling back to heartbeat only" >&2
    fi
else
    TELEGRAM_FAIL=1
    echo "[WRAPPER WARN] hermes_telegram not found in PATH, falling back to heartbeat only" >&2
fi

# D2: Heartbeat warn 行
echo "last_run=${TIMESTAMP} status=alert reason=${ALERT_REASON} telegram_fail=${TELEGRAM_FAIL}" \
    > "$HEARTBEAT" 2>/dev/null || {
    echo "[ALERT] cron_with_alert.sh: heartbeat write FAILED"
    exit 2
}

# 把原命令 stdout 输出 (保留 [ALERT] 前缀供上层 cron 看到)
echo "$RAW"
exit 1
