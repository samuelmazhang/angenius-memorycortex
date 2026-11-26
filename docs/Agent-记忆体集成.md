# Agent 记忆体集成 (arcadia ↔ cortex)

> 适用范围：V5.2.16 – V5.2.23，把 arcadia-agent 记忆层与 cortex 从"高级 KV 数据库"
> 升级为"真正的 agent 记忆层"。本文记录三层记忆机制、agent 可调用的新能力、
> 生命周期治理、以及日常维护。

---

## 1. 三层记忆模型

cortex 的记忆分级基于 **importance 驱动的 TTL**，不是独立的存储类。`importance`
在写入时决定 `tier` 与 `expires_at`（唯一真源：`api_pg.py._tier_for_importance()`）。

| importance | tier        | expires_at            | 语义 |
|-----------|-------------|------------------------|------|
| 1 – 3     | short_term  | now + 7 天             | 会话 scratch，快速淘汰 |
| 4 – 5     | medium_term | now + 30 天            | 跨会话但非策展 |
| 6 – 7     | long_term   | now + 180 天           | 用户策展的持久事实 |
| 8 – 10    | long_term   | NULL（永不过期）       | permanent / never-forget |

**connector 侧的 tier 标签**（`memory_tool.py._TIER_TO_IMPORTANCE`）与此对齐：
`short→3, medium→5, long→7, permanent→9`，未知/省缺 → 7（long，默认）。

### 生命周期闭环
```
write (tier → importance → tier+TTL) 
  → re-grade (update_chunk 重算 tier+TTL, 只升不缩 / permanent 永驻)
  → recall (refresh 记录 access_stats)
  → guided (access_stats 高 → 自动升级下一 tier + 续 TTL)
  → overdue (cleanup 删除 expires_at 已过期的)
  → stats (forgetting/stats 真实 tier 计数)
```

### 关键语义保证
- **只升不缩**：`update_chunk` 重算 TTL 时，若新 importance 蕴含的过期时间早于现有的，
  保留现有（更晚）。升级到 permanent 清空 expires_at。
- **never-forget**：一旦记录是 permanent（expires_at NULL），再降级到任意非 permanent
  importance 都保持 NULL——降级绝不复活 TTL。permanent 一经设定即永久。
- **guided upgrade**：`/forgetting/guided` 读 access_stats（30 天窗口），对 ≥3 次召回
  的 short/medium chunk 提升到下一 tier 并续 TTL（再巩固）。permanent 不受影响。

---

## 2. Agent 可调用的记忆能力（memory_tool actions）

`memory` tool 的 `action` enum 在 V5.2.22 扩展：

| action            | 输入                                    | 作用 |
|-------------------|-----------------------------------------|------|
| `add`             | content, target, tier                   | 写入一条分层记忆 |
| `replace` / `remove` | old_text + content                  | 替换 / 删除 |
| `recall`          | content = 自然语言查询                  | **agentic recall**：hybrid search + 图谱推理，返回聚焦 recall pack（只含相关簇）|
| `save_procedure`  | old_text=trigger, content=action, target=name | 固化可复用流程为 procedural memory |
| `get_procedure`   | content = 任务描述                      | 取最佳匹配流程 |
| `deep_context`    | old_text = chunk_id                     | 6 维溯源（provenance/history/relations/hierarchy/links）|
| `audit_query`     | content = limit(int)                    | 读 audit 日志 |

`schema` 用 registry 的 `dynamic_schema_overrides` 动态注入 cortex live 能力
（`/endpoints/recommend/connector_memory`，30s 缓存，不可达时回退静态）。

---

## 3. 主动回忆注入（Direction 3）

`_load_from_cortex` 在会话启动时调 `reason_with_memory(question="recent context", top_k=5)`
并**保留**结果（V5.2.22 前是丢弃/仅暖缓存），把命中内容作为
「## Memory Recall Context」块追加到 system-prompt snapshot 的 memory 段。

→ agent 启动即带跨会话的相关上下文，而非只靠完整 MEMORY.md / USER.md 全量 dump。

---

## 4. 图谱自生长（Direction 2）

