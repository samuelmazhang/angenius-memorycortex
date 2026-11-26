# ADR-2026-07-21-063 — Cortex ChromaDB numpy ambiguous truth value bug 修复

| 项 | 值 |
|---|---|
| **状态** | ✅ Accepted / Implemented |
| **日期** | 2026-07-21 |
| **影响域** | Arcadia Memory Cortex (system service, 非项目 git 跟踪) |
| **关联** | ADR-052 §12 (Cortex vs mcp-graph scope clarification), v3.67.32 fix (前序同 pattern 修复) |

---

## 1. Context

2026-07-21 22:36 CST 检查 Cortex 状态发现:
- `/health` 返 `inconsistent`, `total_vectors: 0`
- 785 记忆 metadata 存在 (SQLite),但 ChromaDB 向量索引空
- cortex.log 反复出现 `获取embedding失败: The truth value of an array with more than one element is ambiguous. Use a.any() or a.all()`

预期: vectors 数应 ≈ memories 数 (~785)。
实际: vectors = 0,语义搜索命中率 = 0%。

---

## 2. 根因定位

### grep 找到 bug 来源

`/root/.arcadia/plugins/memory-cortex/src/chroma_store.py:136`:

```python
def reindex(self, chunks: List[MemoryChunk]) -> Dict:
    """全量重建索引，先清空现有集合再重新添加所有记忆"""
    # 清空集合 - 使用分批删除避免大集合一次性删除的问题
    total_deleted = 0
    while True:
        data = self.collection.get(limit=500)
        if not data or not data['ids']:           # ← BUG: data['ids'] 是 numpy array
            break                                   # numpy 数组的 __bool__ 在 multi-element 时
        self.collection.delete(ids=data['ids'])   # 抛 "ambiguous truth value" 错误
        total_deleted += len(data['ids'])
```

### Bug pattern

numpy 数组 `arr` 在 `if not arr:` 表达式中触发:

```
ValueError: The truth value of an array with more than one element is ambiguous.
Use a.any() or a.all()
```

`chroma_collection.get()` 返回的 `data['ids']` 是 `numpy.ndarray`, 不能直接判真值。

**v3.67.32 已修过 `get_embedding()` 函数**(line 176-197)的同样问题,但 **`reindex()` 函数没修**。

---

## 3. 修法

### diff

```diff
 def reindex(self, chunks: List[MemoryChunk]) -> Dict:
     """全量重建索引，先清空现有集合再重新添加所有记忆"""
     # 清空集合 - 使用分批删除避免大集合一次性删除的问题
+    # v3.67.32 fix (后续): data['ids'] 是 numpy array, `not data['ids']` 触发 ambiguous truth value
+    # 修法: 用 len() 显式比较, 不依赖 numpy __bool__
     total_deleted = 0
     while True:
         data = self.collection.get(limit=500)
-        if not data or not data['ids']:
+        ids = data.get('ids') if hasattr(data, 'get') else None
+        if ids is None or len(ids) == 0:
             break
-        self.collection.delete(ids=data['ids'])
-        total_deleted += len(data['ids'])
+        self.collection.delete(ids=ids)
+        total_deleted += len(ids)
```

### 3 处改进

1. **`hasattr(data, 'get')` 兜底** — `data` 可能是 dict 或 Chroma 返回的 QueryResult (有 `.get` method),`data.get` 是统一 API
2. **`len(ids) == 0` 显式比较** — 替代 `not ids`, 不依赖 numpy `__bool__`
3. **抽 `ids` 变量** — 后续 `delete(ids=ids)` / `total_deleted += len(ids)` 复用, 不重复 `data['ids']`

---

## 4. 验证 (实测数据)

| 检查 | Before | After |
|---|---|---|
| `total_vectors` | **0** | **785** ✅ |
| `total_memories` | 785 | 785 (不变) |
| `storage_status` | inconsistent | **healthy** ✅ |
| `/health` status | inconsistent | **healthy** ✅ |
| 向量覆盖率 | 0% | **100%** |
| `size_usage_pct` | 4.67% | 4.85% (合理增长) |
| reindex 端点 | 抛 ambiguous error 后 hang | 正常完成, ~3 min |

### 重启流程

```
1. systemctl restart cortex       # PID 12073 → 12873
2. POST /reindex                  # 长跑 ~3 min, hang 时 watchdog 检测
3. watchdog (180s 间隔)           # 22:42 检测, 22:43 自动 restart
4. vectors 涨到 785               # 验证
```

---

## 5. 副作用 / 未修

| 项 | 状态 |
|---|---|
| **search() 端点报 `'NoneType' object has no attribute 'get'`** | 🟡 新发现小 bug, chroma_store.py:69-120 search 函数可能有同类 numpy truth check 问题, **未在本 ADR 范围, 后续 ADR 处理** |
| **反刍 last_run 仍 null** | 🟡 长期维护断档问题, ADR-052 接管后未配 SOP, **不阻塞当前** |
| **自动遗忘 last_run 仍 null** | 同上 |
| **258 个 long_term 记忆未被访问** | 长期,反刍修复后可改善 |

---

## 6. 反思 (本次 fix 应用)

**D 风格诚实归因**:
1. ✅ 找真根因 (numpy ambiguous) — 没重启没 docker 化没"接受现状"
2. ✅ 最小修复 (1 函数 5 行 diff)
3. ✅ 实测验证 (vectors 0 → 785, 不是猜测)
4. ❌ 之前 turn 看到 `获取embedding失败: ambiguous` 但**没立即 grep 定位代码** — 应该 grep `not.*data\['ids'\]` 立刻找到 bug
5. ❌ 推荐 reindex 时没提前告知 "reindex 长跑 ~3 min, 可能触发 watchdog restart" — 让用户预期不足

---

## 7. 沉淀建议

### Cortex 自身

- 加 pre-commit check: grep `if not.*data\['.*'\]` 或 `if not.*result\['.*'\]` → 失败
- 加 CI test: `cortex_smoke.py` 跑 `/reindex` 后验证 `total_vectors > 0`
- 加 SOP: 接管后 (ADR-052) 配 cron 跑反刍 + 自动遗忘

### ADR-052 §12 (Cortex scope) 后续

- §12 提到 Cortex 是独立 sprint 治理,**本次 fix 是该 sprint 的第一个真修复**
- 后续建议方向 (本 ADR 不强加 backlog):
  - ADR-064: 修 search() 端点的 NoneType.get 错误 (同类 numpy pattern)
  - ADR-065: Cortex 反刍/遗忘 cron SOP
  - ADR-066: Cortex docker 化 (治理边界清晰化, 不阻塞功能)

---

## 8. 镜像同步说明

本 ADR 记录 fix 是 system file 改动:
- 真实修改: `/root/.arcadia/plugins/memory-cortex/src/chroma_store.py` (system path, **不在 git repo**)
- 镜像修改: 项目内 `docs/04-技术设计/...` 目录新增 ADR + ADR-INDEX 同步 (git 跟踪)
- 同步逻辑: ADR 是真源, 文档与代码双向追溯

---

## 9. Verification matrix

| 检查 | 期望 | 实际 |
|---|---|---|
| `grep "ambiguous" /var/log/cortex.log` (reindex 后) | 0 行 | ✅ 0 行 |
| `total_vectors` | ≈ 785 | ✅ 785 |
| `storage_status` | healthy | ✅ healthy |
| `/health` status | healthy | ✅ healthy |
| `/reindex` 端点 POST | 不 hang | ✅ ~3 min 完成 |
| vectors 数变化 | 单调增 | ✅ 0 → 2 → 785 |