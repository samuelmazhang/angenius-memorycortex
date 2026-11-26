# v6.5.0 #A.1 Sprint 实施报告

> **状态**: 完成,2026-08-23 02:38 CST
> **承接**:整合评审 + 你"跑 #A consolidation 真实开发"指令

---

## 1. 落地结果概览

| ID | 任务 | 工具 | 验证 |
|---|---|---|---|
| **A1.1** | mcp-graph daemon phase 切到 IMPLEMENT | mcp__mcp_graph__set_phase | ✅ |
| **A1.2** | 创建 #A task + #A.1 subtask (daemon-side) | mcp__mcp_graph__node add | ✅ |
| **A1.3** | 升级 spec.md + dispatch-prompt.md | (写文件) | ✅ |
| **A1.4** | 真派 claude-code (中间被 kill 重启, 但第一版完成) | claude -p --bare | ✅ |
| **A1.5** | 主 hermes 6 步验收 | pytest + git + daemon DoD | ✅ 18/18 + 60/60 |
| **A1.6** | 通过 daemon finish_task (forceFinish) | mcp__mcp_graph__finish_task | ✅ |
| **A1.7** | commit b068f4a 进 worktree 分支 | git commit | ✅ |
| **A1.8** | phase 切到 REVIEW | mcp__mcp_graph__set_phase | ✅ |

---

## 2. 关键发现

### 2.1 claude-code 的 sandbox 限制

claude-code 报告:
> PowerShell returns exit code 66 on every invocation in this session (verified: simple echo test, 1+1, cmd /c echo test all fail). I cannot run pytest to validate the 13 tests pass at runtime.

**但产物完整**: extract.py 316 行 + test_consolidate_v6_5.py 507 行(18 tests)+ report.md 10832 bytes。

**主 hermes 验证通过**: pytest 18/18 PASS, 60/60 server/tests/ PASS。

### 2.2 mcp-graph daemon lifecycle 真实状态

通过 daemon_status 验证:
- `mode: inactive` (但实际 MCP tools 都工作 — daemon UI 静默状态 ≠ 实际状态)
- 必须先 `set_phase(IMPLEMENT)` 才能调 `node.add` 等 write tools
- status flow: `backlog → in_progress (start_task) → done (finish_task)`
- 跳过 status flow 用 `forceFinish: true`(escape hatch,但应该写 evolutionReason)

### 2.3 工作树 .gitignore bug

worktree 的 .gitignore 多了 `.sprint/` ignore 段(我之前加的,跟主仓不一致)。在 commit #2 时还没加(后加的),所以 .sprint/v6.5.0-phase-1-direction-2-report.md 成功 commit。

**修复**: commit #A.1 时把 worktree .gitignore 改成跟主仓一致(.sprint/ 不 ignore)。`.sprint/v6.5.0-phase-1-direction-A1-report.md` 现在能 commit。

### 2.4 pytest-asyncio 缺失

claude-code 写的 test 用 `@pytest.mark.asyncio`,但 pytest-asyncio 没装 → "Unknown pytest.mark.asyncio" 警告。

**修复**: 装 pytest-asyncio 1.4.0 (主 hermes 装到 hermes venv)。

---

## 3. 测试结果详情

```
server/tests/test_consolidate_v6_5.py (单独跑)
============================= 18 passed in 0.30s ==============================

server/tests/ (3 文件一起跑)
================= 60 passed, 12 warnings in 90.48s ==================

- test_consolidate_v6_5.py: 18 passed (#A.1)
- test_recall_v6_5.py: 24 passed (#2)
- test_taxonomy_boost.py: 18 passed (现有,无回归)
```

**0 失败,0 回归**。

---

## 4. claude-code 实际产物 vs spec 对齐

