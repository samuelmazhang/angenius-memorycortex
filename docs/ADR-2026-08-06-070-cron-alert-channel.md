# ADR-070: cortex cron 告警通道设计 (2026-08-06)

- Status: Accepted
- Date: 2026-08-06
- Author: Arcadia Agent (MiniMax-M3)
- Depends on: ADR-069 (数据治理 + cron)
- Supersedes: 无

## Context

ADR-069 D5 列了"已知遗留":
> cron `deliver=local`, 失败时用户**不会收到通知**。

3 个现有 cron (`cortex-daily`/`weekly`/`hourly` via `cortex_maintenance.py`) + 1 个新 cron (`cortex-data-cleanup` via `maintenance_daily.sh`) 全部 `deliver=local`, 即沉默 watchdog 模式。

**问题**: 静默失败不可见 (DB 满盘 / PG 挂了 / SA 阈值爆表 都看不到)。

## Decision

**3 通道并行** (按降级链):

| 优先级 | 通道 | 实现 | 失败兜底 |
|--------|------|------|----------|
| 1 (主) | **Telegram** | 复用 ADR-069 cron 框架, `deliver='telegram:CHAT_ID:THREAD'` | → 2 |
| 2 (本地) | **Heartbeat 文件** | `/tmp/cortex_maintenance_heartbeat` 写时间戳 + 结果 | → 3 |
| 3 (沉默) | **watchdog 沉默** | 当前 `deliver=local` 行为保留 | — |

**触发条件** (满足任一即发):

| 触发 | 阈值 |
|------|------|
| SA 删除 > 50 | (ADR-069 已设) |
| working_memory 删除 > 20 | (ADR-069 已设) |
| archived (imp≤2 + >90d) > 200 | (ADR-069 已设) |
| **新增**: DB size > 90% of 5GB | PG `pg_database_size` 查 |
| **新增**: cron 本身 exit ≠ 0 | shell exit code |

**正常时**: 全部沉默 (no stdout to deliver)。

## Implementation

### D1. Telegram 通道

复用现有 `arcadia_telegram` adapter (已在 Arcadia 配置)。新增 cron wrapper:

```bash
# server/scripts/cron_with_alert.sh
#!/usr/bin/env bash
set -euo pipefail
result=$("$@" 2>&1) || rc=$?
# 检查是否触发告警
if [[ "${result}" =~ \[ALERT\] ]] || [[ ${rc:-0} -ne 0 ]]; then
    echo "[CORTEX ALERT] ${result}" | arcadia_telegram notify --channel admin
fi
```

### D2. Heartbeat 文件

```bash
# 在 maintenance_daily.sh 末尾
echo "last_run=$(date -u +%FT%TZ) status=ok deletions={sa:N,wm:N,arch:N}" \
  > /tmp/cortex_maintenance_heartbeat
```

**外部检查** (独立 cron / 监控 agent): 读 heartbeat, > 26h 未更新 → 告警。

### D3. 沉默模式

不动。`deliver=local` 保留。

## Consequences

### 正面
- 失败可见 (从"沉默 watchdog" → "沉默 + 主动告警")
- 三通道降级链: Telegram 挂了 → heartbeat 还在 → 至少沉默模式下个 cron 还会跑
- 不引入新依赖 (复用 Arcadia telegram adapter)

### 负面 / 风险
- Telegram 通道依赖 Arcadia 进程存活 (但 24/7 跑)
- Heartbeat 文件本机, 远程监控看不到 (本期先解决"进程死了"问题, 跨机监控下期)
- 阈值是硬编码, 后续可放 settings 表

## Verification

- [x] `cron_with_alert.sh` 单元测试: exit 0 无 alert, exit 1 有 alert
- [x] `maintenance_daily.sh` 末尾写 heartbeat
- [x] 模拟 SA 删除 > 50 → Telegram 收到 `[CORTEX ALERT]`
- [ ] 真实 8.2 跑 7 天后检查 heartbeat 文件 + telegram 告警日志

## Out of Scope

- Prometheus / Grafana 集成 (本期不引)
- PagerDuty / OpsGenie 集成 (企业级, 当前不需要)
- 跨机监控 (下期, 需要 heartbeat 服务化)
