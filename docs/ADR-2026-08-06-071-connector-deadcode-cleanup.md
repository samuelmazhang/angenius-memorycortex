# ADR-071: connector/arcadia/cortex_api.py 死代码清理 (2026-08-06)

- Status: Accepted
- Date: 2026-08-06
- Author: Arcadia Agent (MiniMax-M3)
- Depends on: ADR-069 D5 列的"connector 死代码"遗留
- Supersedes: 无

## Context

ADR-069 §D5 列了"已知遗留":
> connector 死代码: `connector/arcadia/cortex_api.py` 自 V5.2.6 (commit 3bf8a59) 起 import `memory_chunk.py` 不到, 整个 `connector/arcadia/` 是孤儿代码

事实核查 (本轮 2026-08-06):

```bash
$ ls connector/arcadia/
README.md        cortex_api.py        cortex_memory_backend.py  memory_tool.py
__pycache__/     scripts/             tests/

$ grep -rn "cortex_api" --include="*.py" .
# 零引用
```

`cortex_api.py` 头部 import:
```python
from .memory_chunk import MemoryChunk, MemoryChunkCreate, MemorySearchRequest
from .metadata_store import MetadataStore
from .chroma_store import ChromaStore
from .memory_service import MemoryService
from .memory_rumination import MemoryRuminationService
```

**5 个被 import 的模块全都不在 `connector/arcadia/`** (目录只有 4 个文件: README/cortex_api/cortex_memory_backend/memory_tool)。
→ 该文件**无法 import**, 整文件是死代码。

## Decision

**立即删除** `connector/arcadia/cortex_api.py` (89KB, ~1700 行)。

### 保留

| 文件 | 用途 |
|------|------|
| `connector/arcadia/cortex_memory_backend.py` | (待评估, 可能是当前 backend) |
| `connector/arcadia/memory_tool.py` | MEMORY.md/USER.md 持久化 (Arcadia agent 个人笔记, 跟 cortex 无关) |
| `connector/arcadia/scripts/` | cron 脚本 |
| `connector/arcadia/tests/` | connector 测试 |

### 删除

| 文件 | 理由 |
|------|------|
| `connector/arcadia/cortex_api.py` | V2 时代 FastAPI app, 5 个 import 失败, 0 外部引用 |

### 不评估

`cortex_memory_backend.py` 是否真在用, **本 ADR 不动**, 需独立 ADR-073 评估 (next sprint)。

## Verification

```bash
# 删除前
$ grep -rn "cortex_api" --include="*.py" . | wc -l
0

# 删除
$ git rm connector/arcadia/cortex_api.py

# 删除后 imports 仍正常
$ python -c "from connector.arcadia import cortex_memory_backend, memory_tool" 2>&1
# 应 import 成功 (若失败: 回滚)
```

## Consequences

### 正面
- 移除 ~1700 行死代码, -89KB 仓库体积
- 减少新 contributor 的认知负担 (避免误以为 connector/arcadia 是 V2 时代入口)
- CI 不再 lint 这 1700 行

### 负面 / 风险
- 若有人**真**在外部分支/脚本里引用 `from connector.arcadia.cortex_api import ...` 会立即 break
  - 缓解: 仓库内 grep 已确认 0 引用; 仓库外若是用户私有脚本, 后续会从 import error 中发现
- 不可逆 (除非 git revert)

## Rollback

```bash
git revert <commit>  # 恢复 cortex_api.py
```

无数据库 schema 变更, 无 config 变更, **纯文件删除**, rollback 风险接近 0。
