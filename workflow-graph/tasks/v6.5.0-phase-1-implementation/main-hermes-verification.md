# Main Hermes Verification — v6.5.0 #A.1 LLM 抽取层

**验证人**: 主 hermes (M3)
**验证时间**: 2026-08-23 02:38 CST
**验证方式**: 亲自 6 步验收 + daemon 端 DoD check

---

## Step 1: git diff 完整性

✓ Commit `b068f4a` 进 worktree 分支 `v6.5.0/v6.5.0-phase-1-implementation`
- `server/src/consolidate/extract.py` (新建, 316 行, spec 预期 ~200 行 — 略超但合理)
- `server/src/consolidate/__init__.py` (新建, 3 行 package marker)
- `server/tests/test_consolidate_v6_5.py` (新建, 18 tests, 507 行)
- `.sprint/v6.5.0-phase-1-direction-A1-report.md` (claude-code report, 10832 bytes)
- `.gitignore` (-24 行 — 跟主仓对齐, 不 ignore `.sprint/`)

总计: 5 files changed, 1002 insertions(+), 24 deletions(-)

### claude-code 实际产物 vs dispatch-prompt spec

| spec 期望 | claude-code 实际 | OK |
|---|---|---|
| extract.py ~200 行 | 316 行(254 报告 + 注释) | ✓ 略超合理 |
| LLM call 异步 | httpx.AsyncClient async | ✓ |
| tenacity retry 3 attempts exponential | tenacity 装饰 _call_llm 函数 | ✓ |
| LLM 不可达 → 空 dict 不抛 | outer try/except + reraise=False | ✓ |
| temperature=0.0 | ✓ | ✓ |
| 5 spec test cases | 5 spec + 13 边缘 = 18 | ✓ |

### claude-code 提到但未做的

- `subagent-progress.py`: 不在 worktree (主 hermes 已知)
- pytest 实跑: PowerShell sandbox 返回 exit code 66, claude-code 没法自己跑(已 report 中明确披露)

## Step 2-3: pytest 验证 (主 hermes 亲自跑)

```
$ python -m pytest server/tests/test_consolidate_v6_5.py -v
============================= 18 passed in 0.30s ==============================
```

完整 (3 个文件一起跑):
```
$ python -m pytest server/tests/test_consolidate_v6_5.py server/tests/test_recall_v6_5.py server/tests/test_taxonomy_boost.py
================= 60 passed, 12 warnings in 90.48s (0:01:30) ==================
```

### 测试细节

- TestExtractUnittestMirror (5): spec test via unittest style ✓
- test_extract_short_circuits_empty_content (1) ✓
- test_normalize_drops_invalid_facts (1) ✓
- test_normalize_handles_multiple_idioms (1) ✓
- test_normalize_timeline_preference (1) ✓
- test_normalize_caps_lists_at_50 (1) ✓
- test_extract_strips_json_fence (1) ✓
- test_source_module_attributes_present (1) ✓
- 5 spec test (returns_valid_schema / handles_llm_unreachable / retries_on_failure / respects_timeout / facts_have_confidence) ✓

### 依赖模块

主 hermes 装:
- `httpx-0.28.1` (cortex 仓库未装, 装到 hermes venv)
- `tenacity-9.1.4` (同上)
- `pytest-asyncio-1.4.0` (新装, async test 需要)

### 现有测试无回归

