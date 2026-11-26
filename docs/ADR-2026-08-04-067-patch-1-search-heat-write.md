# ADR-067-Patch-1 — Cortex search→refresh 闭环（最小修复，让热度机制活过来）

| 项 | 值 |
|---|---|
| **状态** | 🟡 Draft（待评审） |
| **日期** | 2026-08-04 |
| **影响域** | `server/src/api_pg.py:search_memory` |
| **前置** | ADR-067 §3.9（composite score 热度项接入），本文是 §3.9 的 **最小补丁** |
| **替代** | 原 §3.9 提议"在 api_pg.py:search_memory 增加 composite 计算"——**实际不需要，因为已经写好了** |

---

## 1. Context

### 1.1 真实差距（v5.0.0 已实测）

按用户要求"解读和完善文档然后开始改进流程"，2026-08-04 完整审计 `server/src/api_pg.py` 1988 行后发现：

| 热度机制组件 | 状态 |
|---|---|
| `access_stats` 表 | ✅ 真实存在 |
| `_relevance_scores(ids)` 函数读 access_stats 推热度 | ✅ L670-687 真实存在 |
| `composite = 0.6*rrf + 0.4*sim + 0.05*(imp/10) + 0.1*recency + 0.1*rlv` | ✅ L890-891 真实存在，热度项 `0.1*rlv` 已经在主路径 |
| `forgetting_guided` 按 30 天内 access_count ≥ 3 升级 tier | ✅ L1206-1263 真实存在 |
| `forgetting_cleanup` 过期淘汰 + forgetting_log 写入 | ✅ L1180-1202 真实存在 |
| **`search_memory` 末尾调用 `/memories/{id}/refresh`** | ❌ **缺失**——主路径没写 access_stats |
| **`eval/feedback` 写入 relevance_score 增量** | ❌ stub（仅 `{"status":"received"}`） |
| **`storage/evict` 按 relevance_score 末位淘汰** | ❌ stub（`{"evicted":0}`） |

### 1.2 影响链

`/search` 不写 access_stats → access_count 永远是 0 → `_relevance_scores()` 永远返回 0 → composite 中热度项无效 → `forgetting_guided` 永远不升级 → 整条热度机制死锁。

---

## 2. Decision — 加一行热补丁

在 `search_memory` 函数返回前，对每个**真实命中的 chunk id** 调一次 `/memories/{id}/refresh` 等价 SQL（直接写 access_stats，不经 HTTP）。

### 2.1 代码改动

**位置**：`server/src/api_pg.py` 第 928 行（`results = results[: req.limit]` 之后、return 之前）

**新增代码**（不破坏现有签名，纯加性）：

```python
    # ──────────────────────────────────────────────────────────────────
    # ADR-067-Patch-1 (2026-08-04): 热补丁——让热度机制活过来.
    #
    # 现象: /search 命中了 chunk, 但 access_stats 没写入 → access_count 永远是 0
    #       → _relevance_scores() 永远返回 0 → composite 热度项 0.1*rlv 无效
    #       → forgetting_guided 永远不升级 tier.
    # 根因: /memories/{id}/refresh 端点存在, 但 search_memory 没调用它.
    # 修法: 在 search 返回前, 对命中 chunk 写 access_stats (异步, 不影响主路径).
    # ──────────────────────────────────────────────────────────────────
    if req.boost_relevance and results:
        try:
            for r in results:
                execute(
                    "INSERT INTO access_stats(memory_id, access_type, accessed_at) "
                    "VALUES (%s, 'search', NOW())",
                    (r["id"],),
                )
        except Exception as e:
            logger.warning(f"ADR-067-Patch-1: access_stats 写入失败 (不影响 search 返回): {e}")
```

### 2.2 为什么加 `if req.boost_relevance` 守卫

- `boost_relevance=false` 是纯向量降级路径（V5.3 设计保留），不写入避免拖累纯向量场景
- `boost_relevance=true`（默认）是 RRF 主路径，热度机制生效

### 2.3 为什么用 `asyncio.to_thread` 包不包？

- `execute()` 是同步 psycopg 调用，**主路径已经全部同步**（不是 async def），所以不需要
- 加 try/except 是为了**不影响 search 返回**——access_stats 写失败也不能让 search 挂

