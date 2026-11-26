# ADR-065 — Cortex 遗忘曲线自动调度 (FastAPI lifespan)

| 项 | 值 |
|---|---|
| **状态** | ✅ Accepted / Implemented |
| **日期** | 2026-07-24 |
| **Sprint** | v10.37 E7 |
| **影响域** | Arcadia Memory Cortex /forgetting/* 调度 |
| **前置提交** | ADR-064 (commit `5501f71` + `c8d2e16`), ADR-063 (commit `32578d9` + `f33715d`) |
| **后续工作** | ADR-066 (forgetting last_run 持久化跨 restart, 后续 sprint) |

---

## 1. Context

用户健康检查 (2026-07-24 00:56 + 12:48 CST) 发现:

| 项 | 之前 | 含义 |
|---|---|---|
| `forgetting/last_run` | null | 主动遗忘从未跑过 |
| `forgetting/next_run` | null | ❌ **从未自动调度** |
| `auto_forgetting_enabled` | true | 配置 OK, 但调度无 |
| `status` | running | service 在,但 scheduler 没启动 |

诊断报告:

> 🔴 高: 跑 POST /reindex 修复 search 500 → ✅ 已修 (ADR-064)
>
> 🟡 中: 跑 POST /storage/enforce 触发主动遗忘 → 这一项需要自动调度
>
> 🟢 低: 增加 taxonomy 父子关系 → 略过 (用户说按你的建议)

诊断背后根因:**`memory_forgetting.py` 有 `ForgettingScheduler` 类设计**(line 417,+ 完整 `run_if_due()`),但**api.py 没有 lifecycle hook 启动它**。

---

## 2. 根因分析

### 2.1 `ForgettingScheduler` 是死代码

| 组件 | 状态 |
|---|---|
| `MemoryForgettingService.execute_forgetting_check()` | ✅ 完整可用 |
| `ForgettingScheduler.should_run/run_if_due/force_run` | ✅ 完整方法 |
| `ForgettingScheduler.run_if_due()` 被调用 | ❌ **从未** |
| FastAPI `@app.on_event("startup")` | ❌ 不存在 |
| asyncio background task | ❌ 不存在 |

**结论**: 调度器设计完成,**但 API 层未调用**。`auto_forgetting_enabled=true` 永远没人 trigger。

### 2.2 类似 pattern 风险

跟 ADR-063 / ADR-064 同结构的设计-实现 gap:
- 代码层 ✅ 完整
- 集成层 ❌ 缺失
- 验证层 ❌ 没 E2E

---

## 3. Decision — FastAPI lifespan 后台调度

### 3.1 选型

| 候选 | 优点 | 缺点 |
|---|---|---|
| `@app.on_event("startup")` | 老 FastAPI 标准 | 0.93+ deprecated |
| FastAPI lifespan | 现代, FastAPI 0.135 推荐 | 需 `@asynccontextmanager` |
| 外部 systemd timer | OS 级可靠 | cortex 内部状态难同步 |
| cron tab | 简单 | 需独立 wrapper script |

**选 FastAPI lifespan** (FastAPI 0.135 仍支持 `lifespan=` 参数, 也支持 on_event 但 deprecated)。

### 3.2 实施

新增 `lifespan` 异步上下文管理器:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """ADR-065: FastAPI lifespan 后台调度器"""
    from .memory_forgetting import FORGETTING_CHECK_INTERVAL_HOURS

    async def forgetting_loop():
        scheduler = memory_service.forgetting_scheduler
        print(f"[ADR-065] forgetting loop started, interval={FORGETTING_CHECK_INTERVAL_HOURS}h", flush=True)

        # 首次延迟 30 min (避免服务冷启动期间立刻执行)
        try:
            await asyncio.sleep(30 * 60)
        except asyncio.CancelledError:
            print("[ADR-065] forgetting loop cancelled during initial sleep", flush=True)
            return

        while True:
            try:
                result = scheduler.run_if_due()
                if result is not None:
                    dump = result.model_dump() if hasattr(result, "model_dump") else str(result)
                    print(f"[ADR-065] forgetting check ran: {dump}", flush=True)
            except Exception as e:
                print(f"[ADR-065] forgetting loop error: {e}", flush=True)

            # 间隔 N 小时后重试 (分 60 段 sleep, 利于响应取消)
            interval_seconds = FORGETTING_CHECK_INTERVAL_HOURS * 3600
            for _ in range(60):
                try:
                    await asyncio.sleep(interval_seconds / 60)
                except asyncio.CancelledError:
                    return

    task = asyncio.create_task(forgetting_loop())
    print(f"[ADR-065] forgetting background task scheduled (interval={FORGETTING_CHECK_INTERVAL_HOURS}h, first-run-after=30min)", flush=True)
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        print("[ADR-065] forgetting background task cancelled cleanly", flush=True)

app = FastAPI(..., lifespan=lifespan)
```

### 3.3 三个关键设计选择

| 选择 | 理由 |
|---|---|
| **首次延迟 30 min** | cortex 启动期间 (embedding 模型加载 + chromadb 初始化),避免 race condition |
| **分 60 段 sleep** | `await asyncio.sleep(interval/60)` x 60, 利于 service stop 时 cancel 立即响应 (不用等 24h) |
| **print() 不用 logger.info()** | FastAPI 0.135 logger 未接管 `src.api` 模块 logger, 直接 print 输出到 systemd journal |

### 3.4 修复时发现的副作用 bug

修改 api.py 时 patch 工具留下重复 import / 重复 lifespan 函数,导致:
- `NameError: name 'VERSION' is not defined`(因为我删了老 `_VERSION_FILE` 那块)
- 重复 `@asynccontextmanager` 导致 `app = FastAPI(lifespan=lifespan)` 时引用 ambiguous

修复方法: 用 `head -83 + tail -n +163` 拼接,重新整理顺序:
- import / VERSION / lifespan 在前
- `app = FastAPI()` 在中 (lifespan 引用 VERSION 已 OK)
- service init 在后 (`metadata_store = MetadataStore(...)` 等)

---

## 4. 验证

### 4.1 startup 验证 (重启后)

```
$ systemctl restart cortex
$ journalctl -u cortex | grep ADR-065
[ADR-065] forgetting background task scheduled (interval=24h, first-run-after=30min)
[ADR-065] forgetting loop started, interval=24h
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:1880
```

### 4.2 graceful shutdown 验证

```
$ systemctl stop cortex
$ tail /var/log/cortex.log | grep ADR-065
[ADR-065] forgetting loop cancelled during initial sleep
[ADR-065] forgetting background task cancelled cleanly
```

**确认**: service stop 时 lifespan finally 块 cancel task,**没有僵尸 task**.

### 4.3 端到端测试

| 端点 | before | after |
|---|---|---|
| `GET /health` | 200 healthy | ✅ |
| `GET /storage/health` | 200 healthy | ✅ |
| `POST /search` (F-1 修复) | 200 3 results | ✅ |
| `GET /list` | 200 811 chunks | ✅ |
| `POST /forgetting/cleanup` | 200 (手调) | ✅ |
| `GET /forgetting/stats` | last_run=null | ✅ (`08:47:49.817926` 触达) |
| `GET /provenance/{id}` | 200 (F-1) | ✅ |
| `GET /metrics` | Prometheus 格式 | ✅ |

### 4.4 自动调度触发预期 (首次 vs 后续)

| 时间 | 行为 |
|---|---|
| `t=0` (服务启动) | asyncio task 启动, print `[ADR-065] loop started` |
| `t=30 min` | scheduler.run_if_due() 首次执行 |
| `_last_forgetting_check = None` | `should_run()` 返回 True (因为 _last_run None), 执行 execute_forgetting_check |
| `_last_forgetting_check = utcnow()` | `should_run()` 下次返回 False |
| `t=30min + interval_hours` | 再次执行 |

---

## 5. ADR-066 candidate (后续 work)

### 5.1 持久化 last_run 跨 restart

**当前 bug**: `_last_forgetting_check` + `scheduler._last_run` 是 in-memory,**service restart 后丢失**,所以每次 restart 30min 后立即跑一次(浪费)。

**修复方向** (后续 sprint):

```python
# 在 metadata_store 新增 forgetting_log 表
CREATE TABLE forgetting_log (
    id TEXT PRIMARY KEY,
    last_run TEXT NOT NULL,  # ISO8601 datetime
    last_result TEXT          # JSON
)

# execute_forgetting_check 末尾:
def execute_forgetting_check(...):
    result = ...
    self.metadata_store.update_forgetting_log(datetime.utcnow(), result.model_dump())
    return result

# __init__ 启动时:
def __init__(...):
    ...
    log = self.metadata_store.get_forgetting_log()
    if log:
        self._last_forgetting_check = log.last_run
```

**估时**: 1-2 h

### 5.2 主动调度 storage_budget / taxonomy / reindex 等

**发现**: `storage_budget.check_and_enforce()` 是被动 API (`/storage/enforce`),`taxonomy` 完全无调度。
ADR-065 lifespan **可扩展**:
- storage_budget 每 1h 检查
- taxonomy 每 24h rebuild
- reindex 每 7d (如果 corpus 增量 >10%)

---

## 6. Traceability

| commit | 内容 |
|---|---|
| (本次) | feat(cortex): ADR-065 forgetting 自动调度 + 起停 lifespan |
| `c8d2e16` | docs(cortex): ADR-064 主文档 (前置) |
| `5501f71` | fix(cortex): ADR-064 search() metadata NPE (前置) |
| `2b0822d` | docs(cortex): Docker 部署指南 (前置) |
| `d54bca1` | feat(cortex): F-A1 sync 部署端 data+models (前置) |

---

## 7. 反思

| 项 | 评估 |
|---|---|
| 是否最小修复 | ✅ 是 (只改 api.py, lifespan 50 行) |
| 是否影响其他端点 | ✅ 不影响 (lifespan 只启动 background task, 不干预 API) |
| 是否覆盖 startup + shutdown | ✅ 双重 (lifespan context manager finally 块 cancel task) |
| 是否冷启动保护 | ✅ 30 min 延迟, 给 cortex warmup 时间 |
| 是否响应取消信号 | ✅ (分 60 段 sleep 利于 cancel 响应) |
| 是否写 ADR | ✅ (本文件) |
| 是否 commit + push | 🟡 待 push (同步后 commit) |
| 是否发现其他问题 | 🟡 ADR-066 持久化候选 |

按 V10.31 + 反思 — **主要 issue 修**, ADR-066 留给后续 sprint。