| spec 项 | 期望 | 实际 | 评估 |
|---|---|---|---|
| extract.py 行数 | ~200 | 316 | ✓ 略超但合理(注释+空行) |
| tenacity retry | 3 attempts, exp 30s/60s/120s | ✓ | ✓ 精确 |
| LLM 不可达 → 空 dict | 硬 contract | outer try/except + reraise=False | ✓ |
| temperature=0.0 | 确定性 | ✓ | ✓ |
| JSON schema 校验 | facts 含 confidence 0-1 | 3 normalizer | ✓ 超 spec(支持多种 LLM idiom) |
| 5 spec test | 必过 | 5 + 13 = 18 | ✓ |

### 超出 spec 的部分

- **3 个 normalizer** (_normalize_fact / _timeline / _preference) — 支持 3 种 LLM idiom (entity/predicate/value, subject/relation/object, head/type/tail)
- **JSON fence 剥离** + brace-balance fallback — LLM 偶发返回 ```json ... ```
- **空 content 短路** — 不打 LLM 省 token
- **cap lists at 50** — 防 LLM 抽疯返回几千条
- **retry_if_exception_type 显式列出 builtin ConnectionError** — 关键修正(不是 httpx.HTTPError 子类)

### 欠缺的(后续 sprint)

- `/consolidate/extract` FastAPI endpoint (api_pg.py:2453) 还是 regex-split 笨实现 — #A.2 应该把它换成 `extract()` 调用
- `memory_extracted_facts` 表 (schema 已有) — #A.2 写时用

---

## 5. mcp-graph daemon 节点状态

| 节点 | id | 状态 | evolutionCount |
|---|---|---|---|
| Epic | node_v6_5_0_phase_1_epic | in_progress | 2 |
| Task #2 | node_v6_5_0_d2_recall_fix | done | 1 |
| Subtask #2.1 | node_v6_5_0_subtask_2_1_formula | done | — |
| Subtask #2.2 | node_v6_5_0_subtask_2_2_cold_start | done | — |
| Subtask #2.3 | node_v6_5_0_subtask_2_3_pydantic | done | — |
| Subtask #2.4 | node_v6_5_0_subtask_2_4_related | done | — |
| Task #A | node_0620546ff0f9 | done | — |
| Subtask #A.1 | node_e0fcb00af751 | done | 1 |

**8 个节点,2 个 done task,6 个 done subtask**。

---

## 6. Commit history

```
b068f4a feat(cortex): v6.5.0 Phase 1 direction #A.1 — LLM 抽取层 extract.py
189b520 feat(cortex): v6.5.0 Phase 1 direction #2 — 召回公式修正
3ba180c feat(workflow-graph): v1.7 task-dir standard + multi-cli v1.0-v1.6 migration
43ec2b1 chore(cortex): v6.5.0 planning + connector hermes v6.3.1 dual-write + maint cron
```

worktree 分支: `v6.5.0/v6.5.0-phase-1-implementation` (2 个 sprint commit)

---

## 7. 反思 / v1.7 SKILL 改进建议

1. **加 R22 (mcp-graph lifecycle phase)**:
   - 主 hermes dispatch 前必先 `set_phase(IMPLEMENT)`
   - close task 时 `set_phase(REVIEW)`
   - commit 时 `set_phase(HANDOFF)`
   - 部署时 `set_phase(DEPLOY)`
2. **加 status_flow_valid 红线**: backlog → in_progress → done, 跳过要 forceFinish + evolutionReason
3. **claude-code sandbox 限制**: PowerShell exit 66 时它会自己披露, 主 hermes 应该手动跑 pytest 验证(已经做了)
4. **worktree .gitignore bug**: init-worktree 复写 .gitignore 时应该不覆盖, 或者 init-worktree 应该从主仓拉取当前 .gitignore 而不是用内置模板
5. **pytest-asyncio 依赖**: cortex 项目 pyproject.toml 应该有 pytest-asyncio, 不是临时装
6. **npx spawn ENOENT in daemon testGate**: daemon 默认想自己跑测试, 但 mcp-graph 用 npm 包。需要修 daemon 或者配 `mode: advisory`(本次默认 advisory)

---

## 8. 下一步

- 等用户拍:commit #A 改动到 main / 开 #A.2 / 开 #3 / 收尾

— Hermes Agent (M3), 2026-08-23 02:38 CST
