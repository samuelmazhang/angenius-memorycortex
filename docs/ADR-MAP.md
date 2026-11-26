# ADR ↔ Code 双向追溯 (2026-07-25)

| ADR | 主题 | 实施位置 | 测试覆盖 |
|---|---|---|---|
| **ADR-063** | chroma reindex numpy ambiguous truth value fix | `chroma_store.py:114-130` (header comment + `dict.get`) | 没 reindex unit test, 但 ADR-064 测试隐含覆盖 |
| **ADR-064** | search() metadata NPE 修复 | `chroma_store.py:1-4` (file header), `:113-129` (search fix) | `/search` 9 重现, 已修复 |
| **ADR-065** | FastAPI lifespan 后台调度器 | `api.py:29-91` (`lifespan` + `forgetting_loop`) | ✅ 30 min 后跑首次 + 跨 restart 验证 |
| **ADR-066** | forgetting last_run 持久化 + taxonomy 扩充 | `metadata_store.py:117-159` (`forgetting_log` 表 + helper) <br> `memory_forgetting.py:54-72, 312-330` (load + save) | ✅ 跨 restart 保留 last_run |


| **ADR-067** | Cortex 热度+推翻完整实施 (V6.0) | `server/src/api_pg.py` (5 个 patch 标注) | ✅ E2E 全链路 |
| **ADR-067-Patch-1** | search→access_stats 热补丁 | `api_pg.py:929-950` (search_memory 末尾 try/except) | ✅ access_stats.search-type: 0→130, forgetting/guided 9 条升级 |
| **ADR-067-Patch-2** | eval/feedback stub→真实 | `api_pg.py:1968-2012` (eval_feedback) | ✅ audit log 写入, 2 项 updated/total |
| **ADR-067-Patch-3** | storage/evict stub→末位淘汰 | `api_pg.py:1973-2020` (storage_evict) | ✅ 1 条 cold chunk 被淘汰 |
| **ADR-067-Patch-4** | reconsolidate stub→关联衰减 | `api_pg.py:_reconsolidate_sync` | ✅ 2051 关联 ×0.95, 500 升级 |
| **ADR-067-Patch-5** | rumination/run stub→矛盾检测 | `api_pg.py:_rumination_sync` | ✅ 200 对矛盾标记 metadata |

## ADR-068 (2026-08-05) V5.4 conflict-resolve: 新事实优先旧事实

按用户原话 "新信息跟老信息冲突时,要优先更新老记忆,如果不能合并就删除旧的相关内容,添加新的内容" 实施。

| 子能力 | 实施位置 | 行为 |
|---|---|---|
| Schema 加 4 列 + memory_conflict_log 表 | `server/docker/unified/schema.sql` | `is_deprecated/deprecated_at/superseded_by` + 审计表 |
| write-path `/conflict/upsert` | `api_pg.py` 新增 | 写入新事实时自动按 `(user_id, source_key)` 桶检测冲突,supersede 或 mark_only |
| periodic `/conflict/scan` | `api_pg.py` 新增 | 兜底扫多版本桶,处理异步竞态/手工写入 |
| audit `/conflict/log` | `api_pg.py` 新增 | 查询 supersede 决策历史 |
| daily cron 接入 | `cortex_maintenance.py:118-123` | daily 跑 `/conflict/scan` + `/conflict/log` |

**决策矩阵**: confidence≥0.7 AND new_imp≥old_imp → SUPERSEDE;否则 MARK_ONLY。详见 ADR-2026-08-05-068-cortex-conflict-resolve.md

## ADR-067 实施状态 (2026-08-04)

| Patch | 实施 | E2E |
|---|---|---|
| 1. search→access_stats | ✅ | ✅ 已验证 |
| 2. eval/feedback | ✅ | ✅ 已验证 |
| 3. storage/evict | ✅ | ✅ 已验证 |
| 4. reconsolidate | ✅ | ✅ 已验证 |
| 5. rumination/run | ✅ | ✅ 已验证 |
| 6. ADR-MAP/CHANGELOG 闭环 | 🟡 进行中 | — |
| 7. 回滚测试 | ⏳ 待测 | — |
| 8. 推送 Gitea | ⏳ 待推 | — |

## ADR-067 候选 (原 wire taxonomy_boost, 已并入 Patch 系列)

(注: ADR-067 主方案改为分 5 个 Patch 实施, taxonomy_boost 推迟到 ADR-068)

| 候选 | 描述 | 估时 | 风险 |
|---|---|---|---|
| **ADR-067** | `apply_taxonomy_boost` wire 到 `/search` 主路径 | 30 min | 中 (改 ranking, 需 E2E 重测) |

## ADR 引用 grep 模板

```bash
grep -rnE "ADR-(063|064|065|066|067)" /root/.hermes/plugins/memory-cortex/src
```

## 维护规则

- 每个新 ADR 在 `docs/ADR-XXX-<slug>.md` 写详细 doc
- 实施 file 头部加 `# ADR-XXX: <一句话>` 注释 (file-level)
- 关键 function 内加 `# ADR-XXX fix (YYYY-MM-DD): <详细>` (line-level)
- 每月回看此 map, 确认 ADR-XXX 真存在于代码, 无 stale doc

---

