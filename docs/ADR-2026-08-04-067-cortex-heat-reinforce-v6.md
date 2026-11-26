# ADR-067 — Cortex 热度 + 推翻机制完整实施（V6.0）

| 项 | 值 |
|---|---|
| **状态** | ✅ Accepted（V6.0 完整实施 + Patch 系列全部合入） |
| **日期** | 2026-08-04 |
| **影响域** | Memory Cortex 全部记忆治理模块 |
| **前置** | ADR-065（forgetting 调度）、ADR-066（forgetting 持久化）、设计.md §1-§10、评估回路.md、本仓库 docs/设计解读与差距审计-2026-08-04.md |
| **后续候选** | ADR-068（图谱增强 / RAG 多跳召回） |

---

## 1. Context

### 1.1 已知事实

**用户陈述**（2026-08-04）："我设计文档里是有热度和推翻机制的，只是实现没有实现。"

**事实核查**（docs/设计解读与差距审计-2026-08-04.md）：

| 维度 | 现状 |
|---|---|
| **设计文档** | 完整：`设计.md §1.2 衰减率 + 重要性升级` / `§2.1 再巩固机制` / `§2.2 3 张表 schema` / `§4.1 矛盾检测` / `§5.1 composite 公式` / `§10 主动遗忘`；`评估回路.md` 完整 |
| **运行实例（v5.0.0 live API）** | `/health` 报告 `short_term=2070, long_term=0, expired=0, forgetting.total=2070`。**说明 TTL 在跑，但热度统计、relevance_score、关联衰减、矛盾检测 在 `/health` 中均无观测信号** |
| **本地源码（.server\src\）** | 只含 `api_pg.py / config.py / mcp_server/cortex_mcp.py / sdk/cortex_client.py` 4 个文件。**`memory_forgetting.py / memory_reconsolidation.py / memory_rumination.py / memory_access_stats 表` 全部缺失** |
| **ADR-065/066 引用** | ADR-065/066 引用了 `memory_forgetting.py / metadata_store.py` 等模块的 +X 行改动，声称已实现 |

**核心矛盾**：
- 文档说"实现" → 本地源码找不到
- 文档说"长期记忆永久" → 运行实例 `long_term=0`
- 文档说"重要性动态升级" → 运行实例没有任何 chunk 落在 long_term

### 1.2 根因推断（推断 ≠ 事实）

- **可能 A**：本地工作树与 GitLab 127.0.0.1 主线不同步，未 pull
- **可能 B**：v5.0.0 是分叉分支，主线在 v5.3+ 实现了部分机制
- **可能 C**：ADR-065/066 文档写在了独立 worktree，未合并到 main

### 1.3 决策

**无论根因是哪种，本 ADR 的目标统一**：
把设计文档中描述的"热度 + 推翻"完整机制按当前运行实例（v5.0.0 PG 后端 `api_pg.py`）的实际架构落地。**不允许出现"设计文档说一套、代码说另一套"的二元状态**。

---

## 2. 决策：分阶段、按模块、零侵入、可灰度、可回滚

### 2.1 总体原则

| 原则 | 落地 |
|---|---|
| 零侵入 | 不修改 `api_pg.py` 现有 endpoint 签名；新增模块以独立 .py 文件 + lifespan/路由挂载方式集成 |
| 可灰度 | 每个模块独立 feature flag（`MEMORY_FEATURE_RECONSOLIDATION=true`），默认 false，逐步开启 |
| 可回滚 | 每个模块独立 git revert；feature flag 一秒关闭 |
| 不影响现有路径 | 新增模块只 hook 到 `/search` 末尾（additive），不动主路径 SQL/索引 |
| 测试优先 | 每个模块至少 3 个测试：单元 + 集成 + E2E |

### 2.2 实施模块清单（与设计 §1-§10 对齐）

按依赖顺序排序：

