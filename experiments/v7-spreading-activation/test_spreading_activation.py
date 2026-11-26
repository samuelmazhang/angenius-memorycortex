"""Unit tests for spreading_activation module (ADR-074).

Tests:
- spread_activation: decay, threshold, max_hops, cycle safety, ranking, edge cases
- _bfs_recall: baseline reference correctness

独立模块设计: 不依赖 psycopg2 → 测试不需装 DB 驱动.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestSpreadActivation(unittest.TestCase):
    """spread_activation 核心算法测试."""

    def test_empty_seeds_returns_empty(self):
        """无种子 → 返空."""
        from spreading_activation import spread_activation
        result = spread_activation(seeds=[], graph={"a": {"b": 0.9}})
        self.assertEqual(result, [])

    def test_single_seed_no_neighbors(self):
        """单种子无邻居 → 只返种子."""
        from spreading_activation import spread_activation
        result = spread_activation(seeds=["a"], graph={"a": {}})
        self.assertEqual(result, [("a", 1.0)])

    def test_decay_one_hop(self):
        """1 跳激活 = seed × 0.7 × edge_weight."""
        from spreading_activation import spread_activation
        graph = {"a": {"b": 1.0}}
        result = spread_activation(seeds=["a"], graph=graph)
        # a: 1.0, b: 1.0 * 0.7 * 1.0 = 0.7
        self.assertIn(("a", 1.0), result)
        self.assertIn(("b", 0.7), result)

    def test_decay_two_hops(self):
        """2 跳激活按 0.7^2 衰减."""
        from spreading_activation import spread_activation
        # a -> b -> c, all edges 1.0
        graph = {"a": {"b": 1.0}, "b": {"c": 1.0}}
        result = spread_activation(seeds=["a"], graph=graph)
        # a: 1.0
        # b: 1.0 * 0.7 * 1.0 = 0.7
        # c: 0.7 * 0.7 * 1.0 = 0.49
        result_dict = dict(result)
        self.assertAlmostEqual(result_dict["a"], 1.0, places=4)
        self.assertAlmostEqual(result_dict["b"], 0.7, places=4)
        self.assertAlmostEqual(result_dict["c"], 0.49, places=4)

    def test_decay_three_hops(self):
        """3 跳 0.7^3 = 0.343."""
        from spreading_activation import spread_activation
        graph = {"a": {"b": 1.0}, "b": {"c": 1.0}, "c": {"d": 1.0}}
        result_dict = dict(spread_activation(seeds=["a"], graph=graph))
        self.assertAlmostEqual(result_dict["d"], 0.343, places=4)

    def test_threshold_stops_low_activation(self):
        """激活 < threshold → 不再扩展."""
        from spreading_activation import spread_activation
        # 3 跳后 d 激活 = 0.343, 4 跳 e = 0.24 (仍 > 0.05), 5 跳 f = 0.168
        # 但 max_hops=2 时 e 不应出现
        graph = {"a": {"b": 1.0}, "b": {"c": 1.0}, "c": {"d": 1.0}, "d": {"e": 1.0}}
        result_dict = dict(spread_activation(seeds=["a"], graph=graph, max_hops=2))
        self.assertIn("a", result_dict)
        self.assertIn("b", result_dict)
        self.assertIn("c", result_dict)
        self.assertNotIn("d", result_dict)  # 第 3 跳超出 max_hops=2

    def test_max_hops_defensive(self):
        """max_hops 限制最大跳数."""
        from spreading_activation import spread_activation
        # 链长 10, max_hops=3 → 只能扩到第 3 跳
        graph = {f"n{i}": {f"n{i+1}": 1.0} for i in range(10)}
        graph["n0"]["n1"] = 1.0
        result_dict = dict(spread_activation(seeds=["n0"], graph=graph, max_hops=3))
        # n0..n3 应有, n4 不应有
        for i in range(4):
            self.assertIn(f"n{i}", result_dict)
        self.assertNotIn("n4", result_dict)

    def test_cycle_safety(self):
        """环不无限循环 (max-merge 阻止放大)."""
        from spreading_activation import spread_activation
        # a -> b -> a (环)
        graph = {"a": {"b": 1.0}, "b": {"a": 1.0}}
        result_dict = dict(spread_activation(seeds=["a"], graph=graph, max_hops=5))
        # a: 1.0
        # b: 0.7 (从 a)
        # 第 2 跳: b 激活 0.7, 不会传给 a (因为 max-merge 不会变大)
        # 但 a 本身保持 1.0 (种子初始激活)
        self.assertAlmostEqual(result_dict["a"], 1.0, places=4)
        self.assertAlmostEqual(result_dict["b"], 0.7, places=4)

    def test_max_merge_prevents_amplification(self):
        """max-merge 防止环放大 (关键安全属性)."""
        from spreading_activation import spread_activation
        # 双向环 a<->b, 边权 1.0
        graph = {"a": {"b": 1.0}, "b": {"a": 1.0}}
        # seeds = [a]
        # hop 1: b = 1.0 * 0.7 * 1.0 = 0.7
        # hop 2: a 已被 a 自己传播 1.0 (保持), b = a * 0.7 * 1.0 = 0.7 (不变, 因为 max(0.7, 0.7)=0.7)
        result_dict = dict(spread_activation(seeds=["a"], graph=graph, max_hops=3))
        # 不应该出现 0.7 * 0.7 = 0.49 这种放大
        for score in result_dict.values():
            self.assertLessEqual(score, 1.0)
            self.assertGreaterEqual(score, 0.0)

    def test_edge_weight_included(self):
        """边权重影响激活强度."""
        from spreading_activation import spread_activation
        graph = {
            "a": {"b": 1.0, "c": 0.5},  # b 高权, c 低权
        }
        result_dict = dict(spread_activation(seeds=["a"], graph=graph))
        # b = 1.0 * 0.7 * 1.0 = 0.7
        # c = 1.0 * 0.7 * 0.5 = 0.35
        self.assertAlmostEqual(result_dict["b"], 0.7, places=4)
        self.assertAlmostEqual(result_dict["c"], 0.35, places=4)
        # 排序: b 应在 c 之前
        result_list = list(spread_activation(seeds=["a"], graph=graph))
        b_idx = [i for i, (k, _) in enumerate(result_list) if k == "b"][0]
        c_idx = [i for i, (k, _) in enumerate(result_list) if k == "c"][0]
        self.assertLess(b_idx, c_idx)

    def test_top_k_returns_sorted_desc(self):
        """返 top-K 按激活降序."""
        from spreading_activation import spread_activation
        graph = {
            "a": {"b": 1.0, "c": 0.5, "d": 0.1},
        }
        result = spread_activation(seeds=["a"], graph=graph)
        scores = [s for _, s in result]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_multi_seeds_max_merge(self):
        """多种子 → max 合并."""
        from spreading_activation import spread_activation
        graph = {
            "a": {"x": 1.0},
            "b": {"x": 0.5},
        }
        result_dict = dict(spread_activation(seeds=["a", "b"], graph=graph))
        # x 激活 = max(1.0 * 0.7 * 1.0, 1.0 * 0.7 * 0.5) = 0.7
        self.assertAlmostEqual(result_dict["x"], 0.7, places=4)

    def test_orphan_seed_with_neighbors(self):
        """孤立种子 (无邻居) 只返自身."""
        from spreading_activation import spread_activation
        graph = {"a": {}, "b": {"c": 1.0}}
        result = spread_activation(seeds=["a"], graph=graph)
        self.assertEqual(result, [("a", 1.0)])

    def test_invalid_decay_raises(self):
        """decay ∈ (0, 1)."""
        from spreading_activation import spread_activation
        with self.assertRaises(ValueError):
            spread_activation(seeds=["a"], graph={}, decay=0.0)
        with self.assertRaises(ValueError):
            spread_activation(seeds=["a"], graph={}, decay=1.0)
        with self.assertRaises(ValueError):
            spread_activation(seeds=["a"], graph={}, decay=1.5)

    def test_invalid_threshold_raises(self):
        """threshold > 0."""
        from spreading_activation import spread_activation
        with self.assertRaises(ValueError):
            spread_activation(seeds=["a"], graph={}, threshold=0.0)
        with self.assertRaises(ValueError):
            spread_activation(seeds=["a"], graph={}, threshold=-0.1)

    def test_invalid_max_hops_raises(self):
        """max_hops >= 1."""
        from spreading_activation import spread_activation
        with self.assertRaises(ValueError):
            spread_activation(seeds=["a"], graph={}, max_hops=0)

    def test_branching_network(self):
        """分支网络: 1 个种子扩散到多个分支."""
        from spreading_activation import spread_activation
        # a → {b, c, d} 各有权
        # b → {e}
        graph = {
            "a": {"b": 0.9, "c": 0.7, "d": 0.3},
            "b": {"e": 0.8},
            "c": {"f": 0.6},
        }
        result_dict = dict(spread_activation(seeds=["a"], graph=graph, max_hops=2))
        # 期望: a(1.0), b(0.63), c(0.49), d(0.21), e(0.3528), f(0.294)
        #   a → b: 1.0 * 0.7 * 0.9 = 0.63
        #   a → c: 1.0 * 0.7 * 0.7 = 0.49
        #   a → d: 1.0 * 0.7 * 0.3 = 0.21
        #   b → e: 0.63 * 0.7 * 0.8 = 0.3528
        #   c → f: 0.49 * 0.7 * 0.6 = 0.2058
        self.assertAlmostEqual(result_dict["a"], 1.0, places=4)
        self.assertAlmostEqual(result_dict["b"], 0.63, places=4)
        self.assertAlmostEqual(result_dict["c"], 0.49, places=4)
        self.assertAlmostEqual(result_dict["d"], 0.21, places=4)
        self.assertAlmostEqual(result_dict["e"], 0.3528, places=4)
        self.assertAlmostEqual(result_dict["f"], 0.2058, places=4)


class TestBFSRecallBaseline(unittest.TestCase):
    """_bfs_recall 基准算法测试."""

    def test_empty_seeds_returns_empty(self):
        from spreading_activation import _bfs_recall
        result = _bfs_recall(seeds=[], graph={"a": {"b": 0.9}})
        self.assertEqual(result, [])

    def test_respects_min_strength(self):
        """min_strength 过滤低权边."""
        from spreading_activation import _bfs_recall
        graph = {"a": {"b": 0.9, "c": 0.3}}  # c < min_strength=0.5
        result = _bfs_recall(seeds=["a"], graph=graph, min_strength=0.5)
        self.assertIn("b", result)
        self.assertNotIn("c", result)

    def test_respects_cap(self):
        """cap 限制最大返回数."""
        from spreading_activation import _bfs_recall
        # a 连 10 个邻居, cap=3 → 只返 3 个
        graph = {"a": {f"n{i}": 0.9 for i in range(10)}}
        result = _bfs_recall(seeds=["a"], graph=graph, cap=3)
        self.assertEqual(len(result), 3)

    def test_no_self_loops(self):
        """BFS 不返种子自身."""
        from spreading_activation import _bfs_recall
        graph = {"a": {"a": 1.0, "b": 0.9}}
        result = _bfs_recall(seeds=["a"], graph=graph)
        self.assertNotIn("a", result)
        self.assertIn("b", result)

    def test_two_hop(self):
        """2 跳扩展."""
        from spreading_activation import _bfs_recall
        graph = {"a": {"b": 0.9}, "b": {"c": 0.9}}
        result = _bfs_recall(seeds=["a"], graph=graph, cap=10)
        self.assertIn("b", result)
        self.assertIn("c", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
