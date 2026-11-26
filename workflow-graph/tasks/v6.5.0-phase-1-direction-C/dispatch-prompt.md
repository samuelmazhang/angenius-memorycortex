# Dispatch — v6.5.0-phase-1-direction-C

**收件方**: claude-code (auto-pilot)
**发件方**: 主 arcadia
**Worktree**: `..worktrees\v6.5.0-phase-1-direction-C`
**mcp-graph node**: `node_4cd220af7a96`

按 spec.md 实现. 完成写 `.sprint/<task-id>-report.md`.

## 约束

1. 只做本 task 范围
2. **不要在 worktree 根创建 tests/** (P31), 用 `server/tests/...` 嵌套路径
3. spec 写功能不写行数
4. 不调 LLM (mock/stub)
5. 不 commit (主 arcadia 自动)
6. **如果 stall**, 写 `.sprint/STALL_REPORT.md`, exit 0 (P30 重派)

## stall 检测 (P30 综合判断 + P32 notification + P33 cron fallback)

主 arcadia 等 notify_on_complete 自动 push. 不每分钟 poll (P32).
cron monitor.py (P33) 每 5 分钟检查真 stall (>5min 无 writing 动作) → 自动 kill + 重派.
lock-file (P34) 防主 arcadia + cron 双杀.
trigger-file (P35) 让主 arcadia 知道 cron 处理.

完成后 exit 0.