每次写入 chunk 后，`_add_to_cortex` 自动调 `backend.auto_link_new_chunk()`：
- 用 `search_hybrid(content, top_k=5)` 找最相似既有 chunk
- 对相似度 ≥ 0.30 且非自身的 top 3 建 Zettelkasten `related` 链接（strength = 相似度）

**历史教训**：旧钩子调 `backend.search(content, top_k=3, min_score=0.7)`（签名错配）且从
list 上读 `.get("chunks")`，每次抛异常被吞 → 图谱从未生长（1953 chunks 仅 110 links）。
V5.2.22 换用 `auto_link_new_chunk()` 修复，实测一次写入 0→3 links。

---

## 5. 跨轮对话记忆（Direction B 收尾，V5.2.21）

working memory 现在含两块：
- **scratch KV**：`working_memory` 表，`/working/set` + `/working/{sid}`。
- **对话**：`memory_conversations(session_id, seq, role, content)` 表，`/working/{sid}/append`
  的 `{role, content}` 路径写入（seq 单调递增）。之前此路径 501 stub。

`GET /working/{sid}/context` 把两块融合返回，供 LLM 恢复跨轮上下文。

---

## 6. 运行时 API（新增/改动端点）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/search` | POST | **v5.3.0**: 三路 RRF 混合检索（向量+字面+tag）+ 三因子重排，返回 composite_score/rrf_score/relevance_score |
| `/graph/mine` | POST | **v5.3.0**: 离线启发式图谱回填（相似度/共享标签/同来源），110→1394 relations |
| `/graph/khop` | GET | **v5.3.0**: k-hop 多跳子图遍历（GraphRAG 推理） |
| `/forgetting/guided` | POST | 引导遗忘：access_stats 驱动 tier 升级（V5.2.22 D4） |
| `/forgetting/cleanup` | POST | 删除过期 chunk（V5.2.17/18 修复后可真删） |
| `/forgetting/stats` | GET | 真实 tier 计数（`GROUP BY tier`） |
| `/procedural/search` | GET | 接受 `?q=` 或 `?trigger=`（V5.2.23 修复） |
| `/procedural` (POST) | — | 接受 trigger/action 字段（V5.2.23） |
| `/working/{sid}/append` | POST | `{role, content}` 对话路径（V5.2.21） |
| `/working/{sid}/context` | GET | working + conversation 融合（V5.2.21） |
| `/endpoints/recommend/connector_memory` | GET | 动态能力发现（V5.2.16 A） |

新增 PG 表：`memory_conversations`（V5.2.21）。

---

## 7. 定时维护（cron）

每天自动跑 引导遗忘 + 清理，让三层生命周期自维护：

```bash
# VM: /opt/cortex-deploy/cortex-memory-maintenance.sh (chmod 700)
#   1) POST /forgetting/guided   (access 驱动 tier 升级)
#   2) POST /forgetting/cleanup  (剔除过期 chunk)
# cron (<user>, 每日 04:30):
sudo crontab -u <user> -l
#   30 4 * * * /opt/cortex-deploy/cortex-memory-maintenance.sh >> /opt/cortex-deploy/memory-maintenance.log 2>&1
```

手动安全触发（均幂等）：
```bash
curl -X POST -H "Content-Type: application/json" -d '{}' http://127.0.0.1:1880/forgetting/guided
curl -X POST -H "Content-Type: application/json" -d '{}' http://127.0.0.1:1880/forgetting/cleanup
```

---

## 8. 验证清单（端到端，V5.2.22/23 全绿）

- 写 4 档 tier → 各自 tier+TTL 正确（short 7d / medium 30d / long 180d / permanent NULL）
- re-grade 升档 → tier+TTL 跟随、只升不缩
- permanent 降级 → 保持 NULL（never-forget）
- 写 chunk → auto-link 自动建 3 条 related 链接
- `recall` → hybrid 10 hits + reason 图谱 chunks
- `save_procedure` → `get_procedure` 往返可查（trigger 搜索）
- `/forgetting/guided` + `/forgetting/cleanup` → 200、幂等
- 三层 lifecycle：写 short → force-expire → cleanup 删除，permanent 存活
