"""v6.5.0 Phase 1 direction #5 — HNSW m=16 → m=24 retune tests.

Spec covers 4 changes:
1. server/docker/unified/schema.sql: HNSW index declared with (m=24, ef_construction=200)
2. server/src/migrations/2026_08_23_hnsw_m24.sql: DROP+CREATE index with m=24
3. server/src/config.py: HNSW_M=24 + HNSW_EF_SEARCH=120 constants (module-level + class)
4. server/src/api_pg.py: startup hook runs `SET hnsw.ef_search = 120`

Tests do not require a live Postgres connection.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

# Repo root for absolute-path lookups
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class TestSchemaHnswIndexComment(unittest.TestCase):
    """Spec step 1: schema.sql must declare the HNSW index with (m=24, ef_construction=200)
    and document the rationale."""

    SCHEMA_PATH = _REPO_ROOT / "server" / "docker" / "unified" / "schema.sql"

    def setUp(self):
        self.assertTrue(
            self.SCHEMA_PATH.exists(),
            f"schema.sql not found at {self.SCHEMA_PATH}",
        )
        self.schema = self.SCHEMA_PATH.read_text(encoding="utf-8")

    def test_schema_has_hnsw_index_comment(self):
        """schema.sql 必须含 HNSW m=24 注释 + (m = 24, ef_construction = 200) 索引声明."""
        # 1. 索引存在 (CREATE INDEX ... USING hnsw ...)
        has_hnsw_create = re.search(
            r"CREATE\s+INDEX.*?USING\s+hnsw", self.schema, re.IGNORECASE | re.DOTALL
        )
        self.assertIsNotNone(
            has_hnsw_create,
            "schema.sql 必须 CREATE INDEX ... USING hnsw",
        )

        # 2. WITH (m = 24, ef_construction = 200) 紧跟 hnsw 行
        hnsw_block_pattern = re.compile(
            r"USING\s+hnsw[^\n]*\n[^\n]*WITH\s*\(\s*m\s*=\s*24\s*,\s*ef_construction\s*=\s*200\s*\)",
            re.IGNORECASE,
        )
        self.assertIsNotNone(
            hnsw_block_pattern.search(self.schema),
            "HNSW index 必须 WITH (m = 24, ef_construction = 200) (spec 要求)",
        )

        # 3. 注释引用 v6.5.0 Phase 1 direction #5
        self.assertIn(
            "V6.5.0 Phase 1 direction #5",
            self.schema,
            "schema.sql 必须有 V6.5.0 Phase 1 direction #5 注释引用",
        )

        # 4. 注释里要解释为啥 24 (而不是 pgvector 默认 16)
        self.assertTrue(
            "m=16" in self.schema or "m 16" in self.schema,
            "schema.sql 注释必须对比 m=24 vs m=16 (决策理由)",
        )


class TestMigrationSetsM24(unittest.TestCase):
    """Spec step 2: migrations/2026_08_23_hnsw_m24.sql 必须 DROP + CREATE 索引
    且 WITH (m = 24, ef_construction = 200)."""

    MIGRATION_PATH = (
        _REPO_ROOT / "server" / "src" / "migrations" / "2026_08_23_hnsw_m24.sql"
    )

    def setUp(self):
        self.assertTrue(
            self.MIGRATION_PATH.exists(),
            f"migration not found at {self.MIGRATION_PATH}",
        )
        self.sql = self.MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_sets_m24(self):
        """migration 必须 DROP + CREATE HNSW index WITH (m = 24, ef_construction = 200)."""
        # 1. 索引 DROP
        self.assertRegex(
            self.sql,
            r"DROP\s+INDEX\s+IF\s+EXISTS\s+memory_chunks_hnsw_idx\s*;",
            "migration 必须先 DROP INDEX memory_chunks_hnsw_idx",
        )

        # 2. 索引 CREATE
        create_match = re.search(
            r"CREATE\s+INDEX.*?USING\s+hnsw", self.sql, re.IGNORECASE | re.DOTALL
        )
        self.assertIsNotNone(
            create_match,
            "migration 必须 CREATE INDEX ... USING hnsw",
        )

        # 3. WITH (m = 24, ef_construction = 200) 选项
        m24_pattern = re.compile(
            r"WITH\s*\(\s*m\s*=\s*24\s*,\s*ef_construction\s*=\s*200\s*\)",
            re.IGNORECASE,
        )
        self.assertIsNotNone(
            m24_pattern.search(self.sql),
            "migration 必须声明 WITH (m = 24, ef_construction = 200)",
        )

        # 4. spec 引用注释 (可读性 + 可追溯)
        self.assertIn(
            "direction #5",
            self.sql,
            "migration 必须含 v6.5.0 direction #5 spec 引用",
        )

        # 5. 幂等性: IF EXISTS / IF NOT EXISTS
        self.assertIn("IF EXISTS", self.sql, "DROP 必须 IF EXISTS")
        self.assertIn("IF NOT EXISTS", self.sql, "CREATE 必须 IF NOT EXISTS")


class TestConfigHnswConstants(unittest.TestCase):
    """Spec step 3 + 4: config.py 必须有 HNSW_M=24 + HNSW_EF_SEARCH=120
    (既作为 module-level 常量, 又作为 CortexConfig 实例属性)。"""

    CONFIG_PATH = _REPO_ROOT / "server" / "src" / "config.py"

    def setUp(self):
        self.assertTrue(
            self.CONFIG_PATH.exists(),
            f"config.py not found at {self.CONFIG_PATH}",
        )
        self.src = self.CONFIG_PATH.read_text(encoding="utf-8")

    def test_config_has_hnsw_constants(self):
        """config.py 必须有 HNSW_M=24 + HNSW_EF_SEARCH=120 常量."""
        # 1. Module-level constants (大写, 等号赋值)
        # Use regex to allow whitespace variations.
        m_pattern = re.compile(r"^HNSW_M\s*:\s*int\s*=\s*24\b", re.MULTILINE)
        ef_search_pattern = re.compile(
            r"^HNSW_EF_SEARCH\s*:\s*int\s*=\s*120\b", re.MULTILINE
        )
        ef_construction_pattern = re.compile(
            r"^HNSW_EF_CONSTRUCTION\s*:\s*int\s*=\s*200\b", re.MULTILINE
        )

        self.assertIsNotNone(
            m_pattern.search(self.src),
            "config.py 必须有模块级常量 HNSW_M: int = 24",
        )
        self.assertIsNotNone(
            ef_search_pattern.search(self.src),
            "config.py 必须有模块级常量 HNSW_EF_SEARCH: int = 120",
        )
        self.assertIsNotNone(
            ef_construction_pattern.search(self.src),
            "config.py 必须有模块级常量 HNSW_EF_CONSTRUCTION: int = 200",
        )

        # 2. 同一文件同时把 HNSW_M/HNSW_EF_SEARCH 暴露给 CortexConfig 实例
        # (便于环境变量覆盖, 例如 HNSW_M=32 测试)。
        self.assertIn("self.hnsw_m", self.src, "CortexConfig 必须有 self.hnsw_m 属性")
        self.assertIn(
            "self.hnsw_ef_search", self.src,
            "CortexConfig 必须有 self.hnsw_ef_search 属性",
        )

        # 3. env override (HNSW_M / HNSW_EF_SEARCH)
        self.assertIn('os.getenv("HNSW_M"', self.src, "env HNSW_M 必须可覆盖")
        self.assertIn(
            'os.getenv("HNSW_EF_SEARCH"', self.src,
            "env HNSW_EF_SEARCH 必须可覆盖",
        )

        # 4. 决策理由注释 (v6.5.0 Phase 1 direction #5)
        self.assertIn(
            "direction #5",
            self.src,
            "config.py 必须有 v6.5.0 Phase 1 direction #5 引用",
        )


class TestApiPgSetsEfSearchAtStartup(unittest.TestCase):
    """Spec step 5: api_pg.py 必须在启动时 SET hnsw.ef_search = 120 (默认 120)。"""

    API_PG_PATH = _REPO_ROOT / "server" / "src" / "api_pg.py"

    def setUp(self):
        self.assertTrue(
            self.API_PG_PATH.exists(),
            f"api_pg.py not found at {self.API_PG_PATH}",
        )
        self.src = self.API_PG_PATH.read_text(encoding="utf-8")

    def test_api_pg_sets_ef_search_at_startup(self):
        """api_pg.py 必须在 lifespan / startup 阶段 SET hnsw.ef_search."""
        # 1. SET hnsw.ef_search = ... (SQL 字符串)
        ef_search_sql = re.search(
            r"SET\s+hnsw\.ef_search\s*=\s*%s", self.src, re.IGNORECASE
        )
        self.assertIsNotNone(
            ef_search_sql,
            "api_pg.py 必须包含 SET hnsw.ef_search = %s SQL",
        )

        # 2. 启动钩子 (lifespan / on_event)
        has_lifespan_hook = bool(
            re.search(r"asynccontextmanager|on_event\(\s*[\"\']startup", self.src)
        )
        self.assertTrue(
            has_lifespan_hook,
            "api_pg.py 必须在 startup / lifespan 阶段调用 SET",
        )

        # 3. 默认值 120 (来自 HNSW_EF_SEARCH config 常量)
        #     既可以硬编码 120, 也可以引用 config.HNSW_EF_SEARCH。
        uses_constant = "HNSW_EF_SEARCH" in self.src
        self.assertTrue(
            uses_constant,
            "api_pg.py 必须引用 HNSW_EF_SEARCH 常量 (避免硬编码)",
        )

        # 4. v6.5.0 direction #5 spec 引用
        self.assertIn(
            "direction #5",
            self.src,
            "api_pg.py 必须有 v6.5.0 direction #5 注释引用",
        )


class TestHnswConstantsRuntime(unittest.TestCase):
    """运行时 import 验证: HNSW_M=24, HNSW_EF_SEARCH=120 在 import 时就生效。"""

    def test_module_level_constants_importable(self):
        """import src.config 后 HNSW_M/HNSW_EF_SEARCH/HNSW_EF_CONSTRUCTION 是 int。"""
        sys.path.insert(0, str(_REPO_ROOT / "server"))

        # CortexConfig() 在 import 时会跑 _load() 并要求 EMBEDDING_MODEL_PATH,
        # 提供一个 placeholder 让 _load 不抛错 (我们只验证常量, 不验证 config 实例)。
        os.environ.setdefault(
            "EMBEDDING_MODEL_PATH",
            "/tmp/fake-model-for-test-only",
        )

        from src.config import HNSW_M, HNSW_EF_SEARCH, HNSW_EF_CONSTRUCTION

        self.assertEqual(HNSW_M, 24)
        self.assertEqual(HNSW_EF_SEARCH, 120)
        self.assertEqual(HNSW_EF_CONSTRUCTION, 200)


if __name__ == "__main__":
    unittest.main()
