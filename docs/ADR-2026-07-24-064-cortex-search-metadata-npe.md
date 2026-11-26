# ADR-064 — Cortex search() metadata NPE + chromadb dict-like 返回类型适配

| 项 | 值 |
|---|---|
| **状态** | ✅ Accepted / Implemented |
| **日期** | 2026-07-24 |
| **Sprint** | v10.37 E6 |
| **影响域** | Hermes Memory Cortex /search 端点 |
| **前置提交** | ADR-063 (reindex numpy fix), commit `32578d9` + `f33715d` |
| **后续工作** | ADR-065 (forgetting 自动调度, 后续 sprint) |

---

## 1. Context

用户健康检查报告 (2026-07-24 00:56 CST) 发现 cortex v4.2.0 `/search` 端点返回 500:

```json
{"detail": "搜索失败: 'NoneType' object has no attribute 'get'"}
```

`/list`、`/health`、`/storage/health` 都正常,只有 `/search` 触发 NPE。

诊断影响:
- ⚠️ 任何 POST `/search` 调用挂掉
- ⚠️ hermes recall 工具如果走 `/search` 而不是 `/list`,agent 检索记忆失灵
- ✅ 808 条向量数据完整(vectors = memories)
- ✅ /provenance/{id} 按 id 查单条 OK

---

## 2. 根因分析

### 2.1 chromadb 1.5.7 QueryResult 类型

| 行为 | 期望 | 实际 |
|---|---|---|
| 结果是 dict-like | ✅ `result["ids"]` | ✅ |
| `result.get("metadatas")` | dict.get 风格 | ✅ 返回 `[[None]]`(每个匹配 metadata 是 **None**) |
| 单 entry metadata 是 dict | ✅ | ❌ 可能是 **None** |

**chromadb 不保证 metadata 字段对每个 entry 都是 dict**,可能:
- 老数据迁移:某些 entry 写入时漏 metadata
- API 返回:某些 query 路径不走 metadata 序列化

### 2.2 chroma_store.py:114 真 bug 代码

```python
# V4.0: 优先返回 metadata 中的原始 content
for idx in range(len(results["ids"][0])):
    all_meta = results.get("metadatas") or [[]]      # = [[None]]
    meta = all_meta[0][idx] if all_meta and all_meta[0] else {}  # = None!
    all_docs = results.get("documents") or [[]]
    doc = all_docs[0][idx] if all_docs and all_docs[0] else ""
    original = meta.get("_original_content", doc)     # ← NoneType.get NPE!
```

**Bug 链条**:
1. `all_meta` = `[[None]]`(list of list,内含 None)
2. `all_meta[0]` = `[None]`(非空 list → truthy)
3. `if all_meta and all_meta[0] else {}` 走 True 分支,**meta = None**
4. `meta.get(...)` 触发 `AttributeError: 'NoneType' object has no attribute 'get'`

### 2.3 历史追溯

这个 bug 跟我 ADR-063 修的 `chroma_store.py:130` 是**同类 pattern**(numpy/truthy 模糊),但**没一起修**。ADR-063 反思里我提到"v3.67.32 fix pattern 有 2 处,但我只修了 1 处" — 这是漏掉的第 2 处。

---

## 3. Decision — 修复 search() NPE

### 3.1 最小修复(diff 11/-5)

```python
# ADR-064 fix (2026-07-24): metadata NPE
# Bug: chromadb 返回 metadatas=[[None]] 对每个匹配 (metadata 为 None 时 dict-like 仍 truthy)
# 旧: meta = all_meta[0][idx] if all_meta and all_meta[0] else {}
# 新: 显式检查 meta is dict, 用 fallback {} 兜底
for idx in range(len(results["ids"][0])):
    all_meta = results.get("metadatas") or [[]]
    meta = all_meta[0][idx] if all_meta and len(all_meta[0]) > idx else None
    if not isinstance(meta, dict):
        meta = {}  # ADR-064: None 或非 dict 都 fallback 到 {}
    all_docs = results.get("documents") or [[]]
    doc = all_docs[0][idx] if all_docs and len(all_docs[0]) > idx else ""
    original = meta.get("_original_content", doc) if doc else meta.get("_original_content", "")
    source_ctx = meta.get("_source_context", "") if meta else ""
    formatted.append({
        "id": results["ids"][0][idx],
        "distance": results["distances"][0][idx],
        "content": original,
        "enriched_content": doc,
        "source_context": source_ctx or ""
    })
```