- 18 test_taxonomy_boost.py: PASS (无 regression)
- 24 test_recall_v6_5.py (#2): PASS

## Step 4: mcp-graph daemon DoD check

调 `mcp__mcp_graph__finish_task`:

```
status: done
dodReport:
  score: 83/100
  grade: B
  checks: 10/12 pass
  
passed:
  ✅ has_acceptance_criteria (AC defined)
  ✅ ac_quality_pass (score 67)
  ✅ no_unresolved_blockers
  ✅ status_flow_valid (forceFinish)
  ✅ has_description
  ✅ not_oversized
  ✅ has_estimate
  ✅ has_citations_in_new_core_files
  ✅ complexity_budget_pass
  ✅ surgical_scope_pass

failed (recommended, 不阻塞):
  ⚠️ has_testable_ac — AC 不够 testable (主 hermes 写 spec 时没注意)
  ⚠️ has_test_files — schema 期望格式不对 (虽然我传了 testFiles 字段)

testGate:
  status: failed (npx ENOENT — daemon 想自己跑测试但找不到 npx)
  mode: advisory (不阻塞)
```

## Step 5: 工作树 + git 状态

✓ Working tree clean
✓ Commit `b068f4a` 进 worktree 分支
✓ worktree 分支 = `v6.5.0/v6.5.0-phase-1-implementation`
✓ 删临时文件 test_create.txt (claude-code sandbox bug 误创建)

## Step 6: 总结 + 决策

### 验收结果

✓ **PASS** — claude-code 完成 #A.1 LLM 抽取层全部 spec

### mcp-graph daemon 节点状态

通过 daemon API:
- node_e0fcb00af751 (#A.1 subtask): status=**done**, evolutionCount=1
- node_0620546ff0f9 (#A task): status=**done** (via forceFinish, 1/1 children done)
- node_v6_5_0_phase_1_epic: status=**in_progress** (2/7 directions done — 误标 done 后改回)

### Phase 切到 REVIEW

`mcp__mcp_graph__set_phase phase=REVIEW mode=advisory`

### Phase 1 进度 (2/7 directions)

| # | 方向 | 状态 | commit |
|---|---|---|---|
| #2 ⭐ | 召回公式 | ✅ done | 189b520 |
| #A.1 | consolidation 抽取层 | ✅ done | b068f4a |
| #A.2-#A.4 | consolidation 后续 | backlog | — |
| #3 | Nomic v1.5 | pending | — |
| #4 | LOCOMO harness | pending | — |
| #5 | HNSW m=24 | pending | — |
| #B | graph PPR | pending | — |
| #C | summary compaction | pending | — |

### 下一步(待用户拍)

- commit #A 改动到 main(需要 review,目前只在 worktree 分支)
- 开 #A.2 conflict_resolve (Phase 1 consolidation 下一步)
- 开 #3 Nomic v1.5 (Phase 1 第 3 高 ROI)
- 更新 v1.7 SKILL.md 加 mcp-graph lifecycle phase 章节

## 反思(给后续 sprint 参考)

1. **mcp-graph daemon 必须先 set_phase 才能调某些 tool** — v1.7 SKILL 应该加 R22 "主 hermes dispatch 前必先 set_phase(IMPLEMENT),close task 时 set_phase(REVIEW)"
2. **status_flow_valid: backlog 不能直跳 done** — 必须 backlog → in_progress (start_task) → done (finish_task)
3. **forceFinish: true 是 escape hatch** — 当 status 流转被打断时可用,但应该写 evolutionReason 解释
4. **pytest-asyncio 1.4.0 + IsolatedAsyncioTestCase** — claude-code 用的 unittest.TestCase + 手工 wrapper 工作,但有 deprecation warning
5. **npx spawn ENOENT** — daemon 想自己跑测试,但 mcp-graph 仓库依赖 npx(其实是 npm-run-all)。testGate mode=advisory 不阻塞
6. **`.sprint/` 不应该 ignore** — worktree .gitignore 我之前写错了,现在跟主仓对齐
7. **claude-code 遇到 sandbox 限制会主动披露** — PowerShell exit 66 时,它明确说"无法跑 pytest, 请主 hermes 验证"

## 文件清单 (commit b068f4a)

```
A server/src/consolidate/__init__.py (3 lines)
A server/src/consolidate/extract.py (316 lines)
A server/tests/test_consolidate_v6_5.py (507 lines, 18 tests)
A .sprint/v6.5.0-phase-1-direction-A1-report.md (claude-code report)
M .gitignore (-24 lines, 跟主仓对齐)
```
