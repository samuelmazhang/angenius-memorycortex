# Claude 评审 — 工作流编排 + 多 CLI 协同视角

## 问题 4(中等)— 缺 rollback / forward-fix 决策
Step 5 串行任务 B 失败时,Step 6 合并时怎么决定回滚 A?现在完全没写。GitHub Squad 用"每任务一个 PR,合并靠 review"自然处理回滚,v1.2 用单 worktree 反而缺这个机制。

## 缺失 / 漏洞

- **缺 CLI 选型决策树**:3 个 CLI 怎么选?claude/codex/opencode 各自 strengths/weaknesses 全靠 Pitfall D/E 暗示。Squad 模式有"agent capability matrix"。
- **缺 mcp-graph 健康降级路径**:P7 写"daemon 挂了只能重启",但 sprint 中途挂了怎么办?整个 session 放弃?应该有"mcp-graph offline 模式"(subagent-driven-development fallback)预案。
- **缺铁律 5 的 test_plan 模板**:主 arcadia 写 test_plan 时具体写什么?反例 J/K/L 只给反例,没给正例模板。

## 反驳现有设计

- **铁律 4 "200-400 行 diff"上限**:Spec-Driven Development 文献(Spec-Kit / OpenSpec)按 acceptance criteria 切,不按行数;Squad 按 PR 切。**行数硬约束是过度工程**,改为"独立 acceptance criteria + 独立 commit + 可独立回滚"。
- **铁律 5 "主 arcadia 写 test_plan"分工**:Spec-Kit/OpenSpec 都让 AI 起草 acceptance criteria、人 review;v1.2 反过来让 controller 起草,违反 spec-first 文献主流。
- **批处理 dispatch**:Anthropic 2026 文档明确推荐"checkpoint + 短轮",v1.2 写"批处理不接 stdin"是对长跑失控的过度防御,真实问题是 exit-code 监控 + 中断恢复,而非禁止交互。

## 外部参考来源(推断)
- Spec-Kit / OpenSpec(spec-first acceptance criteria 切分法)
- GitHub Squad 模型(每任务一 PR,合并靠 review)
- Anthropic 2026 multi-agent 文档(checkpoint + 短轮)
- Squad mode "agent capability matrix"