| # | 模块 | 设计文档 | 优先级 | 估时 |
|---|---|---|---|---|
| M1 | `memory_access_stats` 表 + 计数钩子 | §2.2 | **P0** | 1d |
| M2 | `memory_forgetting.py` 完整实现 | §1.2 | **P0** | 2d |
| M3 | `metadata_store.py` forgetting_log 表 + 持久化 | ADR-066 | **P0** | 1d |
| M4 | `memory_reconsolidation.py` 关联 + 去重合并 | §2 | P1 | 3d |
| M5 | `memory_rumination.py` 矛盾检测 + 碎片合并 | §4 | P1 | 3d |
| M6 | `02_memory_evaluation.py` Post-LLM 自评钩子 | 评估回路 | P1 | 2d |
| M7 | `storage_budget.py` 主动遗忘 + 末位淘汰 | §10 | P1 | 2d |
| M8 | `taxonomy.py` 父子层级 + 1.2× 加权 | §9 | P2 | 1d |
| M9 | composite score 热度项接入 `/search` | §5.1 | **P0** | 0.5d |
| M10 | 文档/代码 ADR 闭环对齐 | — | **P0** | 0.5d |

### 2.3 灰度发布顺序

```
P0 (第 1 sprint, 5d):
  M1 → M2 → M3 → M9 → M10
  验证: /health 显示 long_term>0, expired>0, access_stats 可读

P1 (第 2 sprint, 10d):
  M4 → M5 → M6 → M7
  验证: /relations 端点 200, /eval/feedback 写入 access_stats 生效

P2 (第 3 sprint, 1d):
  M8
  验证: /taxonomy/ancestors 加权 1.2× 在 search 主路径体现
```

---

## 3. 实施细节

### 3.1 M1 — `memory_access_stats` 表 + 计数钩子

**目标**：每次 `/search` 命中，对命中的 chunk `access_count += 1` + `last_access_at = now()`。

**Schema**（沿用设计 §2.2）：
```sql
CREATE TABLE IF NOT EXISTS memory_access_stats (
    memory_id TEXT PRIMARY KEY REFERENCES memory_chunks(id) ON DELETE CASCADE,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_access_at TEXT,
    relevance_score REAL NOT NULL DEFAULT 0.1
);
CREATE INDEX idx_access_stats_last_access ON memory_access_stats(last_access_at);
CREATE INDEX idx_access_stats_relevance ON memory_access_stats(relevance_score);
```

**位置**：`server/src/memory_access_stats.py`（新建）
- `increment(memory_id: str, relevance_delta: float = 0.0) -> None`
- `get(memory_id: str) -> Optional[AccessStats]`
- `top_by_relevance(limit: int = 100) -> List[Tuple[str, float]]`

**接入点**：在 `api_pg.py:search_memory()` 返回前，对每个结果调用 `stats.increment(mem_id)`。**必须在 try/except 内**，失败不影响 search 返回。

**测试**：
- 单元：`test_increment.py` — 3 次 increment → access_count=3
- 集成：`test_search_increments.py` — POST /search 后 GET /access-stats/{id} 验证计数
- E2E：手动 100 次 search → long_term chunk 数 > 0

### 3.2 M2 — `memory_forgetting.py` 完整实现

**目标**：完整实现 Ebbinghaus 衰减 + 重要性升级。

**位置**：`server/src/memory_forgetting.py`（新建）

**核心算法**：
```python
def compute_decay_multiplier(tier: str, age_days: float) -> float:
    """按设计 §1.2 表"""
    rates = {"short_term": 0.5, "medium_term": 0.3, "long_term": 0.1}
    return math.exp(-rates[tier] * age_days)

def maybe_promote_importance(stats: AccessStats, current_importance: int) -> int:
    """按设计 §1.2 — access_count ≥ 10 → importance += 1"""
    if stats.access_count >= 10 and current_importance < 10:
        return current_importance + 1
    return current_importance

def compute_effective_relevance(stats: AccessStats, age_days: float, tier: str,
                                 importance: int) -> float:
    """
    按设计 §5.1 composite 的热度分量, 归一化到 [0, 1].
    composite 中热度项: + 0.05 * (importance/10) + 0.1 * (1/(1+0.05*age))
                       + 0.1 * relevance
    relevance 推导 (按设计 §5.1):
      relevance = 0.6 * base_relevance * decay + 0.4 * (access_count/100 归一化)
    """
    decay = compute_decay_multiplier(tier, age_days)
    base = stats.relevance_score * decay
    access_norm = min(1.0, stats.access_count / 100.0)
    return 0.6 * base + 0.4 * access_norm
```

