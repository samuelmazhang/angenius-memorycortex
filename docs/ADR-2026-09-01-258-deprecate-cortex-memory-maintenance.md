# ADR-258: 弃用 cortex-memory-maintenance.sh (P2 worker 接管)

- Status: Accepted
- Date: 2026-09-01
- Author: Hermes (subagent dispatch) + 天行健 (决策)
- Supersedes: 无 (deprecates /opt/cortex-deploy/cortex-memory-maintenance.sh)
- Depends on: v6.5.0 P2 write-time scheduler (server/src/consolidate/scheduler.py)

## 背景 (Context)

`/opt/cortex-deploy/cortex-memory-maintenance.sh` 是在 v6.5.0 P2 写时维护之前,
8.2 host 上唯一会触发 consolidation pipeline (extract + resolve_conflicts)
的机制。它是一个 daily-cron 脚本,内容大致是:

```bash
curl -X POST /forgetting/guided
curl -X POST /forgetting/cleanup
```

`memory-maintenance.log` 显示从 2026-08-25 到 2026-09-01 每天 04:30 都跑了一次,
最近一次: 2026-09-01 04:30:01 `guided: checked=546 upgraded=0` + `cleanup: total_checked=0 expired=0`。

## 决策 (Decision)

**完全弃用** cortex-memory-maintenance.sh。v6.5.0 P2 write-time scheduler
(`ConsolidationScheduler` Phase 2) 已经是它的超集:

| 维度 | 旧 cron (cortex-memory-maintenance.sh) | 新 P2 worker (scheduler) |
|---|---|---|
| 触发 | 每 24h 一次 | 每条新 chunk 入库 (实时) |
| 范围 | 全表扫 (lookback=7d) | 增量 (只处理新 chunks) |
| 死信 | 无 (失败静默) | 5 retries 后 status='dead' |
| 持久化 | 无 (进程退出即丢) | PG 表 `memory_maintenance_queue` 持久 |
| Lease | 无 (无 worker 协调) | 30s SKIP LOCKED lease |
| 可观测 | log 文件 | `/admin/maintenance/stats` endpoint |
| 操作 | SSH + crontab | endpoint 实时查询 |

P2 worker 还**多做了**事情: 走完整的 extract + resolve_conflicts, 不只是
/forgetting/guided (用 recall 频率决策 tier) + /forgetting/cleanup (按 TTL 删过期)。

## 状态 (Status)

- crontab 已无 cortex entry (之前某次清理时删过, 8.2 host 的 crontab 目前只有
  1 行 BT-Panel cron)
- 脚本本体 `/opt/cortex-deploy/cortex-memory-maintenance.sh` (902 字节) 还存在
  在 8.2 host 但**已无 cron 触发** (dead code, 不会跑)
- 不删除脚本 (off-repo, 在 8.2 host 本地, 不影响 git tree)
- 不在 git 删除 (该文件本就不在 repo 内)

## 验收 (Verification)

- `crontab -l` 输出**不包含** cortex-memory-maintenance.sh
- `memory-maintenance.log` 自 2026-09-01 04:30 后**无新 entry**
- P2 scheduler 在跑: `curl /admin/maintenance/stats` 返回
  `is_running: true, worker_id: <host>:<pid>`
- POST /chunks 新增 1 条 → 2 秒内 `total_chunks_processed` +1

## 风险 (Risks)

- 如果未来 8.2 host 重装 crontab, 旧脚本可能**意外被加回 cron**。建议:
  - 在 ops 文档 (`docs/04-技术设计/8.2-host-ops.md` 之类) 标注"v6.5.0+ 不再使用 daily maintenance cron"
  - 或把 `cortex-memory-maintenance.sh` 重命名为 `cortex-memory-maintenance.sh.disabled`
    (显式 mark)