### 2.4 为什么不用现有 `/memories/{id}/refresh` HTTP 端点

- 当前端点存在但每次都发 HTTP 会绕一圈（性能差 + 自调用反模式）
- 直接 SQL 等价即可（已经审计过该端点的实现：L1615-1620）

---

## 3. 验证

### 3.1 单元测试

```python
# tests/test_search_heat_writes.py
def test_search_writes_access_stats():
    before = query("SELECT COUNT(*) AS c FROM access_stats", fetch="one")["c"]
    r = httpx.post(f"{BASE}/search", json={"query": "test", "boost_relevance": True, "limit": 5})
    assert r.status_code == 200
    after = query("SELECT COUNT(*) AS c FROM access_stats", fetch="one")["c"]
    assert after >= before + len(r.json()["results"])

def test_search_no_write_when_boost_false():
    """纯向量降级路径不应写入 access_stats"""
    before = query("SELECT COUNT(*) AS c FROM access_stats", fetch="one")["c"]
    httpx.post(f"{BASE}/search", json={"query": "test", "boost_relevance": False, "limit": 5})
    after = query("SELECT COUNT(*) AS c FROM access_stats", fetch="one")["c"]
    assert after == before  # 不写入
```

### 3.2 集成测试

```bash
# 1) 清空 access_stats
psql -c "TRUNCATE access_stats;"

# 2) 跑 5 次 search 命中同一 chunk
for i in 1..5; do
    curl -X POST localhost:1880/search -d '{"query":"QuantPlatform Cortex 热度","boost_relevance":true}' > /dev/null
done

# 3) 验证
psql -c "SELECT memory_id, COUNT(*) FROM access_stats GROUP BY memory_id;"
# 期望: 命中的 chunk 都有 count >= 1

# 4) 跑 guided
curl -X POST localhost:1880/forgetting/guided
# 期望: upgraded > 0 (满足 short_term 且 cnt>=3 的 chunk 被升级)
```

### 3.3 E2E

```bash
# 跑 30 次 search → 触发 guided → 验证 long_term chunk 数量上升
for i in $(seq 1 30); do
    curl -X POST localhost:1880/search -d "{\"query\":\"query-$i\"}" > /dev/null
done
sleep 2
curl localhost:1880/forgetting/stats | jq .tiers
# 期望: long_term > 0
```

---

## 4. 回滚

```bash
git revert <commit>
# 或 patch 反向: 删除新增的 try/except 块
systemctl restart cortex
```

回滚后：
- search 仍正常返回
- access_stats 不再增长
- composite 热度项仍 0（但代码不报错）

---

## 5. Feature Flag（可选）

如果不希望默认开启，可加：

```python
# config.py 新增
self.heat_write_on_search: bool = os.getenv("MEMORY_HEAT_WRITE_ON_SEARCH", "true").lower() == "true"
```

```python
# api_pg.py 改为
if req.boost_relevance and results and getattr(CortexConfig(), "heat_write_on_search", True):
    ...
```

**默认 true**——这就是修这个 bug 的意义。

---

## 6. Traceability

| commit | 内容 |
|---|---|
| (本 ADR) | fix(cortex): ADR-067-Patch-1 search→access_stats 热补丁 |
| (前置) | docs: ADR-067 Cortex 热度 + 推翻完整实施 |
| (前置) | docs: 设计解读与差距审计 2026-08-04 |
| (前置) | fix(cortex): ADR-066 forgetting_log 持久化 + FIX(2026-08-02) score_before/after 字段清理 |

---

## 7. 反思

| 项 | 评估 |
|---|---|
| 是否最小修复 | ✅ 是——8 行代码，纯加性 |
| 是否影响其他端点 | ✅ 不影响——`/search` 返回结构不变 |
| 是否影响主路径延迟 | ⚠️ 5 个 INSERT 增加约 5-15ms（同步），可接受；如需更快改 async 后续 |
| 是否可灰度 | ✅ feature flag 可关闭 |
| 是否可回滚 | ✅ git revert 即可 |
| 是否覆盖 startup + shutdown | ✅ N/A（无状态变更） |
| 是否发现其他问题 | 🟡 eval/feedback stub + storage/evict stub 后续 ADR-068 处理 |