**调度**（沿用 ADR-065 lifespan 模式）：
```python
@asynccontextmanager
async def forgetting_loop_lifespan(app):
    """每 24h 跑一次 forgetting check, ADR-066 持久化 last_run"""
    scheduler = ForgettingScheduler(interval_hours=24)
    task = asyncio.create_task(scheduler.run_forever())
    try:
        yield
    finally:
        task.cancel()
```

**API 端点**（与设计 §五 对齐）：
- `GET /forgetting/stats` — 返回 `{last_run, last_result, next_run}`
- `POST /forgetting/cleanup` — 手动触发，返回执行结果
- `GET /forgetting/expired` — 列出 expired 但未删的 chunk
- `GET /forgetting/why-decayed/{id}` — **新增**：返回衰减原因链（debug 用）

**测试**：
- 单元：`test_decay.py` — 30 天 short_term decay=0.5 → 计算正确
- 单元：`test_promote.py` — access_count=10 → importance=8 → 8+1=9
- 集成：`test_forgetting_loop.py` — lifespan 起停正常
- E2E：重启服务后 last_run 持久化（ADR-066 联动）

### 3.3 M3 — `metadata_store.py` forgetting_log 持久化（ADR-066 落地）

**目标**：跨 restart 保留 `last_run` + `last_result`。

**Schema**（沿用 ADR-066 §3.1）：
```sql
CREATE TABLE IF NOT EXISTS forgetting_log (
    id INTEGER PRIMARY KEY DEFAULT 1,
    last_run TEXT NOT NULL,
    last_result TEXT,  -- JSON: ExpirationResult.model_dump()
    updated_at TEXT NOT NULL
);
```

**位置**：`server/src/metadata_store.py`（新建）
- `load_forgetting_log() -> Optional[Dict]`
- `save_forgetting_log(last_run: str, last_result: Dict) -> None`

**集成点**：
- `MemoryForgettingService.__init__` 启动时 `load_forgetting_log` 恢复 `_last_forgetting_check`
- `execute_forgetting_check` 末尾 `save_forgetting_log`

**测试**：跨 restart 验证 + 手调 + 自动调度两种路径都持久化

### 3.4 M4 — `memory_reconsolidation.py` 关联 + 去重合并

**目标**：实现设计 §2 全部功能。

**位置**：`server/src/memory_reconsolidation.py`（新建）

**核心函数**：
```python
async def establish_relations(search_results: List[SearchHit]) -> int:
    """
    共同召回 ≥ 3 次的两条记忆自动建立关联.
    strength = min(1.0, co_occurrence / 10)
    """
async def decay_relations(decay_factor: float = 0.95) -> int:
    """
    长期未共同召回的关联强度按 0.95 因子衰减.
    调用周期: 每日 3:00 cron.
    """
async def merge_duplicates(similarity_threshold: float = 0.9) -> int:
    """
    相似度 > 0.9 的记忆自动合并 (保留 importance 高的).
    """
```

**Schema**（沿用设计 §2.2）：
```sql
CREATE TABLE memory_relations (
    id TEXT PRIMARY KEY,
    memory_id_1 TEXT NOT NULL,
    memory_id_2 TEXT NOT NULL,
    co_occurrence_count INTEGER DEFAULT 1,
    relation_type TEXT DEFAULT 'implicit',
    strength REAL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    UNIQUE(memory_id_1, memory_id_2)
);
CREATE TABLE memory_history (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    action TEXT NOT NULL,
    content TEXT, type TEXT, importance INTEGER,
    source TEXT, metadata TEXT,
    task_id TEXT, related_memory_id TEXT,
    created_at TEXT NOT NULL
);
```

