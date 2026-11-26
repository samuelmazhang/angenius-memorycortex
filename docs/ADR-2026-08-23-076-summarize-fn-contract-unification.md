# ADR-076: 统一 `summarize_fn` 回调契约为 `list[str]`

- Status: Accepted
- Date: 2026-08-23
- Author: ExampleOrg Agent (执行) + 天行健 (决策)
- Supersedes: 无 (新决策,不影响其他 ADR)
- Depends on: ADR-068 (cortex-conflict-resolve) + V6.5.0 Phase 1 #C (summarize_chunks) + V6.5.0 Phase 2 #9 (RAPTOR build_tree)

## 背景 (Context)

cortex v6.5.0 引入两个独立的 consolidation 入口,二者的 `summarize_fn` 回调**类型签名不一致**,迫使任何想用一个函数服务两个调用点的操作者都必须写一个适配 shim。这是个真问题,不是潜在的:

| 入口 | 模块 | 文件 | `summarize_fn` 签名 |
|---|---|---|---|
| 长对话压缩 | `compact_long_conversation` → `summarize_chunks` | `server/src/consolidate/summary.py` | `summarize_fn(chunks: list[Dict]) -> str`,每个 dict 是 `{"id", "content", "importance"}` |
| RAPTOR 递归摘要 | `build_tree` | `server/src/consolidate/raptor.py` | `summarize_fn(texts: list[str]) -> str` |

集成测试 (`server/tests/integration/test_summary_raptor.py::test_contracts_diverge_known_design_fact`) 在 v6.5.0 阶段**显式 pin 了这个分歧**作为"已知设计事实",但这本身就是个反模式 —— pin 一个让用户必须写 shim 的不一致,等于承认它会持续制造问题。

2026-08-23 在做 B 层集成测试时,该 shim 模式被实际触发:同时需要给 `build_tree` 和 `summarize_chunks` 注入同一个 summarize 函数,被迫立刻写适配层。这是不一致从"理论问题"变成"实际摩擦"的转折点。

## 决策 (Decision)

**统一两个入口的 `summarize_fn` 契约为 `summarize_fn(texts: list[str]) -> str`,且这一形式是 v6.5.1+ 的唯一正式契约。**

具体改动:

1. `consolidate.summary.summarize_chunks` 内部把 normalized chunk 列表 → `texts = [c["content"] for c in sorted_chunks]` 后传给 `summarize_fn` (而不是传 normalized dict 列表)。
2. `raptor.build_tree` 契约**保持不变** (本来就已经是 `list[str]`)。
3. 新增 `consolidate.summary.adapt_dict_summarize_fn(dict_fn)` —— 一个 backward-compat 适配器,让 v6.5.0 时代的 dict-style 函数仍能用。文档里写明:这是过渡工具,**新代码不应使用**。
4. 集成测试 `test_summary_raptor.py` 从"pin 分歧"翻转为"pin 统一 + pin 适配器",反映新契约。
5. `SummarizeFn = Callable[[List[str]], str]` 类型别名加到 `consolidate.summary`,作为正式类型签名供 type checker / IDE 使用。

## 后果 (Consequences)

**正面**:
- 操作者写一个 `summarize_fn` 可以无 shim 地同时给 RAPTOR 和 compact 用,这是 v6.5.0 想给但没做到的。
- 减少"为什么我的函数不工作"的认知负担:类型签名统一后,新人不会再遇到"`list[Dict]` 进去的版本在 RAPTOR 上 crash"的踩坑。
- `SummarizeFn` 类型别名给 type checker 一个 anchor,IDE 自动补全更准。

**负面**:
- 调用 `summarize_chunks` 时如果想拿到 `id` / `importance` 信息,得另外从原 chunk 列表自己索引 —— 适配器 `adapt_dict_summarize_fn` 给的是空 id / 默认 importance。
- 任何在外部已经按 dict-style 写的 summarize 函数需要主动改写或包一层 `adapt_dict_summarize_fn`。这是有意为之的"小规模迁移成本",换来长期契约一致性。

**兼容性**:
- 默认 template 路径完全不变 (`summarize_fn=None` 时仍走 `DEFAULT_TEMPLATE` 渲染)。
- `compact_long_conversation` 的 caller **无需改动** —— 它只是 `summarize_fn=summarize_fn` 透传。
- 单测 `test_compact_v6_5.py` / `test_consolidate_v6_5.py` 全绿,`npm test` → **291 passed, 8 skipped, 0 failed**。

## 验证 (Verification)

| 验证项 | 结果 |
|---|---|
| 单测 + 集成测试 | 291 passed, 8 skipped, 0 failed (在 `.` 跑 `npm test`) |
| 集成测试 scenario 4 重写 | `test_summary_raptor.py` 现在 4 个测试: `test_raptor_summarize_fn_contract_is_list_of_strings` + `test_consolidate_summarize_fn_contract_is_list_of_strings` + `test_one_function_works_for_both_call_sites` + `test_adapt_dict_summarize_fn_backward_compat` |
| import 路径 | `from consolidate import adapt_dict_summarize_fn, SummarizeFn` 可用,见 `consolidate/__init__.py` `__all__` |
| 默认 fallback | `summarize_fn=None` 仍走 `DEFAULT_TEMPLATE`,行为不变 |
| 真环境 E2E | 见 ADR-076 部署日志 + `server/tests/integration/test_e2e_recall_loop.py` (D 层) |

## 不做的 (Out of Scope)

- 不改 `compact_long_conversation` 的内部行为 —— 它只是透传 `summarize_fn`,新契约对它无影响。
- 不强制把 `id` / `importance` 信息注入 `summarize_fn` —— 这是设计权衡:统一为最简单形式 `list[str]` 比塞 metadata 进去更可测、更易推理。如果未来需要,可以扩展为 `list[(id, text, importance)]` 二元/三元组,但目前没有驱动力。
- 不删除旧的 `consolidate.summarize_chunks` —— 那是 `consolidate.summary.summarize_chunks` 的 re-export,行为不变,只是契约升级了。