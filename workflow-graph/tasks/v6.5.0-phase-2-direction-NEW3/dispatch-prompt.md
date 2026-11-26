# Dispatch — v6.5.0 Phase 2 #NEW3 fact quality monitoring

**收件方**: claude-code (auto-pilot Phase 2)

按 spec.md 实现. 完成写 `.sprint/<task-id>-report.md`.

## 约束

1. 只做 NEW3 范围
2. **不要在 worktree 根创建 tests/** (P31)
3. spec 写功能不写行数
4. 不调 LLM
5. 不 commit (主 hermes 自动)
6. **如果 stall**, 写 `.sprint/STALL_REPORT.md`, exit 0 (P30 重派)

完成后 exit 0.