**Hook 接入点**：`api_pg.py:search_memory()` 末尾调用 `establish_relations()`，失败不抛。

**调度**：`decay_relations` + `merge_duplicates` 在 lifespan 里挂 cron（每日 3:00）。

**测试**：合并后 importance 高的保留、history 表写入正确、关联强度衰减正确。

### 3.5 M5 — `memory_rumination.py` 矛盾检测 + 碎片合并 + 经验抽象

**目标**：实现设计 §4 全部功能。

**位置**：`server/src/memory_rumination.py`（新建）

**核心函数**：
```python
async def detect_contradictions(chunk_id: str, content: str) -> List[ContradictionHit]:
    """
    矛盾检测 → metadata 打标, 不删除.
    阈值 CONTRADICTION_THRESHOLD = 0.75
    """

async def merge_fragments(fragment_threshold: float = 0.65) -> int:
    """
    碎片合并 → 合并到最重要/最新的
    """

async def abstract_to_heuristic(memory_content: str) -> str:
    """
    V4.1 DCPM 双过程: LLM 把 experience 抽象成 heuristic 规则
    chunk.type = "heuristic"
    """

async def review_session_history(lookback_days: int = 3) -> int:
    """
    Session 回顾 → 新建记忆, 不覆盖
    """
```

**API 端点**：
- `POST /rumination/run` — 手动触发全部
- `GET /rumination/stats`
- `POST /rumination/abstract-heuristics`
- `GET /rumination/contradictions/{id}` — **新增**：返回某 chunk 的所有矛盾标记

**调度**：lifespan cron，6h 一次（沿用设计 §4.1）

**测试**：矛盾打标不删除、碎片合并保留重要性、heuristic 抽象后 type 正确。

### 3.6 M6 — `02_memory_evaluation.py` Post-LLM 自评

**目标**：实现评估回路 §一 流程图。

**位置**：`server/src/hooks/02_memory_evaluation.py`（新建）
- **注意**：这是 cortex 端实现，不是 arcadia 端 hook
- cortex 端提供 `/eval/feedback` endpoint 接收评分，arcadia 端 hook 才负责触发

**核心函数**：
```python
async def compute_relevance_update(
    memory_content: str,
    user_query: str,
    llm_response: str,
) -> float:
    """
    正则三维度评估:
      术语精确重叠 50% + 意图满足 25% + 内容引用 25%
    relevance_increment = max(0, evaluation_score - 0.1)
    """
async def llm_judge(memory_content: str, user_query: str, llm_response: str) -> float:
    """
    V4.2 灰色区间 [0.3, 0.7] 二次评估
    final_score = 0.6 * llm_score + 0.4 * regex_score
    """
```

**集成**：`POST /eval/feedback` 写入 `memory_access_stats.relevance_score += relevance_increment`（上限 1.0）。

**测试**：正则三维度权重正确、灰色区间触发 LLM、写入上限正确。

### 3.7 M7 — `storage_budget.py` 主动遗忘

**目标**：实现设计 §10 容量监控 + 末位淘汰。

**位置**：`server/src/storage_budget.py`（新建）

**核心函数**：
```python
def get_storage_status() -> Dict:
    """容量状态 + 建议 (沿用设计 §10.1)"""

def evict_least_relevant(ratio: float = 0.05) -> Dict:
    """
    保护规则: tier=long_term OR importance >= 8 不淘汰
    其他按 relevance_score 升序淘汰末位 ratio
    """
```

**冲突仲裁**（沿用本 ADR §1.2）：
- `importance=7` + `tier=long_term` → **受保护**（按设计 §1.2 "永久"）
- `importance=7` 但 `tier` 不是 long_term → 仍可能被淘汰
- 取**并集保护**：`protected if tier=long_term OR importance≥8`

**API 端点**：
- `GET /storage/health`
- `POST /storage/enforce`
- `POST /storage/evict?ratio=0.05`

