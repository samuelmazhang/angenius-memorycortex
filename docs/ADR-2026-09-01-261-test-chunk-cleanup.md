# ADR-261: 2026-09-01 test residue cleanup + script `scripts/cortex-purge-test-chunks.sh`

- Status: Accepted
- Date: 2026-09-01
- Author: Hermes (subagent dispatch) + 天行健 (决策)
- Depends on: v6.5.0 P2 scheduler (status='dead' column), memory_chunks table

## 背景 (Context)

cortex 在多次 verify / e2e / pytest 测试中累计留下了 ~150 条测试残骸
chunk: 它们 source=`verify`/`verify_651`/`verify-script`/`live`/`verification`
或 source=`hermes-session-summarizer` 且 content 含 `test-session-summary-ssbb-...` /
`verify-summarizer-...` / `[session:...source:verify-e2e]` / `[session:...source:pytest-end-to-end]`,
或 content 完全等于 `probe1` / `probe2` / `e2e-memtool-XXX` / `probe-1-verify-deploy-XXX`
这类纯测试命名标记。importance 多在 5-7 之间, archived 多为 1 但也有
active=0 的。它们不会被 P2 worker 处理 (extract 返回空 envelope, 总 actions=0),
但占用 storage + 干扰 recall 检索。

## 决策 (Decision)

### 删除分类 (2026-09-01 实际执行)

| 桶 | pattern | 删除数 |
|---|---|---|
| 1. source IN (verify/live/verification/...) | 硬枚举 | 6 |
| 2. type='test' | 硬枚举 | 1 |
| 3. source='hermes-session-summarizer' | 该 source 全是 verify-e2e / pytest-end-to-end 测试产物 | 30 |
| 4. content IN (probe1, probe2) | 纯测试 content | 86 |
| 5. content LIKE 'e2e-memtool-%' (开头) | e2e 测试命名 | 19 |
| 6. content LIKE 'verify-summarizer%' OR '%test-session-summary-ssbb%' OR '%session:test-session-%' | pytest/verify-e2e 自动 session 摘要 | 9 |
| **总计** | | **151** |

### 关键 pattern 安全教训 (写给以后)

- `content LIKE '%XXX%'` (双侧 wildcard) **危险**: 真实 user 笔记可能在
  正文里提到工具名 (e.g. 提到 "e2e-memtool" 作为例子)。**只匹配 e2e-memtool
  纯字符串 chunk**, 不会误删;**但 `LIKE '%e2e-memtool%'` 误伤了 3 条真实
  user 笔记** (幸好这次没真删 -- 因为我们用了 `LIKE 'e2e-memtool-%'` 开头
  匹配, 不含子串)。
- **正确 pattern**: `content = 'probe1'` (等值) 或 `content LIKE 'e2e-memtool-%'`
  (开头匹配)。
- **错误 pattern (禁止)**: `content LIKE '%xxx%'` 在 user-generated content
  上不可信 -- 工具/库/版本号常被引用。

### 脚本化 (scripts/cortex-purge-test-chunks.sh)

把这次清理的 11 条 SQL 固化进可复用脚本, 后续 sprint 结束后一键跑,
避免下次又积 200 条测试残骸。

## 状态 (Status)

执行完成 (commit daba5be 后立刻跑): 

```
BEFORE total: 2694
AFTER  total: 2543
deleted:     151
误删:        0 (3 条 e2e-memtool-提及 的 user 笔记 已确认仍在, importance=7)
```

## 验收 (Verification)

- 7 个测试残留 pattern 全部清零:
  - `source IN (verify, verify_651, ..., user-cli-...)` -> 0 rows
  - `type = 'test'` -> 0 rows
  - `source = 'hermes-session-summarizer'` -> 0 rows
  - `content IN (probe1, probe2)` -> 0 rows
  - `content LIKE 'e2e-memtool-%'` -> 0 rows (开头匹配, 不伤真笔记)
  - `content LIKE '%verify-summarizer%'` -> 0 rows
  - `content LIKE '%test-session-summary-ssbb%'` -> 0 rows
- 真实 user 笔记 (`source='user'`) 完整保留 (2428 行 -> 仍然 2428)
- 真实 Sprint 36/37 session 摘要 (`source='hermes-agent-sprint36-37'`) 保留
- `hermes_memory` knowledge (Hermes Memory Architecture) 保留

## 风险 (Risks)

- **没有 git history 还原机制**: cortex memory chunks 不在 git 里,
  PG 一旦 DELETE 行就永久消失。下次清理前**必须**用 `psql -c "SELECT ... LIMIT 5"`
  抽样验证 pattern 不误伤真数据。
- **archived=1 也被删**: 这次清包括已 archive 的测试残骸 (节省
  storage)。如果未来想保留 test 历史, 加 `AND archived=0`。
- **P2 worker 没有自动清理这种残骸**: extract 返回 base envelope (empty),
  total_actions=0, 但 chunk 不被 P2 worker 自动删。**手动定期清理
  (建议每个 sprint 收口前一次)**。

## 后续 ops 建议

1. **每个 sprint 收口 commit**: 跑一次 `scripts/cortex-purge-test-chunks.sh`
2. **CHANGELOG 提及**: 每个 sprint CHANGELOG 加 "Test residue cleanup: -N
   chunks" 行
3. **ALERT**: cortex_queue_dead > 10 之外, 增加
   `total_memories - archived > THRESHOLD` 增长告警 (看是不是测试残骸累积)
