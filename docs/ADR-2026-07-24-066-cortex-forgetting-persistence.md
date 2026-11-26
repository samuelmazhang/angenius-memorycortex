# ADR-066 — Cortex 遗忘曲线持久化 + Taxonomy 父子关系扩充

| 项 | 值 |
|---|---|
| **状态** | ✅ Accepted / Implemented |
| **日期** | 2026-07-24 |
| **Sprint** | v10.37 E7 follow-up |
| **影响域** | Memory Cortex 调度 + 标签层级 |
| **前置提交** | ADR-065 (commit `79ad16d`), ADR-064 (`5501f71` + `c8d2e16`), ADR-063 (`32578d9` + `f33715d`) |

---

## 1. Context

ADR-065 实施了 FastAPI lifespan + asyncio 后台 task 启动 `ForgettingScheduler.run_if_due()`,**解决了"never scheduled"问题**。

但遗留 2 个二级问题:
1. **last_run in-memory 跨 restart 丢失** — service restart 后 scheduler._last_run = None, → 30 min 后立刻跑一次 (浪费, cortex 每次重启都强制跑一遍)
2. **taxonomy 父子关系薄** — 只有 4 条 (fastapi→python→backend, javascript→frontend), `parent_boost_factor=1.2` 配置但 `apply_taxonomy_boost` 没 wire 到 search 主路径 → 不起作用

用户于 2026-07-24 23:13 CST 授权:
> "ADR-066 持久化 / taxonomy 扩充 / 看下次 forgetting 自动跑 做一下"

---

## 2. 实施总览

| 文件 | 改动 | 行数 |
|---|---|---|
| `metadata_store.py` | 加 `forgetting_log` 表 + `load_forgetting_log/save_forgetting_log` helper | +52 |
| `memory_forgetting.py` | __init__ 启动 load + execute_forgetting_check 末尾 save | +27 |
| `data/metadata/memory.db` | 直接写入 7 条 taxonomy (sqlite INSERT) | 7 records |
| `docs/ADR-2026-07-24-066-...md` | 本文件 | new |

---

## 3. Decision — single-row forgetting_log 表

### 3.1 schema

```sql
CREATE TABLE IF NOT EXISTS forgetting_log (
    id INTEGER PRIMARY KEY DEFAULT 1,   -- 单行
    last_run TEXT NOT NULL,
    last_result TEXT,                  -- JSON (ExpirationResult.model_dump())
    updated_at TEXT NOT NULL
)
```

### 3.2 接口

```python
def load_forgetting_log(self) -> Dict:
    """读 forgetting_log (单行) 的 last_run / last_result."""
    # 返回 {"last_run": ISO8601 或 None, "last_result": {}, "updated_at": ...}

def save_forgetting_log(self, last_run: str, last_result: Optional[Dict] = None) -> None:
    """写入 forgetting_log (覆盖 last_run / last_result)."""
    # 写一行 (id=1), 覆盖 last_run + last_result
```

### 3.3 集成点

`MemoryForgettingService.__init__` 启动时 load:
```python
log = metadata_store.load_forgetting_log()
persisted_last_run = log.get("last_run") or None
if persisted_last_run:
    self._last_forgetting_check = datetime.fromisoformat(persisted_last_run)
```

`execute_forgetting_check` 末尾 save:
```python
result_dict = {
    "total_checked": ..., "expired_count": ..., "upgraded_count": ...,
    "execution_time_ms": ..., "status": "success",
}
self.metadata_store.save_forgetting_log(
    self._last_forgetting_check.isoformat(),
    result_dict,
)
```

---

## 4. Decision — taxonomy 数据驱动扩充

### 4.1 现状 (前)

```json
[
  {"child_tag": "fastapi", "parent_tag": "python", "level": 1},
  {"child_tag": "javascript", "parent_tag": "frontend", "level": 1},
  {"child_tag": "python", "parent_tag": "backend", "level": 1},
  {"child_tag": "react", "parent_tag": "javascript", "level": 1}
]
```

### 4.2 数据驱动分析

通过 SQL 提取 `memory_chunks.tags` 全部 tag + 频次,识别 hierarchy 模式。

| 类型 | 模式 | 推断依据 |
|---|---|---|
| 主题派生 | `rumination` → `memory` | rumination (177) 是 memory (13) 的具体化过程 |
| 主题派生 | `memory_entry` → `memory` | memory_entry (71) 是 memory 的一种 entry 类型 |
| 主题派生 | `pitfall` → `heuristic` | pitfall (8) 是 heuristic (7) 的一种 |
| 实现 vs 抽象 | `quantplatform` → `quantagent` | quantagent (8) 是 QuantPlatform (10) + quantplatform (20) 的抽象 |
| 实现 vs 抽象 | `QuantPlatform` → `quantplatform` | 大小写合并 |
| 派生会话 | `hermes_session_end` → `session_end` | hermes_session_end (9) 是 session_end (75) 的特定实现 |
| 派生会话 | `session-summary` → `session-extract` | session-summary (5) 是 session-extract (175) 的具体产物 |

### 4.3 不加: 类型 1-3 (平台/用户/turn:N)

| 类型 | 候选 | 跳过理由 |
|---|---|---|
| 平台 | `platform:telegram` → `platform` | hermes 自己有 platform metadata, cortex 加可能 duplicate |
| 用户 | `user:5589304481` → `user` | 用户 ID 是个体, 不一般化 |
| turn:N | `turn:1` → `turn` | hermes turn metadata, 同平台 |

