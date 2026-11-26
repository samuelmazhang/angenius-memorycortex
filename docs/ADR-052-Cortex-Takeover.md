# ADR-052-Cortex-Takeover — Cortex 部分迁移自 ADR-052 (v10.31 Hygiene Sprint)

| 项 | 值 |
|---|---|
| **状态** | ✅ Accepted / Implemented (cortex 部分) |
| **日期** | 2026-07-22 (迁移日, 原始 ADR 2026-07-14) |
| **Sprint** | v10.31 E3 Cortex Takeover |
| **影响域** | Hermes Memory Cortex (跨 session 语义记忆) |
| **原 ADR 位置** | QuantPlatform.Web.SaaS `docs/04-技术设计/ADR-2026-07-14-052-v10.31-Hygiene-Sprint-Cortex-Takeover.md` |
| **拆分原因** | ADR-052 是 v10.31 混合 ADR (含 5 decisions): STATUS.md hygiene + mcp-graph hygiene + Cortex Takeover + .gitignore + ADR-INDEX。本文件仅保留 Cortex Takeover 部分 (§4 + §12)。其他 4 个 decisions 仍留在 QuantPlatform 原 ADR-052 内。 |

---

## 1. 迁移说明

ADR-052 整体是 v10.31 sprint 收口叙事,5 个 decisions 中只有 **Decision 3 (Cortex 完全接管)** 属于 cortex 范畴。其他 4 个 decisions (STATUS.md hygiene / mcp-graph backlog / .gitignore / ADR-INDEX) 属于 QuantPlatform 项目 hygiene,留 QuantPlatform。

按用户规则 "cortex 内容放 memory-cortex",**只把 Cortex 部分迁过来**,不拆原 ADR。

**关联**:
- 原 ADR-052 (QuantPlatform, 5 decisions 混合)
- ADR-063 (2026-07-22 v4.2.0 ChromaDB reindex numpy fix, 同在 memory-cortex)
- ADR-052 §12 Scope Clarification (Hermes Memory Cortex vs mcp-graph 命名区分)

---

## 4. Decision 3 — Hermes memory_tool.py Cortex 完全接管 recovery (E3)

### 4.1 根因

7-04 完成 cortex 完全接管(参见 backup `/root/.hermes/plugins/memory-cortex/backups/2026-07-04-cortex-takeover/`):
- `memory_tool.py` patch +475/-3 lines
- `cortex_memory_backend.py` 新模块 285 lines
- `test_memory_tool.py` fixture 调整
- README + 完整 5 步实施指南

7-04 完成 cortex 完全接管后,**7-14 Gateway restart 时 v0.18.2 升级 reset 了 memory_tool.py**,cortex 接管 patch 丢失(7 件套只剩 3 件:context embedding + semantic_search + memory_injection),`.md` 文件路径回退(4 类 .md: MEMORY/HERMES/decisions/notes)。

恢复后 7 件套全 PASS,但因为 v0.18.2 升级可能被再次 reset,**加 V0.18.3 fix 注释 + `v0.18.3_fix_marker` 字符串识别** + 周级别 cortex 接管监控告警(Email/Slack)。

### 4.2 诚实归因 (我的 D 风格)

- **#93**:memory_tool.py v0.18.2 升级 reset 是真实风险,**不是我编的** — 7-14 `git log memory_tool.py` 显示 7-04 cortex-takeover commit `5e89b5e4` 在 v0.18.2 reset commit `a3e89b21` 之后被 reset 丢弃
- **#94**:`v0.18.3_fix_marker` 字符串识别是**事后补丁**,不是预防设计。理想是 Hermes 升级前 diff cortex 相关文件,有 cortex patch 时阻止升级或合并升级
- **#95**:周级别 cortex 接管监控告警是**长线设计**,但当前 Hermes 升级节奏(月级别)与 cortex 稳定需求(周级别)不匹配。短期 OK,长期需 Hermes 升级前 pre-check hook

### 4.3 实施步骤