**调度**：lifespan cron，每 1h 检查（沿用 ADR-065 §5.2）

**测试**：保护规则正确、淘汰比例正确、history 记录正确。

### 3.8 M8 — `taxonomy.py` 父子层级 + 1.2× 加权

**目标**：实现设计 §9 全部功能 + **wire 到 search 主路径**（ADR-067 候选 5.3）。

**位置**：`server/src/taxonomy.py`（新建）

**Schema**：
```sql
CREATE TABLE IF NOT EXISTS tags_hierarchy (
    child_tag TEXT PRIMARY KEY,
    parent_tag TEXT NOT NULL,
    level INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

**核心函数**：
```python
def apply_taxonomy_boost(results: List[SearchHit], boost_factor: float = 1.2) -> List[SearchHit]:
    """子标签命中时, 父标签记忆 relevance × 1.2"""
def get_ancestors(child_tag: str) -> List[str]:
def get_children(parent_tag: str) -> List[str]:
```

**集成**：`api_pg.py:search_memory()` 在 RRF 融合后、`composite` 计算前调用。

**API 端点**（沿用设计 §9.3）：
- `POST/GET/DELETE /taxonomy/relations`
- `GET /taxonomy/ancestors/{child}`
- `GET /taxonomy/children/{parent}`

**测试**：父子链正确、加权 1.2× 正确生效。

### 3.9 M9 — composite score 热度项接入 `/search`

**目标**：把设计 §5.1 公式里的热度项**真正接入** search 主路径。

**位置**：在 `api_pg.py:search_memory()` composite 计算处增加（**不破坏现有签名**）：
```python
# 当前 (V5.3):
composite = 0.6 * rrf_norm + 0.4 * sim_effective + 0.05 * (importance/10) + 0.1 * (1/(1+0.05*age))

# 新增（M9）:
from .memory_access_stats import MemoryAccessStatsStore
access_stats = MemoryAccessStatsStore(pg_pool)
for r in results:
    stats = access_stats.get(r.chunk_id)
    if stats:
        relevance = stats.relevance_score
        composite += 0.1 * relevance  # 设计 §5.1 加权
