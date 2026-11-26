# Codex 评审 — 工具集成 + 安全视角

## 外部参考来源
1. Claude Code: Concurrent sub-agents fail to spawn (claudeissues.com/3499) — 并发 subagent 共享 turn_id 导致卡死
2. Qoder forum bug: 并行 subagent 共享相同 turn_id (forum.qoder.com/8409) — 同上,不同项目复现
3. Anthropic Claude Code issue #64346 — parallel tool calls 一个良性错误取消所有兄弟调用,agent 误读 shell 坏掉
5. dev.to/jupiteros_ai — Why Running Multiple AI Coding Agents Creates Chaos
6. kanbots.dev — Spawn/parallelize 多 agent 安全实践
7. parallelcode.app — 多 agent 同一 repo 实践

## 安全威胁清单(按风险排序)
高风险:
- M1: prompt 注入 → report.md 把内部 token / IP / stack trace 回写主 hermes,无 redaction
- M2: reviewer 自身可被污染 — hermes leaf reviewer 读 report.md + diff 提 verdict,这俩都来自未受信子代理
- M3: worktree 删除与子代理进程竞态 — unlink 后子代理仍写 inode
- M4: .env 自动提交 — 子代理 `git add .` 会把 .env 拉进暂存

低风险:
- L1: npm cache 锁 — 多 worktree 共享全局 cache 时偶发 corrupt

## 工具集成问题
问题 1(严重): mcp-graph 写权限"君子协定"
- 原因: 本地 SQLite MCP server 无 per-caller ACL;铁律 2 完全依赖子代理听话
- 外部参考: issue #34645 + Stawinski RCE + GridTheGrey indirect injection
- 建议: 拆 read/write 两套 MCP server,主 hermes 只暴露 write,子代理配置里只挂 read

问题 2(严重): acp-subagent.sh 路径处理无 sanitization
- 原因: task-id 来源未限,直接拼到 --prompt-file;worktree 路径无 realpath 校验
- 建议: UUID v7 + shell 数组传参 + realpath 校验路径前缀

## 缺失的安全实践
- 无 caller-identity 透传(unix socket peer creds / process pid → token)
- 无 syscall 级 sandbox(seccomp/AppArmor/macOS sandbox-exec),h5i 已有参考
- 无 report.md redaction(扫 token=/Bearer/BEGIN PRIVATE KEY/内网 IP 才入库)
- 无 .sprint/ 目录 per-task 权限(chmod 700)
- 无 worktree 销毁前的进程 kill-and-wait 顺序
- 无 .gitignore 在 worktree 创建时强制写 .sprint/ 和 .env

## 反驳现有红线
- 铁律 2 是形式上的 — 仅 prompt 文字"严禁调 mcp-graph 写工具",无 daemon 校验
- P2 "文件不在 <worktree>/ 下视为越界" — prompt 而非 OS 约束
- 铁律 3 "主 hermes 亲自验收" — 流程正确但 verdict 输入仍受 M2 污染

## 重要: 这是 codex 评审产出(主 hermes 已存档)
