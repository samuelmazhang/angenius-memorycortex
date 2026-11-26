# Dispatch — v6.5.0 Phase 1 direction #4 LOCOMO 1986 harness

**收件方**: claude-code (subagent, auto-pilot 3/7)
**发件方**: 主 arcadia (auto-pilot mode)
**Worktree**: `..worktrees/v6.5.0-phase-1-direction-4`
**mcp-graph node**: `node_90180b7a9dda`

---

## 任务

实现 #4 LOCOMO 1986 online eval harness (7 人天 spec, 大 task).

## 必做 (4 步, 见 spec.md 详情)

1. **LOCOMO loader** (`tests/eval/locomo_loader.py`)
   - 加载 `data/locomo10.json` (snap-research/locomo, 1986 QA pairs × 10 convs)
   - 按 conv_id 索引

2. **Cortex adapter** (`tests/eval/cortex_adapter.py`)
   - 把 LOCOMO 对话喂入 Cortex + 跑 QA
   - 返回 top-K 检索结果

3. **LLM-as-Judge** (`tests/eval/llm_judge.py`)
   - 用 MiniMax-M3 (`http://localhost:8080/v1/chat/completions`)
   - 计算 F1 / BLEU / exact match

4. **CI runner + workflow** (`tests/eval/locomo_runner.py` + `.github/workflows/locomo-ci.yml`)
   - 200 case 抽样 (~5 min CI)
   - 完整 1986 case (nightly cron ~30 min)
   - 报告 baseline (跟方案 v3 对齐)

## 必测 (新建 `tests/eval/test_locomo_v6_5.py`, 4 个 test)

1. `test_loader_parses_locomo_format`
2. `test_adapter_feeds_cortex`
3. `test_judge_returns_metrics`
4. `test_runner_reports_baseline`

## 完成品

写 `<worktree>/.sprint/v6.5.0-phase-1-direction-4-report.md`:

```markdown
# #4 LOCOMO Report

## Summary
- LOCOMO loader 实现 (粘贴关键函数)
- Cortex adapter 实现
- LLM-as-Judge 实现 (mock, 不实际跑)
- CI runner + workflow (粘贴 .github/workflows/locomo-ci.yml)

## Tests
- pytest test_locomo_v6_5.py 全过
- 现有 server/tests/ 70/70 无回归

## Files Changed
- tests/eval/locomo_loader.py
- tests/eval/cortex_adapter.py
- tests/eval/llm_judge.py
- tests/eval/locomo_runner.py
- tests/eval/test_locomo_v6_5.py
- .github/workflows/locomo-ci.yml
- scripts/download_locomo_data.sh (or README 指引)

## Open Questions
(无则 None)

## stall 兜底
(如果 stall, 主 arcadia 会接管,这部分记录中断点)
```

## 约束

1. **只做 #4 范围**(不动 consolidation / embedding / HNSW / graph / summary)
2. spec 写功能不写行数
3. 不改 Pydantic 之外库
4. **不要实际跑 1986 case** (太大, sandbox 不能跑 LLM-as-Judge); 只跑 4 个 unit test
5. **如果 stall**, 写 `<worktree>/.sprint/STALL_REPORT.md` 说明 stall 在哪步
6. **不 commit** (主 arcadia 自动)

## stall 检测 (主 arcadia 会每 1 分钟看进度)

主 arcadia 状态机:
- 跑 >10 分钟无新输出 + worktree 已有新文件 → **主 arcadia kill + 接管**
- 跑 >15 分钟完全没动 → **主 arcadia kill + 重派同一 task**(带 "读 #4-STALL_REPORT.md" instruction)
- 你 (claude-code) 自己感觉 stall 就 `exit 0` + 写 STALL_REPORT.md,**不要无限循环**

## 完成后

exit 0, 主 arcadia 自动 commit + finish_task。