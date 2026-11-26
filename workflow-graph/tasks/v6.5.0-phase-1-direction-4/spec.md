# Task Spec — v6.5.0 Phase 1 direction #4 LOCOMO 1986 online eval harness

**Scenario**: new-feature
**Complexity**: L
**Tokens budget**: 5000
**Worktree**: `..worktrees/v6.5.0-phase-1-direction-4`
**Branch**: `v6.5.0/v6.5.0-phase-1-direction-4`
**mcp-graph node**: `node_90180b7a9dda`
**Plan ref**: docs/改进方案-cortex-v6.5.0-完整版-20260822.md 第 7 节

---

## 功能点 (主 hermes 写,子代理实现)

必做:

1. **LOCOMO 1986 case 加载** (`tests/eval/locomo_loader.py`)
   ```python
   import json
   data = json.load(open("data/locomo10.json"))  # snap-research/locomo
   # 10 conversations, 1986 QA pairs total
   # per conv: [199, 105, 193, 260, 242, 158, 190, 239, 196, 204]
   ```
   - 加载 + 解析 LOCOMO 格式
   - 按 conv 索引: `loader.by_conv_id(conv_id) -> {qa_pairs: [...]}`

2. **Cortex 适配** (`tests/eval/cortex_adapter.py`)
   - 把 LOCOMO 对话喂入 Cortex (`memory_chunks` 表)
   - 跑 LOCOMO QA → 拿到 top-K 检索结果
   - 返回 `{conv_id, question, ground_truth, retrieved_chunks, top_k}`

3. **LLM-as-Judge 评估** (`tests/eval/llm_judge.py`)
   - 用 MiniMax-M3 或 GPT-4o-mini (默认 MiniMax-M3 via http://localhost:8080)
   - 评估: retrieved vs ground_truth
   - 输出: F1, BLEU, exact match

4. **CI 集成** (`tests/eval/locomo_runner.py` + `.github/workflows/locomo-ci.yml`)
   - 200 case 抽样 (CI 快速版) — 跑 ~5 分钟
   - 完整 1986 case (nightly cron) — 跑 ~30 分钟
   - 报告 F1/BLEU/exact match + per-conv breakdown
   - baseline 记录 (跟方案 v3 表 7.4 对齐)

## 验收 (主 hermes 自动跑)

- pytest `tests/eval/test_locomo_v6_5.py` (新建, 4 个 test):
  1. `test_loader_parses_locomo_format` — load 1986 QA pairs
  2. `test_adapter_feeds_cortex` — mock cortex API call
  3. `test_judge_returns_metrics` — mock LLM call,返回 F1/BLEU/EM
  4. `test_runner_reports_baseline` — mock 跑 200 case,断言 baseline 字段
- pytest server/tests/ (现有 70 tests) 无回归
- LOCOMO data file 不在 repo (git submodule 或 download script)

## 不在 #4 范围

- LLM 模型本身训练 (用现有 MiniMax-M3 或 GPT-4o-mini API)
- Cortex 本身改动 (#4 只评估不改)
- LOCOMO 完整 nightly (主 hermes CI 跑,你只跑 200 case 抽样)

## 完成标准

1. 写 `<worktree>/.sprint/v6.5.0-phase-1-direction-4-report.md`
2. 主 hermes 自动 commit + finish_task