## ADR-067 (2026-08-04 → Accepted 2026-08-06) Cortex V6.0 热度+推翻完整实施

| 模块 | 实施位置 | 状态 |
|---|---|---|
| M1 access_stats 计数 | `api_pg.py` (Patch-1) | ✅ `d670954` |
| M2 forgetting 完整实现 | `api_pg.py` | ✅ `4bb5cf7` |
| M3 metadata_store forgetting_log | `api_pg.py` | ✅ `4bb5cf7` |
| M4 reconsolidate 关联衰减 | `api_pg.py:_reconsolidate_sync` | ✅ `4bb5cf7` |
| M5 rumination 矛盾检测 | `api_pg.py:_rumination_sync` | ✅ `4bb5cf7` |
| M6 eval/feedback stub→真实 | `api_pg.py` (Patch-2) | ✅ `4bb5cf7` |
| M7 storage/evict 末位淘汰 | `api_pg.py` (Patch-3) | ✅ `4bb5cf7` |
| M8 taxonomy boost | `server/src/memory_taxonomy.py` | ✅ `5704bf6` (本轮) |
| M9 search 热度项 | `api_pg.py` (Patch-1) | ✅ `d670954` |
| M10 文档闭环 | ADR-MAP/CHANGELOG/设计 | ✅ 本轮 |

## ADR-068 (2026-08-05) V5.4 conflict-resolve: 新事实优先旧事实

| 子能力 | 实施位置 | 行为 |
|---|---|---|
| Schema 加 4 列 + memory_conflict_log 表 | `server/docker/unified/schema.sql` | `is_deprecated/deprecated_at/superseded_by` + 审计表 |
| write-path `/conflict/upsert` | `api_pg.py` 新增 | 写入新事实时自动按 `(user_id, source_key)` 桶检测冲突,supersede 或 mark_only |
| periodic `/conflict/scan` | `api_pg.py` 新增 | 兜底扫多版本桶,处理异步竞态/手工写入 |
| audit `/conflict/log` | `api_pg.py` 新增 | 查询 supersede 决策历史 |
| daily cron 接入 | `cortex_maintenance.py:118-123` | daily 跑 `/conflict/scan` + `/conflict/log` |

## ADR-069 (2026-08-06) Web UI + V5.4 启用 + 数据治理 + cron

| 维度 | 实施位置 | 状态 |
|---|---|---|
| Web UI 路由修复 | `api_pg.py` + bind-mount `static/` | ✅ `efa3600` |
| API 自动探测 (3 级覆盖) | `static/index.html` | ✅ `1460ad7` |
| V5.4 启用 (autosourcekey trigger) | PG 触发器 `trg_chunks_autosourcekey` | ✅ |
| 数据治理 4 件套 | `maintenance_daily.sh` (cron `a1714f2f8720`) | ✅ |
| 回滚脚本 | `server/scripts/rollback_cortex.sh` | ✅ `aa16495` |

## ADR-070 (2026-08-06) cron 告警通道设计

详见 `ADR-2026-08-06-070-cron-alert-channel.md` (本轮新增)

## ADR-071 (2026-08-06) connector 死代码清理

详见 `ADR-2026-08-06-071-connector-deadcode-cleanup.md` (本轮新增)

## ADR-072 (2026-08-06) WebUI E2E (Playwright)

详见 `ADR-2026-08-06-072-webui-e2e-playwright.md` (本轮新增)


---

## ADR-073/074 (2026-08-07) V7 Spreading Activation → Rejected

详见 `docs/ADR-2026-08-07-073-v7-spreading-activation-poc.md` 和 `ADR-2026-08-07-074-spreading-activation-algorithm.md`.

**结论**: 5 次评估 (合成 + 真实数据 + grid search 27 组 + 权重搜索 5 组) 全部 FAIL. SA 在 cortex V5/V6 schema 形态的真实数据上不优于 BFS.

**归档**: `experiments/v7-spreading-activation/` (含 README.md 总结 + 6 个脚本).

**V6 (ADR-067-072) 是 memory-cortex 最终版本.** ADR-075/076/077/078 (三层存储 / 工作记忆 / 双系统 / 巩固升级) 随 V7 整体归档, 不实施.

> **注 (2026-08-23):** ADR-075 (content-driven maintenance) 在 2026-08-19 已被接受并实施 (见 `server/src/api_pg.py` 等)。本 ADR-076 (summarize_fn 契约统一) 是 v6.5.0 的演进,跟上述"V6 是最终版本"判断无冲突 —— 它属于 v6.5.0,不是 V7。ADR-077 (nomic v1.5 本地 snapshot 部署) 同样是 v6.5.0 #3 的真实落地,与本节无冲突。

### ADR-073 vs ADR-067/068/069

| 维度 | V6 ADR-067/068/069 | V7 ADR-073/074 |
|------|---------------------|------------------|
| 范围 | V6 全部 10 模块 + ADR-068 冲突解决 + ADR-069 数据治理 | 仅 spread activation |
| 状态 | ✅ Accepted (V6.0.0 收官) | ❌ Rejected (PoC 全 FAIL) |
| 实施 | `server/src/api_pg.py` + `memory_taxonomy.py` 等 | `experiments/v7-spreading-activation/` (归档) |
