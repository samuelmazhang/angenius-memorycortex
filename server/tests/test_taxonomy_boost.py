"""Unit tests for ADR-067 M8 taxonomy boost (memory_taxonomy module).

Tests:
- get_ancestors: recursive lookup with cycle detection, max depth, DB failure
- boost_factor: 1.2x on ancestor match, 1.0 otherwise, feature flag
- Cache: same tag repeated returns cached value (no extra DB hit)
- reset_cache: cache clearing works

独立模块设计: 不依赖 psycopg2 → 测试不必装真实 DB 驱动.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestGetAncestors(unittest.TestCase):
    """get_ancestors 递归查询 + 防环测试."""

    def setUp(self):
        from memory_taxonomy import reset_cache
        reset_cache()

    def test_no_db_returns_self(self):
        """无 db_lookup: 返回 [tag] (测试场景安全降级)."""
        from memory_taxonomy import get_ancestors
        result = get_ancestors("orphan", db_lookup=None)
        self.assertEqual(result, ["orphan"])

    def test_no_parent_returns_self(self):
        """无父节点: 只返回 [tag]."""
        from memory_taxonomy import get_ancestors
        mock_db = MagicMock(return_value=None)
        result = get_ancestors("orphan", db_lookup=mock_db)
        self.assertEqual(result, ["orphan"])
        mock_db.assert_called_once()

    def test_one_level_parent(self):
        """一层父: 返回 [tag, parent]."""
        from memory_taxonomy import get_ancestors
        mock_db = MagicMock(side_effect=[{"parent_tag": "investment"}, None])
        result = get_ancestors("stock", db_lookup=mock_db)
        self.assertEqual(result, ["stock", "investment"])
        self.assertEqual(mock_db.call_count, 2)

    def test_three_level_chain(self):
        """三层链 stock -> investment -> finance."""
        from memory_taxonomy import get_ancestors
        rows_chain = [
            {"parent_tag": "investment"},
            {"parent_tag": "finance"},
            None,
        ]
        mock_db = MagicMock(side_effect=rows_chain)
        result = get_ancestors("stock", db_lookup=mock_db)
        self.assertEqual(result, ["stock", "investment", "finance"])

    def test_cycle_detection(self):
        """环检测: parent 已在 ancestors → 停止 + warn."""
        from memory_taxonomy import get_ancestors
        rows_chain = [
            {"parent_tag": "investment"},
            {"parent_tag": "stock"},  # 环!
        ]
        mock_db = MagicMock(side_effect=rows_chain)
        with self.assertLogs("memory_taxonomy", level="WARNING") as cm:
            result = get_ancestors("stock", db_lookup=mock_db)
        self.assertEqual(result, ["stock", "investment"])
        self.assertTrue(any("cycle" in m.lower() for m in cm.output))

    def test_db_failure_safe_fallback(self):
        """DB 异常: 返回 [tag] 安全降级."""
        from memory_taxonomy import get_ancestors
        mock_db = MagicMock(side_effect=Exception("connection lost"))
        with self.assertLogs("memory_taxonomy", level="WARNING") as cm:
            result = get_ancestors("stock", db_lookup=mock_db)
        self.assertEqual(result, ["stock"])
        self.assertTrue(any("failed" in m.lower() for m in cm.output))

    def test_cache_hit(self):
        """重复查询同一 tag → cache 命中, DB 只调一次."""
        from memory_taxonomy import get_ancestors
        mock_db = MagicMock(return_value=None)
        get_ancestors("foo", db_lookup=mock_db)
        get_ancestors("foo", db_lookup=mock_db)
        get_ancestors("foo", db_lookup=mock_db)
        self.assertEqual(mock_db.call_count, 1)

    def test_max_depth_10(self):
        """防超深链: 最多 10 层."""
        from memory_taxonomy import get_ancestors
        # 构造 20 层链 (远超 10)
        rows_chain = [{"parent_tag": f"level_{i}"} for i in range(20)] + [None]
        mock_db = MagicMock(side_effect=rows_chain)
        result = get_ancestors("level_0", db_lookup=mock_db)
        # 最多 10 次 query 调用 (start + 9 ancestors)
        self.assertLessEqual(mock_db.call_count, 11)
        # 11 个 tag (start + 10 ancestors)
        self.assertLessEqual(len(result), 11)

    def test_reset_cache(self):
        """reset_cache 后, 缓存清空, DB 重新调用."""
        from memory_taxonomy import get_ancestors, reset_cache
        mock_db = MagicMock(return_value=None)
        get_ancestors("foo", db_lookup=mock_db)
        self.assertEqual(mock_db.call_count, 1)
        reset_cache()
        get_ancestors("foo", db_lookup=mock_db)
        self.assertEqual(mock_db.call_count, 2)


class TestBoostFactor(unittest.TestCase):
    """boost_factor: 1.2x 加权逻辑测试."""

    def setUp(self):
        from memory_taxonomy import reset_cache
        reset_cache()
        # 默认 flag=false, 测试强制开启
        os.environ["MEMORY_FEATURE_TAXONOMY_BOOST"] = "true"
        # reload module 让 env 生效
        import importlib
        import memory_taxonomy
        importlib.reload(memory_taxonomy)

    def tearDown(self):
        os.environ.pop("MEMORY_FEATURE_TAXONOMY_BOOST", None)

    def test_no_chunk_tags_returns_1(self):
        """chunk 没 tags → 不 boost."""
        from memory_taxonomy import boost_factor
        self.assertEqual(boost_factor([], ["finance"]), 1.0)
        self.assertEqual(boost_factor(None, ["finance"]), 1.0)

    def test_no_query_tags_returns_1(self):
        """query 没传 tags → 不 boost."""
        from memory_taxonomy import boost_factor
        self.assertEqual(boost_factor(["stock"], None), 1.0)
        self.assertEqual(boost_factor(["stock"], []), 1.0)

    def test_ancestor_match_returns_1_2(self):
        """chunk tag 的祖先包含 query tag → 1.2."""
        from memory_taxonomy import boost_factor
        mock_db = MagicMock(return_value={"parent_tag": "investment"})
        # chunk tag "stock" → 祖先 [stock, investment]
        # query tag "investment" → 在祖先里 → boost
        self.assertEqual(boost_factor(["stock"], ["investment"], db_lookup=mock_db), 1.2)

    def test_no_match_returns_1(self):
        """chunk tag 的祖先不含 query tag → 1.0."""
        from memory_taxonomy import boost_factor
        mock_db = MagicMock(return_value={"parent_tag": "investment"})
        # chunk tag "stock" → 祖先 [stock, investment]
        # query tag "finance" → 不在祖先里 → 不 boost
        self.assertEqual(boost_factor(["stock"], ["finance"], db_lookup=mock_db), 1.0)

    def test_multiple_chunk_tags_first_match_wins(self):
        """chunk 多个 tag, 第一个命中即返回 1.2."""
        from memory_taxonomy import boost_factor
        # 第一个 tag 不命中 (祖先不含 query), 第二个命中
        mock_db = MagicMock(side_effect=[
            {"parent_tag": "x"},  # 第一个: stock -> x
            {"parent_tag": "finance"},  # 第二个: bond -> finance
            None,
            None,
        ])
        result = boost_factor(["stock", "bond"], ["finance"], db_lookup=mock_db)
        self.assertEqual(result, 1.2)


class TestFeatureFlag(unittest.TestCase):
    """Feature flag 默认 false, opt-in 启用."""

    def test_default_disabled(self):
        """未设置 env → TAXONOMY_BOOST_ENABLED = False."""
        os.environ.pop("MEMORY_FEATURE_TAXONOMY_BOOST", None)
        import importlib
        import memory_taxonomy
        importlib.reload(memory_taxonomy)
        self.assertFalse(memory_taxonomy.TAXONOMY_BOOST_ENABLED)

    def test_explicit_true_enables(self):
        """MEMORY_FEATURE_TAXONOMY_BOOST=true → 启用."""
        os.environ["MEMORY_FEATURE_TAXONOMY_BOOST"] = "true"
        import importlib
        import memory_taxonomy
        importlib.reload(memory_taxonomy)
        self.assertTrue(memory_taxonomy.TAXONOMY_BOOST_ENABLED)
        os.environ.pop("MEMORY_FEATURE_TAXONOMY_BOOST", None)

    def test_one_enables(self):
        """=1 也算 enable."""
        os.environ["MEMORY_FEATURE_TAXONOMY_BOOST"] = "1"
        import importlib
        import memory_taxonomy
        importlib.reload(memory_taxonomy)
        self.assertTrue(memory_taxonomy.TAXONOMY_BOOST_ENABLED)
        os.environ.pop("MEMORY_FEATURE_TAXONOMY_BOOST", None)

    def test_yes_enables(self):
        """=yes 也算 enable."""
        os.environ["MEMORY_FEATURE_TAXONOMY_BOOST"] = "yes"
        import importlib
        import memory_taxonomy
        importlib.reload(memory_taxonomy)
        self.assertTrue(memory_taxonomy.TAXONOMY_BOOST_ENABLED)
        os.environ.pop("MEMORY_FEATURE_TAXONOMY_BOOST", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
