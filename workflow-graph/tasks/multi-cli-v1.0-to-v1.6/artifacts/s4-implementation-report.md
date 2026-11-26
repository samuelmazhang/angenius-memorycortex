# 1.4 场景覆盖章落地报告

> **状态**: 完成,2026-08-23 00:15 CST
> **承接**: 整合评审 §1.4 场景覆盖 4 个缺口(opencode 评审者提)

---

## 1. 落地结果概览

| ID | 任务 | 工具 | 验证 |
|---|---|---|---|
| **S4.1** | 场景路由 + CLI 选型 | `route-task.py` (10.6KB) | ✅ list + 6 个 route + 1 dispatch 全部场景验证 |
| **S4.2** | 铁律 4 破例(debug 任务) | route-task.py + mcp-graph-design refs 更新 | ✅ debug-hard-bug scenario 自动标 `exception_to_iron_law_4=True` |
| **S4.3** | monorepo smoke_cmd array | route-task.py + mcp-graph-design refs 更新 | ✅ monorepo-multi-lang scenario 用 `smoke_cmd_format=array` + example |
| **S4.4** | cve-hotfix fast-path | route-task.py | ✅ cve-hotfix scenario `skip_checklist=[1-9]` 全跳,`skip_verification=[2,3,4,5,6]` |
| **S4.5** | SKILL.md v1.5 → v1.6 + §场景快速路由章 | SKILL.md patch | ✅ 完整性检查全过 |

**全部 5 项落地。**

---

## 2. 端到端测试结果

```
=== Test 1: route-task.py list ===
6 个场景全部列出: single-file-bug-fix, multi-file-refactor, debug-hard-bug, cve-hotfix, monorepo-multi-lang, new-feature

=== Test 2-5: 各场景 route 决策 ===
single-file-bug-fix: skip_checklist=[4,5,6,7,8,9], recommended=[claude-code], est=10min/3K
multi-file-refactor: skip_checklist=[], recommended=[claude-code, codex], est=45min/12K
debug-hard-bug: skip_verification=[6], exception_to_iron_law_4=True, est=120min/30K
cve-hotfix: skip_checklist=[1-9 全部], skip_verification=[2-6], est=5min/2K
monorepo-multi-lang: smoke_cmd_format=array, 3 个语言命令
new-feature: skip_checklist=[], recommended=[claude-code], est=60min/15K

=== Test 6: actual dispatch (single-file-bug-fix) ===
  base: 43ec2b193085 ✓
  budget: 3000 tokens / 10 min ✓
  audit: 191 bytes, 包含 task_dispatch event ✓
  rollback-base: 40 bytes ✓
  budget: 148 bytes ✓
  audit: 191 bytes ✓
```

**端到端 6 测试全过。**

---

## 3. 关键设计决策

### 3.1 fast-path dispatch 落地的复杂度

整合评审 claude 提"3 CLI 选型决策树缺失",opencode 提"hotfix 没时间跑 30 分钟 checklist"。

v1.6 解法:`route-task.py` **单一脚本承担两个职责**:
1. **决策**: 输出 JSON(场景名、推荐 CLI、跳过哪些 checklist 项、估算时间/tokens)
2. **执行**(`--dispatch` flag):自动写 base + budget + audit,准备给子代理派任务

为什么不分两个脚本:决策是单次查询,执行是 "全做"。分成两步反而增加复杂度。

### 3.2 debug 铁律 4 破例的位置

整合评审 opencode 提"debug 任务铁律 4 直接拒绝"。

**v1.6 解法**:
- `route-task.py` 的 `debug-hard-bug` scenario 标 `exception_to_iron_law_4=True`
- `mcp-graph-design/references/field-completeness-checklist.md` v1.1 新增章节,说明 debug 任务的 metadata.test_plan / acceptanceCriteria 可空,但**子代理必须自己写复现测试**
- SKILL.md v1.6 文字明确 "铁律 4 破例" 适用范围

这样**铁律 4 主线不变**(其他场景仍要求 test_plan),只在 debug 场景开例外。

### 3.3 monorepo smoke_cmd 不破坏 mcp-graph schema

整合评审 opencode 提"`smoke_cmd` 强绑单语言;monorepo 要 N 个 smoke 怎么跑"。

**v1.6 解法**:
- mcp-graph schema `smoke_cmd` 字段**仍是 string**(我不改 mcp-graph)
- 但通过 `metadata.smoke_cmd_extra` JSON array 扩展
- 主 hermes 读时合并 `smoke_cmd` (string, 第一个) + `smoke_cmd_extra` (array, 其余)
- 逐条跑 smoke,任一 fail = smoke fail

不破坏 mcp-graph 现有 schema,又支持多语言。

---

## 4. SKILL.md v1.6 状态

| 维度 | v1.5 | v1.6 |
|---|---|---|
| 文件大小 | 37.6KB | 41.7KB |
| 行数 | 767 | 863 |
| 红线总数 | 19(P1-P19) | **20**(P1-P20) |
| 工具脚本 | 12 + 2 升级 | **+1 路由脚本** |
| 章节 | §安全 + §可观测性 + §韧性 | + §场景快速路由章 |
| 版本 | v1.5.0 | v1.6.0 |

---

## 5. 整合评审 4 项 P0 全部完成

| 类别 | 状态 | 工具 | 红线 |
|---|---|---|---|
| **§1.1 安全** | ✅ v1.3 | 5 + 1 升级 | P9-P12 |
| **§1.2 可观测性** | ✅ v1.4 | 3 + 1 升级 | P13-P15 |
| **§1.3 韧性** | ✅ v1.5 | 4 | P16-P19 |
| **§1.4 场景覆盖** | ✅ v1.6 | 1 + 1 refs 联动 | P20 |

**4/4 P0 全部完成。**

---

## 6. 累计(v1.0 → v1.6)总览

| 维度 | 数量 |
|---|---|
| 红线 | **20 条** (P1-P20) |
| 工具脚本 | **13 个 + 2 升级** |
| 章节 | **4 章** (安全 / 可观测性 / 韧性 / 场景路由) |
| 文件大小 | 41.7KB / 863 行 |
| mcp-graph-design 联动更新 | field-completeness-checklist.md v1.1 (12.7KB / 376 行) |
| 实施报告 | 4 份(§1.1 / §1.2 / §1.3 / §1.4) |
| 反思文档 | 1 份 (2026-08-22 multi-cli incident retrospective) |
| 整合评审报告 | 1 份 (4 维度 9.8KB) |

从 v1.0 的"约束卡"变成 v1.6 的"可跑方法论":**3 CLI + 4 大工程实践 + 6 场景路由 + 20 红线 + 13 工具脚本**。

---

## 7. 下一步(等你定)

- 跑真实 sprint 验证 v1.3 + v1.4 + v1.5 + v1.6
- 收尾(commit + 总结 + 删除 v0.7.1 备份)

— Hermes Agent (M3), 2026-08-23 00:15 CST