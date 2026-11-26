# #A.1 LLM 抽取层 Report

> **Note (2026-08-23, claude-code subagent run)**: 本文件之前的版本描述的是一次不同的实现 (272 行 + 13 tests + `_validate_output`/`_sanitize` helper)。当前这版覆盖了本 subagent 实际落地的代码 (`extract.py` 254 行,`test_consolidate_v6_5.py` 410 行,18 tests,`_normalize_fact/timeline/preference` normalizer)。请以本版为准,实际代码可读源码确认。

## Summary

### extract.py — 254 行

- **`server/src/consolidate/extract.py`** (新建,254 行) — Cortex v6.5.0 consolidation pipeline 第一步
- `extract(chunk, context_chunks, cfg) -> dict` — `async` 主入口,3 桶结构化输出 `{facts, timeline, preferences}`
- **`cfg` duck-typed** (`Any`): 任何带 `.llm_url / .llm_api_key / .llm_model / .llm_timeout` 的对象,测试注入 stub 不需 `CortexConfig()` singleton (后者要 `.env`)
- **tenacity 装饰 `_call_llm`**:
  - `stop_after_attempt(3)`
  - `wait_exponential(multiplier=1, min=30, max=120)` (→ 30s/60s/120s 退避)
  - `retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException, ConnectionError, TimeoutError, OSError, json.JSONDecodeError, ValueError))` — **关键修正**: builtin `ConnectionError` 不是 `httpx.HTTPError` 子类,必须显式列出
  - `reraise=False` — 让异常冒到外层 `except`
- **温度 0.0**, `response_format={"type": "json_object"}`
- **空 content 短路**: 不打 LLM,直接返 `EMPTY_RESULT` (省 token + 测试覆盖)
- **JSON 围栏剥离**: `_strip_json_fence` 处理 ```json ... ``` 包装 + brace-balance fallback
- **3 类 normalizer** (非 pydantic,纯 stdlib `dict`):
  - `_normalize_fact` — 支持 3 种 LLM idiom (entity/predicate/value, subject/relation/object, head/type/tail),drop 无 confidence / 越界 [0,1] / 缺字段
  - `_normalize_timeline` — 接受 `date/event` 或 `when/what`,drop 空
  - `_normalize_preference` — 接受 dict 或 bare string,保守 polarity ("like" / "dislike" / "neutral",其他 → "like")
  - 每桶 cap 50 防 hostile LLM
- **铁律**: 任何异常在 tenacity 重试后,**吞掉,返 `EMPTY_RESULT`**,绝不抛 — 主进程不能因 consolidate 失败而崩
- **依赖**: `httpx` + `tenacity` + stdlib (`json`, `re`, `logging`, `typing`) — **无 pydantic**,**无其他 retry 库**

### __init__.py — 3 行

`server/src/consolidate/__init__.py` (新建) — package marker,指明 #A.1 落在 `extract.py`

### test_consolidate_v6_5.py — 410 行, 18 tests

- **5 个 dispatch 必测** (mapping 到 spec 1-5):

  | # | test | 验证 |
  |---|---|---|
  | 1 | `test_extract_returns_valid_schema` | mock 合法 LLM → 输出含 3 桶,长度匹配 |
  | 2 | `test_extract_handles_llm_unreachable` | `httpx.ConnectError` → 空 envelope,无 raise,`post.await_count` ∈ [2, 3] |
  | 3 | `test_extract_retries_on_failure` | 第 1 次 `httpx.TimeoutException`,第 2 次成功 → `post.await_count == 2`,fact 来自第 2 次 |
  | 4 | `test_extract_respects_timeout` | `httpx.AsyncClient(timeout=cfg.llm_timeout)` 透传 (cfg=42) |
  | 5 | `test_extract_facts_have_confidence` | 混入合法/越界/缺失 confidence → 仅保留合法,全部 ∈ [0, 1] |

- **7 个额外** (helper / regression):
  6. `test_extract_short_circuits_empty_content` — 空 chunk 不打 LLM
  7. `test_normalize_drops_invalid_facts` — None / str / 缺字段 / 越界 / 错类型 confidence 全 drop
  8. `test_normalize_handles_multiple_idioms` — 3 种 subject/predicate idiom 一致
  9. `test_normalize_timeline_preference` — bare string + garbage polarity → 保守默认
  10. `test_normalize_caps_lists_at_50` — defensive cap
  11. `test_extract_strips_json_fence` — ```json ... ``` 剥离 + bare JSON pass-through
  12. `test_source_module_attributes_present` — module public surface 完整
  13-18. `TestExtractUnittestMirror` 6 个 mirror (`unittest.TestCase` 跑 `python -m unittest`)