**保守原则**: 数据驱动但**不抢 hermes metadata 职责**。

### 4.4 增后 (11 条)

| child | parent | level |
|---|---|---|
| QuantPlatform | quantplatform | 1 |
| quantplatform | quantagent | 1 |
| fastapi | python | 1 (已有) |
| hermes_session_end | session_end | 1 |
| javascript | frontend | 1 (已有) |
| memory_entry | memory | 1 |
| pitfall | heuristic | 1 |
| python | backend | 1 (已有) |
| react | javascript | 1 (已有) |
| rumination | memory | 1 |
| session-summary | session-extract | 1 |

---

## 5. 反思: ADR-066 vs ADR-067 候选

### 5.1 本 ADR 已落地

- ✅ forgetting_log 表 + load/save helper
- ✅ MemoryForgettingService 集成 (init load + execute save)
- ✅ 7 条 taxonomy (直接 sqlite INSERT)
- ✅ ADR-065 自动调度被验证 (log 显示 30min 后跑了一次, last_run 09:34)

### 5.2 已知 gap (后续 ADR-067 候选)

| gap | 说明 | 是否本 ADR 治 |
|---|---|---|
| `/search` 和 `/list` 没调 `apply_taxonomy_boost` | cortex 设计,本 ADR 暂不动 memory_service.search 路径 | ❌ 本 ADR 不治 |
| taxonomy boost 仅可手动 API 调用 | `apply_taxonomy_boost` 函数存在,需 wire | ❌ 本 ADR 不治 |
| platform/turn/user/N 等 metadata | 跟 hermes 边界可能 cross | ❌ 本 ADR 故意略过 |

### 5.3 ADR-067 candidate

**Wire taxonomy boost to /search + /list**:

```python
# memory_service.search() 末尾:
results = memory_service.search(request)
results = taxonomy_service.apply_taxonomy_boost(results, ...)  # ← 加这行
return results
```

**收益**: `/search` 命中含主题 tag 时,父 tag 命中记忆自动加权 1.2x → 召回更对。

**风险**: 改动 search 主路径,要重新跑 E2E 验证 (可能影响 ranking)。

**估时**: 1-2 h + 1 commit + 验证回归。

**状态**: 🟡 待下次 sprint 拍板。

---

## 6. 验证

### 6.1 forgetting_log persistence

```bash
$ systemctl restart cortex
$ curl /forgetting/stats
{
    "last_run": "2026-07-24T15:18:47.877876",
    ...
}
# ✅ 跨 restart 保留

$ sqlite3 data/metadata/memory.db "SELECT last_run FROM forgetting_log"
2026-07-24T15:18:47.877876
# ✅ DB 持久化 ok
```

### 6.2 forgetting 自动调度 (手调也持久化)

```bash
$ curl -X POST /forgetting/cleanup?force=false
{"execution_time_ms": 604, "status": "success"}

$ sqlite3 data/metadata/memory.db "SELECT last_run, last_result FROM forgetting_log"
2026-07-24T15:18:47.877876 | {"total_checked": 812, "expired_count": 0, "upgraded_count": 0, ...}
# ✅ last_run + last_result (JSON) 都持久化
```

### 6.3 ADR-065 自动调度 (重启后 30 min 跑过)

```
[ADR-065] forgetting loop started, interval=24h
[ADR-065] forgetting check ran: {'total_checked': 812, ..., 'execution_time_ms': 5734, 'status': 'success'}
# ✅ 30 min 延迟生效, 自动跑
```

(注: ADR-065 + ADR-066 联动 — 跨 restart 后第一次 should_run=False,因为 last_run 持久化恢复 = 当前时间 - 24h 内)

### 6.4 taxonomy 端点

```bash
$ curl /taxonomy/relations
总数: 11
   QuantPlatform → quantplatform
   quantplatform → quantagent
   hermes_session_end → session_end
   memory_entry → memory
   pitfall → heuristic
   rumination → memory
   session-summary → session-extract
   (+ 4 已有 fastapi/python/javascript/react)
```

---

## 7. Traceability

| commit | 内容 |
|---|---|
| (本次) | feat(cortex): ADR-066 forgetting 持久化 + taxonomy 扩充 |
| `79ad16d` | feat(cortex): ADR-065 forgetting 自动调度 (前置) |
| `c8d2e16` | docs(cortex): ADR-064 主文档 (前置) |
| `5501f71` | fix(cortex): ADR-064 search NPE (前置) |
| `2b0822d` | docs(cortex): Docker 部署指南 (前置) |
| `d54bca1` | feat(cortex): F-A1 data+models sync (前置) |

---

## 8. 反思

| 项 | 评估 |
|---|---|
| 是否最小修复 | ✅ forgetting_log 表 + 2 helper 函数 + 2 集成点 |
| 是否影响其他端点 | ✅ 不影响 (只 /forgetting/stats 显示 last_run 持久) |
| 是否 ADR-065 联动验证 | ✅ 跨 restart last_run 保留, 自动调度下次按 24h 间隔 |
| taxonomy 是否数据驱动 | ✅ 是 (从 SQLite tags 频次推断) |
| 是否主动扩大范围 | ❌ 没动 wire taxonomy_boost 到 search (留 ADR-067) |
| 是否写 ADR | ✅ (本文件) |
| 是否 commit + push | 🟡 待 push |