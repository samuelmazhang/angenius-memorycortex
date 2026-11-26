# Dispatch Prompt — multi-cli-v1.0-to-v1.6

**回顾**: 这是事后重构,原始 prompt 散落在多次 session 中。

**起始触发**: 2026-08-22 21:00 CST 7-CLI 事故 — 凭空造 `D:\test\cortex-v6.5\` 工作区, 8 个 CLI 并行无 owner,mcp-graph daemon 被 141 节点灌死。

**目标**:
- v0.7.1 skill 完全删除,用 v1.0 `multi-cli-graph-software-development` 替代
- 反思事故 + 加固 5 铁律 + 12 红线 + 9 Pitfalls
- 跟 `mcp-graph-design` skill 协同
- 整合 3 CLI(claude / codex / opencode)评审 + 主 hermes 自评
- 4 项 P0(安全 / 可观测性 / 韧性 / 场景覆盖)全部落地

**执行人**: 主 hermes M3 + 子代理 claude / codex / opencode + hermes leaf

**时间**: 2026-08-22 22:00 - 2026-08-23 01:45 CST

**结果**: v1.6 完整交付(20 红线 + 13 工具脚本 + 4 章),sprint 验证 13/14 通过

---

详细 prompt 见 artifacts/prompt-*.txt(主 hermes 派给 3 CLI 的评审任务原文)