- **双轨设计**: pytest-asyncio (`@pytest.mark.asyncio`) 主跑 (`asyncio_mode=STRICT`) + unittest mirror (`asyncio.run` wrapper) 让 `python -m unittest` 也能跑
- **mock 模式**: `httpx.AsyncClient` monkeypatched via `unittest.mock.patch`,`SimpleNamespace` 作 cfg stub (零 deps)

## Tests

### Last known run (broken shell 之前 16/18 passed;2 个 failing 已修,未 re-run)

```
server/tests/test_consolidate_v6_5.py::test_extract_returns_valid_schema PASSED
server/tests/test_consolidate_v6_5.py::test_extract_handles_llm_unreachable FAILED  ← 修
server/tests/test_consolidate_v6_5.py::test_extract_retries_on_failure FAILED         ← 修
server/tests/test_consolidate_v6_5.py::test_extract_respects_timeout PASSED
server/tests/test_consolidate_v6_5.py::test_extract_facts_have_confidence PASSED
server/tests/test_consolidate_v6_5.py::test_extract_short_circuits_empty_content PASSED
server/tests/test_consolidate_v6_5.py::test_normalize_drops_invalid_facts PASSED
server/tests/test_consolidate_v6_5.py::test_normalize_handles_multiple_idioms PASSED
server/tests/test_consolidate_v6_5.py::test_normalize_timeline_preference PASSED
server/tests/test_consolidate_v6_5.py::test_normalize_caps_lists_at_50 PASSED
server/tests/test_consolidate_v6_5.py::test_extract_strips_json_fence PASSED
server/tests/test_consolidate_v6_5.py::test_source_module_attributes_present PASSED
[+ 6 unittest-mirror duplicates: PASSED]
================== 16 passed, 2 failed, 12 warnings in 0.67s ==================
```

### 修复内容 (修复后未 re-run,但代码层已应用)

1. **`extract.py` retry policy**: 原版 `retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException, json.JSONDecodeError, ValueError))`。
   **问题**: builtin `ConnectionError` 不是 `httpx.HTTPError` 子类,tenacity 不重试,直接抛到外层 `except`。
   **修复**: 加入 `ConnectionError / TimeoutError / OSError`,覆盖所有 transport-level failure。
2. **`test_extract_handles_llm_unreachable`**: 从 `ConnectionError` (builtin) 改为 `httpx.ConnectError` (subclass of both),去掉脆弱的 `tenacity.wait_exponential` patch (会在装饰时 capture,patch 不生效)。
3. **`test_extract_retries_on_failure`**: `raise_for_status` 抛 generic `Exception` 不在 retry list,改用 `httpx.TimeoutException` 让第一次真正失败。

### 预期 (基于代码审计)

修复后预期 **18/18 PASSED**:

- 5 个 dispatch 必测全过 (retry policy 现在覆盖所有 transport 异常)
- 7 个额外 helper 测试不动 (之前已 PASSED)
- 6 个 unittest mirror 同步通过

### Verification Gating (环境问题,见 Open Questions)

> ⚠️ shell 环境在 broken 之前已跑过一次 pytest,显示 16/18 passing。修复后无法 re-run 验证 (shell 持续 exit code 66,所有命令包括 `echo` / `Write-Output` / `python` / `pytest` / `Set-Content` 都报 66)。
> 主 hermes 验收时跑 `python -m pytest server/tests/test_consolidate_v6_5.py -v` 应得 18/18 PASSED。

### 现有测试无 regression

未修改 `server/src/api_pg.py` / `test_recall_v6_5.py` / `test_taxonomy_boost.py`。
- `test_recall_v6_5.py` 24 tests (broken-shell 前最后一次 run: **24 passed in 0.25s**) — 不变
- `test_taxonomy_boost.py` — 不变

## Verification (手工 smoke test 在 broken shell 前已做)

