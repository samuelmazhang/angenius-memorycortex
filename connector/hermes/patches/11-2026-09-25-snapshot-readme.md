# 2026-09-25 hermes-agent 本地修改快照

> 同步来源: `A:\Application\Hermes-Agent\Program\hermes-agent\` (本地未提交改动)
> hermes HEAD: `6e07eb48387044dbcaf12490931c2b8ca7ec8653` (main, 落后 origin 1 commit)

## 同步策略

本目录是 hermes ↔ cortex 集成的 source-of-truth 备份,被 `RECOVERY.md` 用于
恢复。两类文件分开处理:

### A. connector 自有文件 (3 个) → 字节级覆盖

这些文件本来就是 connector/hermes 自己维护的,直接 `cp` 同步:

| 源 (hermes-agent/tools) | 目标 (connector/hermes) |
|---|---|
| `tools/cortex_memory_backend.py` | `cortex_memory_backend.py` |
| `tools/memory_tool.py` | `memory_tool.py` |
| `tools/session_summarizer.py` | `session_summarizer.py` |

同步后 sha256 已验证 (见下方)。这些是 hermes-agent 实际跑的那份代码的
镜像,以后 hermes 这边被覆盖/丢失 → 从 connector/hermes 拷回去即可。

### B. hermes 自身代码的本地修改 (6 个) → 存为 .patch

这些是 hermes 自身仓库的改动,直接覆盖 connector/hermes 没意义(那里没有
这些文件)。所以保存成可重放的 unified diff,以后从 origin/main pull 之后
用 `git apply` 一键回放:

| Patch 文件 | 目标文件 | 行数 |
|---|---|---|
| `06-agent_init-mem-config-direct-load.patch` | `agent/agent_init.py` | +14/-8 |
| `07-cli-cortex-takeover-cli-flag.patch` | `cli.py` | +10/-6 |
| `08-doctor_state-cortex-status.patch` | `hermes_cli/doctor_state.py` | +9/-7 |
| `09-memory_tool_store-cortex-gate-inversion.patch` | `tools/memory_tool_store.py` | +8/-5 |
| `10-test_memory_tool-cortex-takeover-tests.patch` | `tests/tools/test_memory_tool.py` | +52/-13 |
| `HERMES-MODIFIED-2026-09-25.full.diff` | 上面 5 个 + memory_tool.py 的差异 | 233 行 (不含 memory_tool.py 3222 行的内部变化) |

> 注: `memory_tool.py` 自身在 git status 中显示为 modified (+3222/-266),
> 但 connector/hermes/memory_tool.py 是 connector 自己的版本,二者独立维护
> (见 `README.md` "do not overwrite one with the other wholesale")。本次
> 同步以 connector/hermes 的 A 类文件覆盖为准 —— A 已把 hermes-agent/tools
> 的当前最新版本镜像过来。

## 怎么恢复 (未来 hermes 这边被覆盖后)

```bash
# 1) 把 3 个 connector 文件拷回 hermes-agent/tools/
cd /e/MyDevelop/Angenius-Agent/memory-cortex/connector/hermes
cp memory_tool.py          "A:/Application/Hermes-Agent/Program/hermes-agent/tools/memory_tool.py"
cp cortex_memory_backend.py "A:/Application/Hermes-Agent/Program/hermes-agent/tools/cortex_memory_backend.py"
# session_summarizer 用专属安装脚本(还会改 agent_init 等)
python scripts/install_session_summarizer.py sync

# 2) 在 hermes-agent 仓里 apply 5 个 patch
cd "A:/Application/Hermes-Agent/Program/hermes-agent"
git apply /e/MyDevelop/Angenius-Agent/memory-cortex/connector/hermes/patches/06-agent_init-mem-config-direct-load.patch
git apply /e/MyDevelop/Angenius-Agent/memory-cortex/connector/hermes/patches/07-cli-cortex-takeover-cli-flag.patch
git apply /e/MyDevelop/Angenius-Agent/memory-cortex/connector/hermes/patches/08-doctor_state-cortex-status.patch
git apply /e/MyDevelop/Angenius-Agent/memory-cortex/connector/hermes/patches/09-memory_tool_store-cortex-gate-inversion.patch
git apply /e/MyDevelop/Angenius-Agent/memory-cortex/connector/hermes/patches/10-test_memory_tool-cortex-takeover-tests.patch

# 3) 验证
python -c "from tools.memory_tool import MemoryStore; ms = MemoryStore(); print(ms._is_cortex_available())"
```

## sha256 (本次同步后立即验证)

```
6ba9dc787074cda9f9c5888de8f48fc4c0e9b7d7edbc0189619481cc18fb2c8a  cortex_memory_backend.py
f7a37def9dfd68510bb5075d67df7c1a3b77fa7a1986a1b52ba1b250ee31625a  memory_tool.py
3d3a58f2cd107b937274e3adb748f62b1e61ca5ff28841a951c93f2deb61e777  session_summarizer.py
```

## 备注: 没在这次同步里的东西

- `tools/memory_tool.py.backup-20260908-000034-...` 和 `memory_tool_store.py.backup-...`
  是 2026-09-08 takeover-deploy 时留的旧版本备份,本地还有,connector/hermes
  不需要 —— 那些就是 .py 文件的 .bak,功能上等价于"已经回滚过的版本",
  未来如果需要查某个旧版本直接 grep 关键字。
- `workflow-graph/` 是 Hermes 工作流图谱缓存目录,运行时数据,不属于
  hermes-agent 源代码,跳过。
- 那些以 `, is_cortex_active())` 之类开头的文件名(重定向残留)是从
  echo 输出截断产生的垃圾文件,不是 hermes 实际修改,跳过。