1. **7 件套验证**(cortex 接管完整测试 PASS):
   - context_embedding (V4.0 dual-track encoding)
   - semantic_search (BM25 + vector RRF fusion)
   - memory_injection (V4.0 auto-injection rules)
   - consolidation (V4.2 reconsolidation cycle)
   - forgetting (Ebbinghaus curve + auto cleanup)
   - cross_session_memory (跨 session 持久化)
   - provenance (溯源 metadata)

2. **v0.18.3_fix_marker**:`memory_tool.py` 头部加注释 `# V0.18.3 FIX MARKER: Cortex takeover recovery (v10.31 E3)` + `__cortex_takeover_version__ = "v0.18.3"` 字符串识别
3. **周监控告警**:cortex_health.sh (existing) 跑 `check_all_memories.py` + `cortex fix marker 检测`,失败 → Slack #cortex-alerts
4. **升级前 pre-check hook**(v0.19+ 推荐):`hermes upgrade --check cortex` 阻止 reset cortex patch

### 4.4 Gateway 仍未 restart

本次 sprint **未重启 Hermes Gateway**(v0.18.2 → v0.18.3 升级未触发),所以 v0.18.3 fix marker 当前**只是 source code 注释,未生效**。

触发条件:下次 Hermes 升级 (预计 v10.32+) 后,marker 自动生效。

## 12. Scope Clarification (2026-07-18 19:38 CST 体检发现追加)

**D 风格诚实归因 #109 (后置修正,非推翻原 ADR 决策)**: 本 ADR 标题 "Cortex Takeover" 命名混淆,需要 scope clarification 防止未来误读。

**两个 "Cortex" 的精确划分**:

| 名称 | 类型 | 存储 | 范围 | 本 ADR 涉及 |
|---|---|---|---|---|
| **Hermes Memory Cortex** | 语义记忆系统 (长线 + 中期 + 短期) | Python HTTP service `localhost:1880` (cortex_memory_backend.py) | 跨 session 持久化 | ✅ **本 ADR 接管对象** |
| **mcp-graph** | Workflow 治理图 (SQLite) | `workflow-graph/graph.db` (local) | 当前 session 工作流追踪 | ❌ **本 ADR 未涉及** |

**本 ADR 实际修复**:
- §1 Decision 3: `cortex_memory_backend.py` 模块新建 + `.md` 文件路径废弃
- §9 Verification: `cortex_memory_backend.py 存在` + `memory_tool.py cortex 引用 98 处` — **指 Hermes Memory Cortex**

**本 ADR 不修复 / 不声称**:
- mcp-graph backlog 节点对齐属 **§3 Decision 2** (QuantPlatform 项目 hygiene,跟 Cortex 接管无关)
- "Cortex 长记迁移"属 **§11 v10.32+ 候选**,不在本次 ADR 实施范围
- v10.31 E1 实际做的"mcp-graph batch_add governance nodes" 是 Hygiene 工作 (E1-T1 到 T5),**不是 Cortex 接管**

**触发更新信号**: 用户 OOB 提到 "Cortex 接管 / 接管完成 / Cortex 状态" 时,如不确定 scope,**先问清楚是 Hermes Memory Cortex 还是 mcp-graph**(2 个命名都叫 "Cortex" 是历史偶然,非设计意图)。

**v10.32 推荐**:P1 Blazor OTel 推 v10.32,P0-B 推 v10.33,P1-D OIDC 推 v10.34-35(商业化 trigger 前置)。

---

## 2. 与原 ADR-052 的关系

原 ADR-052 (QuantPlatform) 仍保留 §1 Context / §2 STATUS.md hygiene / §3 mcp-graph backlog / §5 .gitignore / §6 ADR-INDEX / §7 Consequences / §8 Alternatives / §9 Verification / §10 Traceability / §11 推荐候选 段。

**本文件 = 原 ADR-052 §4 Decision 3 (Cortex Takeover) + §12 Scope Clarification**。其他段落跟 cortex 无关,留原处。

引用本文件时用:`ADR-052-Cortex-Takeover` (避免与原 ADR-052 命名冲突)。