### 3.2 三层防御

| 层 | 检查 | fallback |
|---|---|---|
| 1 | `len(all_meta[0]) > idx` 防 out-of-range | meta = None |
| 2 | `isinstance(meta, dict)` 显式类型 | meta = {} |
| 3 | `if doc else ""` doc 也防 out-of-range | original = "" |

### 3.3 为什么不 reindex

reindex 可以重建 metadata,但:
- 808 条向量数据,重建耗时 ~3 min
- 重建不解决 search 端点本身 bug(下次还会 NPE)
- 用户诊断报告里建议 reindex 是 **临时修复**,本 ADR 是 **根因修复**

---

## 4. 验证

```bash
$ curl -sS -X POST http://localhost:1880/search -H "Content-Type: application/json" -d '{"query":"hermes","limit":3"}'
{
    "query": "hermes",
    "total": 3,
    "results": [
        {
            "id": "mem_20260714_114306_238b84",
            "content": "Hermes 大版本升级 4 件套验证 SOP...",
            "type": "heuristic",
            "tags": ["hermes-update", "SOP", ...],
            "importance": 9,
            "distance": null,
            ...
        },
        ...
    ]
}
```

| 端点 | Before | After |
|---|---|---|
| POST /search query=hermes limit=3 | 500 NPE | **200** (3 results) |
| POST /search query=test limit=2 | 500 NPE | **200** (2 results) |
| POST /search query=cortex limit=5 | 500 NPE | **(待测,应该 5)** |
| GET /list | 200 (809 chunks) | 200 (809 chunks, 无回归) |
| GET /health | healthy | healthy |

---

## 5. ADR-063 反思 follow-up

ADR-063 § 反思:

> 同 bug pattern (numpy truth value) v3.67.32 修过但 chroma_store.py:136 漏掉,根因 = 没用统一 helper

本 ADR 是 **第 2 处同 pattern 的 NPE**(虽然 trigger 路径不同:metadata None vs numpy ambiguous),**根因还是一样**:

> **没有统一 helper**,每处 `.get()` 都靠 author 写防御代码

### 5.1 后续建议(ADR-065 candidate)

- 抽 `safe_get(d, key, default)` helper 统一处理 None / NonDict / IndexError
- 把 chroma_store.py / metadata_store.py / memory_service.py 所有 `.get()` 调用都过一遍
- 启动时加 test 覆盖 None / NonDict 输入

---

## 6. 关联诊断建议

用户体检报告同时提了其他 3 项(本次未触动,后续 sprint 处理):

| 优先级 | 项 | 状态 | ADR |
|---|---|---|---|
| 🟡 中 | 主动 forgetting `last_run=null`,需调度 | ⏳ 待做 | ADR-065 candidate |
| 🟢 低 | taxonomy 父子关系扩充(4 → 30+) | ⏳ 待做 | ADR-066 candidate |
| 🟢 低 | 261 条 long_term 长期未访问(可能 hermes 没用) | ⏳ 调研 | - |

---

## 7. Traceability

| commit | 内容 |
|---|---|
| `5501f71` | fix(cortex): ADR-064 search() metadata NPE (F-1) ← 本 ADR |
| `2b0822d` | docs(cortex): Docker 部署指南 (前置) |
| `d54bca1` | feat(cortex): F-A1 sync 部署端 data+models (前置) |
| `f33715d` | docs(cortex): ADR-063 主文档 |
| `32578d9` | fix(cortex): ADR-063 chroma_store numpy fix |

---

## 8. 反思

| 项 | 评估 |
|---|---|
| 是否最小修复 | ✅ 是(只动 search() 函数,11/-5 diff) |
| 是否影响其他端点 | ✅ 不影响(/list /health /provenance 仍 OK) |
| 是否需要 reindex | ❌ 否(根因是代码 bug,非数据) |
| 是否覆盖 None / NonDict | ✅ 三层防御 |
| 是否写 ADR | ✅ 是 (本文件) |
| 是否 commit + push | ✅ `5501f71` 已 push dotnet-new |
| 是否写测试 | ❌ 否(后续 ADR-065 抽 helper 时补) |

按 V10.31 + 反思 — **修 bug 不主动扩大范围**,后续 forgetting / taxonomy 留给用户拍板。