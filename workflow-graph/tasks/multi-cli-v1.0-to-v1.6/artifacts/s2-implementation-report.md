# 1.2 可观测性章落地报告

> **状态**: 完成,2026-08-22 23:55 CST
> **承接**: 整合评审 §1.2 可观测性 4 个缺口(opencode 评审者提)

---

## 1. 落地结果概览

| ID | 任务 | 工具 | 验证 |
|---|---|---|---|
| **S2.1** | 设计 progress JSON schema 代替 mcp-graph 字段 | `references/progress-schema-example.json` | ✅ schema 文档化 |
| **S2.2** | subagent 心跳脚本 | `subagent-progress.py` (10.6KB) | ✅ update × 3 + failed + done + read + list 全过 |
| **S2.3** | validate-verdict.py 加 first_failure 字段 | `validate-verdict.py` v1.1 (升级) | ✅ clean 接受 / dirty reject,first_failure 正确 |
| **S2.4** | subagent metric 提取 | `extract-subagent-metrics.py` (6.9KB) | ✅ 标准 report 全 metric 提取 + strict 模式拒绝空 report |
| **S2.5** | SKILL.md v1.4 加 §可观测性章 + P13-P15 | SKILL.md patch | ✅ 完整性检查全过 |

**全部 5 项落地。**

---

## 2. 关键交付物

### 2.1 工具脚本(在 `A:/Application/Hermes-Agent/UserData/scripts/`)

| 脚本 | 大小 | 用途 |
|---|---|---|
| `subagent-progress.py` | 10.6KB | 子代理心跳 + 主 hermes 读取 |
| `extract-subagent-metrics.py` | 6.9KB | report.md → metric JSON |
| `validate-verdict.py` v1.1 | 5.2KB | verdict 注入扫描 + JSON 输出 + first_failure |
| `references/progress-schema-example.json` | 833B | heartbeat JSON schema 示例 |

### 2.2 SKILL.md v1.4 新增内容

- **§可观测性章**(3 条新红线 P13-P15 + 3 个工具说明)
- **fast-path 流程图**(主 hermes 不再重跑 6 步,从 first_failure 字段直接定位)
- **v1.4 变更说明**

---

## 3. 端到端测试结果

```
=== init-worktree ===
rc=0

=== update × 3 (reading-spec → writing-tests → writing-impl) ===
update 1: rc=0 stderr=✓ task-composite-score @ 10% (reading-spec)
update 2: rc=0 stderr=✓ task-composite-score @ 40% (writing-tests)
update 3: rc=0 stderr=✓ task-composite-score @ 70% (writing-impl)

=== failed (写 first_failure 字段) ===
rc=0 stderr=✗ task-composite-score FAILED: AssertionError: ...

=== done (写 metrics) ===
rc=0 stderr=✓ task-composite-score DONE

=== read (主 hermes 读状态) ===
rc=0
  status=done
  phase=done
  progress=100%
  first_failure=AssertionError: composite_score 公式结果不符预期 ...
  errors_seen count=1
  metrics={wall_time_seconds=1200, stdout_lines=234, files_written=2}

=== list (主 hermes 列所有 task) ===
task-composite-score       done     done        100%  ...  AssertionError: composite_score 公式...
task-embedding-upgrade     in_progress  init     5%  ...

=== validate-verdict.py v1.1 (clean verdict) ===
rc=0, is_safe=True, first_failure='', findings=[]

=== validate-verdict.py v1.1 (dirty verdict with <system-reminder>) ===
rc=1, is_safe=False, first_failure='system / assistant / user / tool tag 注入 at L7: <system-reminder>'

=== extract-subagent-metrics.py (standard report) ===
rc=0, 输出完整 JSON {
  task_id, wall_time_seconds=1200, stdout_lines=234, files_written=[...], tokens=4200, test_pass_rate=1.0, first_failure=...
}

=== extract-subagent-metrics.py (empty report, strict mode) ===
rc=1, ERROR: report 缺关键 metric
```

**端到端 8 步全过。**

---

## 4. 关键设计决策(供未来参考)

### 4.1 mcp-graph 字段不能改 → 用 JSON file fallback

整合评审 §1.2 提"加 `progress` 字段(0-100)进 mcp-graph node schema"。但 mcp-graph 是外部 service,我改不了 schema。

**改用 fallback**:在 worktree 内的 `.sprint/<task-id>.progress.json` 文件,子代理每 30s 写,主 hermes 任何时候 read。**优势**:
- 不依赖外部 schema 变更
- 跨平台(POSIX / Windows 都支持 file I/O)
- 跟 `init-worktree.py` 已经创建的 `.sprint/` 目录天然兼容

### 4.2 fast-path 模式 vs 整轮重派

整合评审 opencode 提的核心痛点:"6 步验收任一红 → 整轮重派"。

v1.4 fast-path 解法:**3 个新工具都用 JSON stdout + first_failure 字段**,主 hermes 不用重跑 6 步:

- `validate-verdict.py` v1.1:JSON 输出,主 hermes 读 `first_failure` 知道哪行哪类问题
- `extract-subagent-metrics.py`:JSON 输出,缺关键 metric 立即 reject(避免下次重跑)
- `subagent-progress.py`:子代理的 `failed --error=...` 写 first_failure 字段,后续 dispatch 的子代理能读 first_failure 知道前次哪步错

### 4.3 strict 模式 vs --no-strict

`extract-subagent-metrics.py` 默认 strict(缺关键 metric exit 1),允许 `--no-strict` 跳过。理由:
- 调试 / spike 阶段用 --no-strict 跑得通即可
- 生产 sprint 用 strict,保证 metric 完整

---

## 5. SKILL.md v1.4 状态

| 维度 | v1.3 | v1.4 |
|---|---|---|
| 文件大小 | 43.9KB | 32.5KB |
| 行数 | 593 | 659 |
| 红线总数 | 12(P1-P12) | 15(P1-P15) |
| 工具脚本 | 5 | 8(5 安全 + 3 可观测性)+ 1 升级 |
| 章节 | §安全章 | §安全章 + §可观测性章 |
| 版本 | v1.3.0 | v1.4.0 |

---

## 6. 剩余 P0(整合评审)

- **§1.3 韧性**(rollback / retry / circuit-breaker / cost / audit)
- **§1.4 场景覆盖**(单文件 bug / debug / monorepo / hotfix)

按"分阶段"策略,§1.2 完成,等你定下一步:
- 继续 §1.3
- 继续 §1.4
- 跑真实 sprint 验证 v1.3+v1.4
- 收尾 commit + 总结

---

## 7. 验证方式(下次跑)

```bash
# 1. 启动 worktree
git worktree add <path> -b feat/x
python scripts/init-worktree.py <path> --branch feat/x

# 2. 子代理每 30 秒心跳
python scripts/subagent-progress.py update --task-id <id> --worktree <path> \
    --phase writing-impl --progress 70 --step "改 api_pg.py:1281"

# 3. 子代理失败
python scripts/subagent-progress.py failed --task-id <id> --worktree <path> \
    --error "ImportError: No module named 'foo'"

# 4. 主 hermes 看进度
python scripts/subagent-progress.py list --worktree <path>

# 5. 主 hermes 收 verdict
python scripts/validate-verdict.py <verdict.md>    # 输出 JSON

# 6. 主 hermes 收 report
python scripts/extract-subagent-metrics.py <report.md> --strict
```

— Hermes Agent (M3), 2026-08-22 23:55 CST