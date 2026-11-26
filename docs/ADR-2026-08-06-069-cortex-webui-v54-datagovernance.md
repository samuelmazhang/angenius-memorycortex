# ADR-069: Cortex Web UI 修复 + V5.4 conflict-resolve 启用 + 数据治理 (2026-08-06)

- Status: Accepted
- Date: 2026-08-06
- Author: Arcadia Agent (MiniMax-M3) + 用户联合决策
- Supersedes: 无 (本轮为多任务聚合)
- Depends on: V5.3.9 runtime + V5.4 schema (ADR-068)

## 背景 (Context)

用户在 2026-08-06 单日内触发 4 个连续任务, 5 个聚焦 commit + 1 个 cron 注册, 涉及:

1. **Web UI 不可用**: 容器 v5.3.9 `GET /` 返回 HTTP 500, 看起来 Web UI 没了
2. **数据治理**: 盘点发现 2430 chunks 但 V5.4 schema (`is_deprecated`/`superseded_by`/`source_key`) 0 启用
3. **历史债**: 247 条 SA 流水线临时摘要 + 27 个僵尸 working_memory 会话堆积
4. **运维自动化**: 无 cron 守护, 每次清理需手动跑 SQL

### 根因分析

| 问题 | 根因 |
|---|---|
| Web UI 500 | `api_pg.py` 缺 `/` 路由 (commit `efa3600` 修复); 容器 image-baked 源不含 `static/` 目录 |
| `localhost:1880` 写死 | `index.html:198` `const API = "http://localhost:1880"`, 远程访问时浏览器 fetch 仍打 localhost |
| V5.4 schema 未启用 | schema 加了字段/表但**业务侧 0 调用**: 0 条带 `source_key`, 0 行 `memory_conflict_log` |
| 容器源码不更新 | image-baked 源 (image `memory-cortex-stack:5.2.0/5.3.9`); `git pull` 改宿主机工作树对容器无效 |

## 决策 (Decision)

### D1. 容器更新走 bind-mount, 不重新 build image

用户原话 "不要重新 build image"。解决方案: 用现有 `run_docker_stack_unified.sh` 等价命令 (仅 `IMG_TAG=5.3.9`) 启动容器, bind-mount 宿主机 `/opt/cortex-deploy/work/server/src:/app/src:ro`。

**约束**:
- 宿主机 `server/docker/unified/entrypoint.sh` 必须 `chmod +x` (否则容器启动 permission denied)
- 之后 `git pull` + `docker restart` ≈ 10 秒生效

### D2. Web UI 后端地址自动探测, 三级覆盖链

不再硬编码 `localhost:1880`。三级覆盖 (高 → 低):

1. URL `?api=...` 查询参数 (一次性)
2. `localStorage.cortex_api` (持久, 用户点 ✎ 按钮设置)
3. `window.location.protocol + hostname + :1880` (默认, 自动跟随)
4. `http://localhost:1880` (兜底, 仅 `file://` 等异常协议)

额外: 顶部加 `api-bar` 显示当前 API + ✎ 编辑按钮, 排障友好。

### D3. 数据治理四件套 (P1-P5)

| P | 任务 | 实现 |
|---|---|---|
| P1 | V5.4 conflict-resolve 启用 | (a) SQL 回填 `metadata.source_key = source \|\| ':' \|\| type \|\| ':' \|\| id` for 2429 行; (b) `BEFORE INSERT` 触发器 `trg_chunks_autosourcekey` 自动补 |
| P2 | SA 临时摘要清理 | `DELETE FROM memory_chunks WHERE source LIKE 'SA%' AND importance<6 AND created_at > 24h` (首次跑删 1 条, 阈值保守) |
| P4 | 僵尸 working_memory | `DELETE FROM working_memory WHERE updated_at > 2d OR session_id LIKE 'bb-bb-%'/'test_%'/'sess_%'/'e2e-%'` |
| P5 | tier=permanent 补齐 | `UPDATE memory_chunks SET tier='permanent' WHERE importance>=9 AND tier!='permanent'` |

**设计原则**: 全部操作幂等 + 全部 `WHERE` 限定 + 全部在 `BEGIN/COMMIT` 内 + 备份前置 (`pg_dump --data-only` → `/tmp/cortex_backup_*.sql.gz` ≈ 4MB)

### D4. 每日 cron 自动化 (no_agent watchdog)

**不替换** 既有 3 个 cron (`cortex-daily`/`weekly`/`hourly` 走 `cortex_maintenance.py` HTTP API), 而**增量**添加 `cortex-data-cleanup`:

