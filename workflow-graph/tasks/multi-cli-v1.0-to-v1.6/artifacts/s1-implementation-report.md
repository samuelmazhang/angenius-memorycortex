# 1.1 安全章落地报告

> **状态**: 完成,2026-08-22 23:50 CST
> **承接**: 整合评审报告(2026-08-22 22:35)— P0 必修 §1.1 安全 4 个威胁 + 3 个缺口

---

## 1. 落地结果概览

| ID | 任务 | 工具 | rc | 验证 |
|---|---|---|---|---|
| **S1.1** | 报告入库前过滤敏感信息 | `redact-report.py`(6.5KB) | exit 0 = 干净,1 = 找到 high | ✅ 检出 6 high + 2 medium |
| **S1.2** | reviewer verdict 注入扫描 | `validate-verdict.py`(4.5KB) | exit 0 = 接受,1 = reject | ✅ 干净接受,污染 reject |
| **S1.3** | worktree 删除进程保护 | `safe-worktree-remove.py`(8.6KB) | exit 0 = 删,1 = 失败 | ✅ 空 worktree 删成功;有进程时不强制 rm |
| **S1.4** | worktree 创建强制 .gitignore | `init-worktree.py`(5.5KB) | exit 0 = ok,1 = 验证失败 | ✅ .gitignore 写入 + 4 红线 + .sprint/ chmod 700 |
| **S1.5** | mcp-graph write 工具 OS 层 enforce | `mcp-graph-readonly-proxy.py`(5.3KB) + `references/mcp-graph-readonly-config.md`(4.0KB) | n/a(stdio MCP) | ✅ 8 个单元测试全过 |
| **S1.6** | .sprint/ 目录权限 | (集成在 S1.4 init-worktree) | n/a | ✅ chmod 700(POSIX) / icacls(Windows) |
| **S1.7** | SKILL.md 更新到 v1.3 | (skill 文件直接改) | n/a | ✅ 4 新红线 P9-P12 + §安全章 + 5 工具引用 |

**全部 7 项落地。**

---

## 2. 文件清单(全部在 `A:/Application/Arcadia-Agent/UserData/scripts/`)

```
scripts/
├── redact-report.py                  (6.5KB)  S1.1
├── validate-verdict.py               (4.5KB)  S1.2
├── safe-worktree-remove.py           (8.6KB)  S1.3
├── init-worktree.py                  (5.5KB)  S1.4 + S1.6
├── mcp-graph-readonly-proxy.py       (5.3KB)  S1.5
└── references/
    └── mcp-graph-readonly-config.md  (4.0KB)  S1.5 配置指南

+ SKILL.md 更新 v1.2 → v1.3
```

---

## 3. 端到端测试结果

```
=== Step 2: init-worktree.py ===
rc=0, .gitignore + .sprint/ chmod 700 落地

=== Step 3: redact-report.py (恶意报告含 API key / 内网 IP / 注入 / PEM) ===
rc=1(找到 6 high + 2 medium, exit code 反映严重程度)

=== Step 4: validate-verdict.py (clean verdict) ===
rc=0, 接受

=== Step 5: validate-verdict.py (dirty verdict 含 <system-reminder> + ignore instructions) ===
rc=1, reject + 列 4 个注入点

=== Step 6: safe-worktree-remove.py (空 worktree) ===
rc=0, worktree 真删
```

**端到端 5 步全过。**

---

## 4. 关键设计决策(供未来参考)

### 4.1 不是"防御性脚本"而是"OS 层 enforcement"

整合评审 codex M1 / M2 都强调 v1.2 的"严禁 write 工具"只是 prompt 自我约束。S1.1-S1.5 的设计原则是**把约束从 prompt 移到 OS 层**:

| v1.2 约束形式 | v1.3 enforcement |
|---|---|
| prompt "严禁调 mcp-graph write" | S1.5: mcp-graph-readonly-proxy 在 stdin/stdout 层 reject |
| prompt "子代理不写 .env" | S1.4: init-worktree.py 强制写 .gitignore |
| prompt "不泄 token" | S1.1: redact-report.py 主动扫 + 替换 |
| prompt "verdict 真实" | S1.2: validate-verdict.py 主动扫注入 |

### 4.2 Windows 跨平台兼容性

5 个脚本都用 Python 3 stdlib,**没有第三方依赖**。`safe-worktree-remove.py` 特意做了 Windows + Unix 双 path(Windows 用 taskkill + PowerShell,Unix 用 lsof + SIGTERM)。`init-worktree.py` 跨平台 chmod(POSIX 用 `os.chmod(0o700)`,Windows fallback 到 `icacls`)。

### 4.3 P12 的核心洞察 — git 文件锁是天然最后防线

我之前以为必须100% 找子代理进程才能安全删 worktree。**实际上 git worktree remove 失败时返回 non-zero** + 报 "Permission denied",这就是天然的保护:**任何有进程占用时 git 会拒绝**。所以 `safe-worktree-remove.py` 的核心是:

- 尽力找子代理(可能有 false negative)
- 调 `git worktree remove` 试试
- 失败就 exit 1 + 报告,**绝不用 `shutil.rmtree` 强制**(这是 codex M3 的核心警告)

---

## 5. 下一步(整合评审剩余的 3 项 P0)

§1.2 可观测性 / §1.3 韧性 / §1.4 场景覆盖 三项还在等:
- §1.2 stream 机制 + progress 字段 + 进程级 metric
- §1.3 rollback / retry / circuit-breaker / cost 限制 / audit log / merge conflict
- §1.4 单文件 bug / debug / monorepo / hotfix fast-path

按你的"分阶段"决定,这些先停,等验证 §1.1 能落地后再来。

---

## 6. 验证方式(下次跑)

```bash
# 跑端到端测试
python scripts/init-worktree.py <worktree> --branch test
echo "sk-test api_key=sk-proj-12345" > .sprint/report.md
python scripts/redact-report.py .sprint/report.md --check-only   # exit 1
python scripts/validate-verdict.py .sprint/verdict.md             # exit 0 if clean
python scripts/safe-worktree-remove.py <worktree>                 # exit 0
```

— Arcadia Agent (M3), 2026-08-22 23:50 CST