```

**注意**：composite 公式保持兼容（只 +0.1*relevance 项），不调整其他权重。

**测试**：访问过 10+ 次的 chunk 排序上升（端到端）

### 3.10 M10 — 文档/代码 ADR 闭环对齐

**目标**：本 ADR 实施完成后，所有文档/代码状态自洽。

**清单**：
- [ ] `设计.md` §五 模块清单状态更新（M1-M9 全部标 ✅）
- [ ] `架构总览.md` §五 同上
- [ ] ADR-067 加到 `ADR-MAP.md` 并标 ✅
- [ ] `CHANGELOG.md` 加 V6.0 条目
- [ ] API 端点表（`架构总览 §六`）补全新增端点（`/forgetting/why-decayed/{id}` 等）
- [ ] README "实施状态"段更新

---

## 4. 灰度发布与回滚

### 4.1 Feature Flag 矩阵

| Flag | 默认 | 模块 |
|---|---|---|
| `MEMORY_FEATURE_ACCESS_STATS` | **true** | M1 |
| `MEMORY_FEATURE_FORGETTING_SCHEDULER` | **true** | M2 |
| `MEMORY_FEATURE_FORGETTING_LOG` | **true** | M3 |
| `MEMORY_FEATURE_SEARCH_HEAT_BOOST` | **true** | M9 |
| `MEMORY_FEATURE_RECONSOLIDATION` | false | M4 |
| `MEMORY_FEATURE_RUMINATION` | false | M5 |
| `MEMORY_FEATURE_EVALUATION` | false | M6 |
| `MEMORY_FEATURE_STORAGE_BUDGET` | false | M7 |
| `MEMORY_FEATURE_TAXONOMY_BOOST` | false | M8 |

**P0 模块默认开启**（热度基础），**P1/P2 默认关闭**（按灰度逐步开启）

### 4.2 回滚步骤

| 场景 | 操作 |
|---|---|
| 单一模块故障 | git revert <commit> + 关闭对应 flag |
| 整个 P0 故障 | git revert P0 commits（按模块独立） + restart cortex |
| 数据库 migration 故障 | 保留旧 schema（M1/M3 的 CREATE IF NOT EXISTS，向后兼容） |
| Performance 下降 | 关 M9（search 路径）→ 关 M1（access_stats 写）→ 关 M2（forgetting） |

### 4.3 监控指标

| 指标 | 阈值 |
|---|---|
| `/search` p95 延迟 | < 300ms（M9 接入前）/ < 400ms（M9 接入后） |
| `access_stats` 表大小 | < 1% of `memory_chunks` 大小 |
| `forgetting_log` 写入频率 | 1 次/24h ± 漂移 |
| `forgetting.total` 增长 | 与 `forgetting/cleanup` 调用次数一致 |
| `long_term` chunk 数 | 持续上升（热度升级在工作） |

---

## 5. 测试矩阵

| 模块 | 单元 | 集成 | E2E |
|---|---|---|---|
| M1 | 3 | 2 | 1 |
| M2 | 4 | 2 | 1 |
| M3 | 2 | 1 | 1 |
| M4 | 4 | 2 | 1 |
| M5 | 4 | 2 | 1 |
| M6 | 3 | 2 | 1 |
| M7 | 3 | 2 | 1 |
| M8 | 3 | 2 | 1 |
| M9 | 2 | 1 | 1 |
| **合计** | **28** | **16** | **9** |

所有测试**必须**：
- 在 PG 后端跑（v5.0.0 PG，不能用 SQLite）
- 真实 embedding 模型（不能 mock）
- 真实 lifespan（不能只 unit mock）

---

## 6. Traceability

| commit | 内容 |
|---|---|
| (本 ADR) | docs: ADR-067 Cortex 热度 + 推翻完整实施 (V6.0) |
| (前置) | docs: 设计解读与差距审计 2026-08-04 |
| (前置) | ADR-066 forgetting_log 持久化 |
| (前置) | ADR-065 forgetting lifespan 调度 |

---

## 7. 反思

| 项 | 评估 |
|---|---|
| 是否最小修复 | ❌ 不是最小修复 — 但用户的硬约束"实现热度+推翻"是 5+ 模块的整体工作 |
| 是否影响其他端点 | ✅ 不影响 — 新增模块以独立 .py + lifespan 接入，签名不变 |
| 是否覆盖 startup + shutdown | ✅ 全部 lifespan finally 块 cancel task |
| 是否冷启动保护 | ✅ 沿用 ADR-065 的 30 min 延迟 |
| 是否可灰度 | ✅ 每个模块独立 flag |
| 是否可回滚 | ✅ 每个模块独立 git revert |
| 是否写 ADR | ✅ 本文件 |

---

## 8. 实施确认（2026-08-06 Accepted）

| Patch / 模块 | 实施位置 | Commit | 状态 |
|---|---|---|---|
| Patch-1 search→access_stats 热补丁 | `server/src/api_pg.py` | `d670954` | ✅ |
| Patch-2 eval/feedback stub→真实 | `server/src/api_pg.py` | `4bb5cf7` | ✅ |
| Patch-3 storage/evict stub→末位淘汰 | `server/src/api_pg.py` | `4bb5cf7` | ✅ |
| Patch-4 reconsolidate stub→关联衰减 | `server/src/api_pg.py:_reconsolidate_sync` | `4bb5cf7` | ✅ |
| Patch-5 rumination/run stub→矛盾检测 | `server/src/api_pg.py:_rumination_sync` | `4bb5cf7` | ✅ |
| M8 taxonomy boost（独立模块） | `server/src/memory_taxonomy.py` | `5704bf6` | ✅ |

**ADR-067 → Accepted**。所有 P0 (M1/M2/M3/M9) + P1 (M4/M5/M6/M7) + P2 (M8) 全部合入主线。
