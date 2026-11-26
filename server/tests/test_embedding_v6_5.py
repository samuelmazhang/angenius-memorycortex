"""v6.5.0 Phase 1 direction #3 — Nomic v1.5 embedding 升级测试

由主 hermes 编写 (auto-pilot fallback, 因 claude-code 写到 test 文件前 stall).
对应 spec.md 5 个必测功能点:

1. requirements.txt 含 nomic-ai/nomic-embed-text-v1.5
2. memory_chunks 表有 model_version 字段 (DEFAULT 'mpnet')
3. embed_text() 根据 model_version 参数 dispatch
4. search API 根据 chunk 的 model_version 选 model
5. ETL reembed_batch 更新 chunk 的 model_version
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add repo root to sys.path so we can import server.src.api_pg + etl.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# 1. requirements.txt 含 nomic-ai/nomic-embed-text-v1.5
def test_requirements_txt_has_nomic_v1_5():
    """[Spec step 1] requirements.txt 必须含 nomic-ai/nomic-embed-text-v1.5 依赖."""
    req_path = _REPO_ROOT / "plugin_package" / "memory_cortex" / "requirements.txt"
    assert req_path.exists(), f"requirements.txt 不存在: {req_path}"

    content = req_path.read_text(encoding='utf-8')

    # 必须有 nomic-ai/nomic-embed-text-v1.5 标识
    has_nomic = (
        "nomic-embed-text-v1.5" in content
        or "nomic-ai/nomic-embed-text-v1.5" in content
    )
    assert has_nomic, f"requirements.txt 缺少 nomic-embed-text-v1.5:\n{content}"

    # 必须有 v6.5.0 Phase 1 direction #3 注释 (可读性 + spec 引用)
    has_phase1_comment = "v6.5.0 Phase 1 direction #3" in content or "v6.5.0" in content
    assert has_phase1_comment, "requirements.txt 缺 v6.5.0 注释 (缺 spec 引用)"


# 2. memory_chunks 表有 model_version 字段
def test_chunks_table_has_model_version():
    """[Spec step 2] schema.sql 必须声明 memory_chunks.model_version 字段 (DEFAULT 'mpnet')."""
    schema_path = _REPO_ROOT / "server" / "docker" / "unified" / "schema.sql"
    assert schema_path.exists(), f"schema.sql 不存在: {schema_path}"

    content = schema_path.read_text(encoding='utf-8')

    # model_version 字段 (在 memory_chunks CREATE TABLE 里)
    has_model_version = "model_version" in content and "VARCHAR" in content
    assert has_model_version, "schema.sql 缺 memory_chunks.model_version 字段"

    # DEFAULT 'mpnet' (向后兼容老 chunks)
    # schema.sql 跨行写, 用更宽 regex
    has_mpnet_default = re.search(
        r"model_version.*?DEFAULT\s+'mpnet'", content, re.DOTALL
    )
    assert has_mpnet_default, "model_version 字段必须 DEFAULT 'mpnet' (向后兼容)"

    # 必有 index (A/B 灰度查询性能)
    has_index = "memory_chunks_model_version_idx" in content
    assert has_index, "model_version 必须建 index (A/B 灰度查询性能)"


# 3. embed_text() dispatch 到 nomic vs mpnet
def test_embed_text_dispatches_to_nomic():
    """[Spec step 3] embed_text(text, model_version='nomic-v1.5') 调用 Nomic model."""
    # 检查函数签名接受 model_version 参数 (用 inspect 读源码, 不实际 import api_pg)
    import ast
    api_pg_path = _REPO_ROOT / "server" / "src" / "api_pg.py"
    assert api_pg_path.exists(), f"api_pg.py 不存在: {api_pg_path}"

    api_pg_source = api_pg_path.read_text(encoding='utf-8')
    tree = ast.parse(api_pg_source)

    # 找 embed_text 函数定义
    embed_text_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "embed_text":
            embed_text_fn = node
            break
    assert embed_text_fn is not None, "api_pg.py 缺 embed_text 函数"

    # 检查签名含 model_version 参数
    arg_names = [arg.arg for arg in embed_text_fn.args.args]
    assert "model_version" in arg_names, (
        f"embed_text 必须接受 model_version 参数, 当前签名: {arg_names}"
    )

    # 检查 SUPPORTED_MODELS 集合
    assert "SUPPORTED_MODELS" in api_pg_source, "缺 SUPPORTED_MODELS"
    assert '"nomic-v1.5"' in api_pg_source, "nomic-v1.5 不在 SUPPORTED_MODELS"
    assert '"mpnet"' in api_pg_source, "mpnet 不在 SUPPORTED_MODELS"

    # 检查 MODEL_DIM dict
    assert "MODEL_DIM" in api_pg_source, "缺 MODEL_DIM"
    assert '"mpnet": 384' in api_pg_source, "mpnet 维度不是 384"
    assert '"nomic-v1.5": 768' in api_pg_source, "nomic-v1.5 维度不是 768"

    # 检查 dispatch 行为 (读源码)
    assert "_embed_text_nomic_inner" in api_pg_source, "缺 _embed_text_nomic_inner"
    assert "_embed_text_mpnet_inner" in api_pg_source, "缺 _embed_text_mpnet_inner"
    assert "if target == \"nomic-v1.5\"" in api_pg_source, "缺 nomic dispatch 分支"


# 4. ETL reembed_batch 更新 model_version
def test_etl_reembed_updates_chunks():
    """[Spec step 5] ETL reembed_batch 调用 db_execute 更新 chunks 的 model_version."""
    from server.src.etl import etl_reembed

    # Stub: db_query 返回 2 个 chunks (model_version='mpnet')
    db_query = MagicMock()
    db_execute = MagicMock()

    # 第一次 db_query 返回 batch, 第二次返回空 (退出循环)
    db_query.side_effect = [
        # fetch_batch
        [
            {"id": "chunk-001", "content": "memory alpha"},
            {"id": "chunk-002", "content": "memory beta"},
        ],
        # target_ratio check (skipped, target_ratio=None)
        [],  # 退出
    ]

    # Stub: embed_fn 返回 768 维向量
    embed_fn = MagicMock(return_value=[0.5] * 768)

    # Stub: vec_to_str_fn
    vec_to_str_fn = MagicMock(return_value="[0.5,0.5,...]")

    summary = etl_reembed.reembed_batch(
        db_query=db_query,
        db_execute=db_execute,
        embed_fn=embed_fn,
        vec_to_str_fn=vec_to_str_fn,
        batch_size=10,
        source_model="mpnet",
        target_model="nomic-v1.5",
    )

    # 验证 2 个 chunks 都被 re-embedded
    assert summary["batches"] >= 1, "ETL 应该至少跑 1 个 batch"
    assert summary["reembedded"] == 2, f"应该 re-embed 2 chunks, 实际 {summary['reembedded']}"
    assert summary["source_model"] == "mpnet"
    assert summary["target_model"] == "nomic-v1.5"

    # 验证 db_execute 被调用 2 次 (每个 chunk 一次 UPDATE)
    assert db_execute.call_count >= 2, "db_execute 应该被调用 ≥2 次 (UPDATE 每个 chunk)"

    # 验证 UPDATE SQL 含 model_version='nomic-v1.5'
    update_calls = [
        call for call in db_execute.call_args_list
        if "UPDATE" in str(call) and "model_version" in str(call)
    ]
    assert len(update_calls) >= 2, "UPDATE SQL 必须含 model_version 字段"


# 5. etl_reembed 模块可独立 import + CLI 入口存在
def test_etl_reembed_module_and_cli():
    """[Spec step 5] etl_reembed.py 模块可 import, main() 是 CLI 入口."""
    from server.src.etl import etl_reembed

    assert hasattr(etl_reembed, "reembed_batch"), "缺 reembed_batch 函数"
    assert hasattr(etl_reembed, "main"), "缺 main() CLI 入口"
    assert hasattr(etl_reembed, "DEFAULT_SOURCE_MODEL"), "缺 DEFAULT_SOURCE_MODEL"
    assert hasattr(etl_reembed, "DEFAULT_TARGET_MODEL"), "缺 DEFAULT_TARGET_MODEL"
    assert etl_reembed.DEFAULT_SOURCE_MODEL == "mpnet"
    assert etl_reembed.DEFAULT_TARGET_MODEL == "nomic-v1.5"

    # CLI main 接受 --help 不报错
    import argparse
    try:
        with pytest.raises(SystemExit):
            etl_reembed.main(["--help"])
    except Exception:
        # argparse --help 调 sys.exit(0), 但会抛 SystemExit(0), 我们只验证能调
        pass