| 维度 | 旧 cron (HTTP API 层) | 新 cron (SQL 层) |
|---|---|---|
| Job ID | f33430dee674 / e5602cae9fec / a596ce840d8b | a1714f2f8720 |
| 脚本 | `cortex_maintenance.py` | `maintenance_daily.sh` |
| 调用方式 | HTTP API (`/forgetting/cleanup` 等) | 直 SQL `psql -U postgres -d postgres` |
| Schedule | 03:00 / 04:00 周日 / 每小时 | **04:30** (错开 03:00 daily + 04:00 weekly) |
| Mode | no_agent | no_agent |
| Deliver | local | local (遗留: 告警用户看不到, 见 D5) |

**阈值告警** (单次删除超阈值 → `[ALERT]` 前缀 + metrics):
- SA 删除 > 50
- working_memory 删除 > 20
- archived (imp<=2 + >90d) > 200

**沉默模式** (no_agent + 空 stdout = watchdog pattern): 一切正常时不输出, 只在告警或失败时说话。

### D5. 已知遗留 (未在本轮处理)

1. **告警通道**: cron `deliver=local`, 失败时用户**不会收到通知**。下次需改 `deliver='telegram'` 或加 `/tmp/cortex_maintenance_heartbeat` 文件供独立检查。
2. **回滚脚本**: 有 backup (`/tmp/cortex_backup_*.sql.gz`) 但无 `rollback.sh` 自动脚本。本 ADR 决策: **是必须补的, 见 D6**。
3. **trigger 测试**: 仅测 1 次 INSERT, 未在 CI 跑回归。
4. **connector 死代码**: `connector/arcadia/cortex_api.py` 自 V5.2.6 (commit 3bf8a59) 起 import `memory_chunk.py` 不到, 整个 `connector/arcadia/` 是孤儿代码, 本轮 D 桶 commit 时漏处理。

### D6. 回滚脚本 (本轮新增)

提供 `server/scripts/rollback_cortex.sh`: 一键回滚到清理前状态。

**限制**: 触发器 `trg_chunks_autosourcekey` 创建在 named volume PG, 回滚 backup 时**会保留触发器** (PG dump 不含 trigger DDL by default, 见 D7)。

### D7. 未来 ADR 候选

- ADR-070 (候选): cron 告警通道设计 (telegram vs heartbeat vs prometheus)
- ADR-071 (候选): connector 死代码清理 (走 Jaccard 跨域扫描确认 0 引用)
- ADR-072 (候选): webui E2E 测试 (Playwright, 跟历史 SA5 e2e 经验)

## 验证 (Verification)

### 5 段 ~25 断言 ad-hoc 验证 (临时, 跑完删)

| 段 | 内容 | 结果 |
|---|---|---|
| 1 | 文件存在 + bash 语法 | ✓ |
| 2 | Shell 包装器结构 | ✓ 9/9 |
| 3 | SQL 结构 (sentinel/24h/2d/90d/permanent/WHERE/key=value) | ✓ 12/12 |
| 4 | 真实 8.2 跑 (idempotent exit 0, silent) | ✓ |
| 5 | Arcadia cron 注册 (job_id/schedule/mode) | ✓ |

### 数据库 diff (执行前后)

```
chunks:           2430 → 2429  (-1,  P2 单次)
with_source_key:    0  → 2429  (+2429,  P1 启用 V5.4)
tier=permanent:     0  → 51    (+51,   P5)
working_memory:    48  → 8     (-40,   P4 -83%)
SA 残留:          248  → 247   (-1,    P2)
DB size:           38 MB (未变, 索引占大头)
```

### Web UI

- 容器日志: HTTP 200, 30553B HTML, `<title>Memory Cortex Dashboard</title>`
- `/health`: chunks=2429, relations=1120, vectors=2429
- API 自动探测: 6/6 case + 5/5 结构验证通过

## 影响 (Consequences)

**正面**:
- Web UI 恢复 + 自动适配远程/本机访问
- V5.4 conflict-resolve 真正激活 (新写入自动带 source_key)
- 数据库瘦身 + 长期健康 (cron 守护)
- 文档从 802 行瘦到 198 行 (净 -604 行, ADR-MAP 重生成)

**负面 / 风险**:
- 触发器 `trg_chunks_autosourcekey` 是隐式行为, 后续 schema 变更需 review
- cron `deliver=local` 告警盲区, 需后续 ADR-070 解决
- `connector/arcadia/cortex_api.py` 死代码未清理, 长期债 +1
- 文档瘦身可能丢失历史背景 (建议保留 git history + ADR-MAP 入口)
