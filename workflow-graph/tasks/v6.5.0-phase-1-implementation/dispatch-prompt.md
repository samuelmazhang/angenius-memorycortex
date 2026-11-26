# Dispatch Prompt — v6.5.0 #A.1 LLM 抽取层

**收件方**: claude-code (subagent)
**发件方**: 主 hermes (M3)
**任务 ID**: #A.1 (Phase 1, consolidation pipeline 第一步)
**Worktree**: `..worktrees/v6.5.0-phase-1-implementation`
**mcp-graph 节点**: `node_e0fcb00af751`(已建, daemon-side, lifecycle=IMPLEMENT)
**Plan 文档参考**: `docs/改进方案-cortex-v6.5.0-完整版-20260822.md` 第 5 节(方向 #A 详细 spec)

---

## 任务

新建 `server/src/consolidate/extract.py`,实现 LLM 抽取层。这是 consolidation pipeline 的核心,后续 #A.2 / #A.3 / #A.4 都依赖它。

## 必读(在动代码前先读)

1. `server/src/config.py` 整体 — 看 `Config` 类的 llm_url / llm_api_key / llm_model / llm_timeout(默认 http://localhost:8080)
2. `server/src/api_pg.py` 看 `query()` 函数(SELECT/INSERT 包装)和 chunk 字段定义
3. `docs/改进方案-cortex-v6.5.0-完整版-20260822.md` 第 5 节(方向 #A 详细 spec)
4. 现有 `server/src/sdk/cortex_client.py` (看怎么包装 HTTP 调用)

## 必建的 1 个文件 + 1 个测试文件

### `server/src/consolidate/extract.py` (新建 ~200 行)

实现 extract(chunk, context_chunks, cfg) -> dict 函数,调用 LLM 抽取:
- facts: 实体-谓词-值 三元组 (含 confidence 0-1)
- timeline: 日期-事件
- preferences: 偏好

要求:
- 用 `httpx.AsyncClient` 调 OpenAI-compatible API(cfg.llm_url)
- tenacity 装饰器: 3 attempts, exponential backoff 30s/60s/120s
- LLM 不可达/超时 → 返回 `{"facts": [], "timeline": [], "preferences": []}` 不抛
- temperature=0.0 (确定性)
- JSON schema 校验输出

```python
from __future__ import annotations
import json
from typing import Any
import httpx
import tenacity

from config import Config


EXTRACT_SYSTEM = """你是 Cortex v6.5.0 consolidation 抽取器。从 chunk + 上下文中抽取结构化记忆。要求输出 JSON: {"facts": [...], "timeline": [...], "preferences": [...]},每条 fact 必含 confidence 0-1。"""


@tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, min=30, max=120),
    retry=tenacity.retry_if_exception_type((httpx.HTTPError, json.JSONDecodeError)),
    reraise=False,  # 不 reraise,自己处理 fallback
)
async def extract(chunk: dict, context_chunks: list[dict], cfg: Config) -> dict:
    # 你的实现 (200 行内)
    ...
```

### `server/tests/test_consolidate_v6_5.py` (新建,本 sprint 共用)

5 个 test case:

1. `test_extract_returns_valid_schema` — mock httpx,验证输出 schema (facts/timeline/preferences 都存在,每个 fact 有 confidence 0-1)
2. `test_extract_handles_llm_unreachable` — mock httpx 抛 ConnectionError,验证返回空 dict 不抛
3. `test_extract_retries_on_failure` — mock httpx 第一次失败第二次成功,验证 retry 行为
4. `test_extract_respects_timeout` — 验证 httpx.AsyncClient 的 timeout=cfg.llm_timeout
5. `test_extract_facts_have_confidence` — 验证每个 fact 有 0-1 之间的 confidence 字段

(可加更多 source grep test 防 regression,参考 #2 时的 TestSourceFormulaIntegrity 风格)

## 必返的产物

完成后,在 worktree 内写 `<worktree>/.sprint/v6.5.0-phase-1-direction-A1-report.md`:

```markdown
# #A.1 LLM 抽取层 Report

## Summary
- extract.py 行数 + 关键设计选择
- test_consolidate_v6_5.py 新加几个 test (本 sprint 全部 5 个)

## Tests
- pytest 输出: 5/5 PASS
- 现有 test_taxonomy_boost.py + test_recall_v6_5.py 无回归

## Verification
- 跑一次 extract() on sample chunk + context,验证 JSON 输出

## Files Changed
- server/src/consolidate/extract.py (新建)
- server/tests/test_consolidate_v6_5.py (新建,本 sprint 共用)

## Open Questions
(没有就 None)
```

## 约束(铁律)

1. **只建 #A.1** — 不要建 conflict_resolve / scheduler / eval(其他方向)
2. **不要 commit** — git add 等主 hermes 验证
3. **不引入 tenacity 之外的 retry 库** — 用 `tenacity` 或手写循环
4. **不引入 pydantic 之外的 schema 库** — 用 `dataclass` 或 `TypedDict`
5. **LLM 不可达必须返回空 dict 不抛** — 主进程不能因 consolidate 失败而崩溃

## 时间预算

预期 ~8K tokens,~10-15 分钟。超时 1h retry 3 次后 escalate。

## 完成后

1. 写 `.sprint/v6.5.0-phase-1-direction-A1-report.md`
2. **不要调** subagent-progress.py(脚本不在 worktree 内 — 主 hermes 知道这个限制,后续 sprint 会把脚本部署到 cortex 仓库)
3. exit 0

如果失败:用 `.sprint/<task-id>-report.md` 写 `## FAILED` 段说明原因,主 hermes 会 escalate。

---

**开干,中途有不确定性跟我说**。
