# workflow-graph/tasks/

每个 multi-cli task 一个独立子目录。

## 目录结构

```
multi-cli-v1.0-to-v1.6/
├── dispatch-prompt.md    # 主 arcadia → 子代理的原始 prompt
├── spec.md               # task spec / metadata
├── subagent-report.md    # 子代理返回的 report
├── reviewer-verdict.md   # reviewer verdict
├── decision-log.md       # 主 arcadia 关键决策
├── progress.json         # 心跳进度(从 worktree .sprint/ 复制)
├── audit.jsonl           # 统一 audit log
└── artifacts/            # 子代理产物 (diff / screenshot / test output)
```

## 创建 task

```bash
python create-task-dir.py --name <task-name> [--scenario <s>] [--cli <c>]
```

## 列出所有 task

```bash
python create-task-dir.py list
```

## 注册 worktree 进度到 task-dir

```bash
python create-task-dir.py --name <task-name> --register-from <worktree>
```