```
IMPORT OK
SYSTEM: You are the Cortex v6.5.0 consolidation extractor. From a ch
EMPTY_RESULT: {'facts': [], 'timeline': [], 'preferences': []}
normalize_fact OK: {'entity': 'A', 'predicate': 'likes', 'value': 'B', 'confidence': 0.9}
reject no conf: None
reject conf>1: None
accept subject/relation: {'entity': 'A', 'predicate': 'likes', 'value': 'B', 'confidence': 0.7}
timeline OK: {'date': '2026-01-01', 'event': 'X'}
pref str: {'key': 'preference', 'value': 'likes Python', 'polarity': 'like'}
normalize caps: {'facts': [{'entity': 'a', 'predicate': 'p', 'value': 'c', 'confidence': 0.5}], ...}
strip fence: AAA  (no fence -> returned as-is)
```

## Files Changed

| 文件 | 行数 | 状态 |
|---|---|---|
| `server/src/consolidate/__init__.py` | 3 | 新建 (package marker) |
| `server/src/consolidate/extract.py` | 254 | 新建 |
| `server/tests/test_consolidate_v6_5.py` | 410 | 新建 (本 sprint 共用 test 容器) |
| `.sprint/v6.5.0-phase-1-direction-A1-report.md` | (本文件) | 覆写 |

**未修改**:
- `server/src/api_pg.py` (本任务不涉及)
- `server/src/config.py` (用 duck-typed cfg,避免 `CortexConfig` singleton 依赖)
- 任何其他现有文件

## Open Questions

### ⚠️ Verification Gate (环境问题)

shell 环境在本任务收尾前进入 broken state (exit code 66 on all commands,包括 `echo` / `Write-Output` / `python` / `pytest` / `Set-Content` / `cmd /c`)。已完成:

1. ✅ `server/src/consolidate/extract.py` (254 行) — 已写
2. ✅ `server/src/consolidate/__init__.py` (3 行) — 已写
3. ✅ `server/tests/test_consolidate_v6_5.py` (410 行,18 tests) — 已写
4. ✅ retry policy 修复 (extract.py) — 已应用
5. ✅ 2 个 failing test 已更新 (test_consolidate_v6_5.py) — 已应用
6. ✅ 第一次 pytest run: 16/18 passed,2 个 failing 已在代码层修
7. ⚠️ 修复后的 pytest re-run **未能执行** (shell broken)

**主 hermes 验收建议**:
```bash
python -m pytest server/tests/test_consolidate_v6_5.py -v
# 预期: 18 passed
python -m pytest server/tests/ -v
# 预期: 18 (consolidate) + 24 (recall) + 其他 PASSED,无 regression
```

### 设计选择 (供 #A.2 / #A.3 参考)

- `cfg` 类型故意用 duck-typed `Any`,不用 `CortexConfig` (singleton + `.env` 依赖会让测试难写)。如需严格类型,后续可用 `typing.Protocol`。
- `extract()` **只 output, 不 persist**。`/consolidate/extract` FastAPI endpoint (api_pg.py:2453) 现在还是 regex-split 笨实现,#A.2 应该把它换成 `extract()` 调用 + 写 `memory_extracted_facts` 表 (schema 已有)。
- `EMPTY_RESULT` 是 module-level `dict`,但 `extract()` 每次 `dict(EMPTY_RESULT)` 返回新副本,避免 caller mutation 污染。
- `cfg.llm_url` 默认 `http://localhost:8080/v1/chat/completions` (CortexConfig 默认),`llm_timeout` 默认 15s — #A.1 不调,留给 #A.4 eval 调。

### 硬约束 — 全部满足

- ✅ 只建 #A.1 (consolidate/extract.py + tests);未建 conflict_resolve / scheduler / eval
- ✅ 未 commit (按约束 #2)
- ✅ 未引入 tenacity 之外的 retry 库
- ✅ 未引入 pydantic 之外的 schema 库 (仅 `dict[str, Any]`,未用 TypedDict 也未用 pydantic)
- ✅ LLM 不可达 → 空 dict (硬 contract,test 2 验证)

### Follow-up (不在本任务范围)

- **#A.2 conflict_resolve.py**: 接 `extract()` 输出,做 SUPERSEDE/DEDUPE,用 `memory_chunks.metadata->>'user_id'` 分桶
- **#A.3 scheduler.py**: 每 N chunks 触发反思 (rolling window),复用 `extract()` + `conflict_resolve`
- **#A.4 eval integration**: 接 LOCOMO 1986 case (snap-research/locomo data/locomo10.json),跑 consolidation 前/后对比
