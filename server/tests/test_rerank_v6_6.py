"""Unit tests for V6.6.0 direction #1 — Cross-Encoder 精排层 (server/src/rerank.py).

Mirrors the style of test_recall_v6_5.py / test_taxonomy_boost.py: pure
Python, no live Postgres, no ONNX model download. `score_pairs` takes an
injected session, so a stub scorer exercises all the ordering/fallback
logic without the real cross-encoder.

Covers:
1. Feature flag default-off + explicit enable.
2. fail-open: model unavailable → recall order preserved, no exception.
3. alpha blending: pure CE (alpha=1) / pure RRF (alpha=0) / balanced.
4. top_k truncation: only first N candidates reranked, tail dropped.
5. score/result count mismatch → recall order.
6. ops probe `stats()` shape.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rerank import (  # noqa: E402
    RerankUnavailable,
    is_enabled,
    rerank_ids,
    reset_session,
    stats,
)


def _stub_session(scores):
    """构造一个可注入的 (session, tokenizer, name) 三元组 stub.

    scores: 按候选顺序给定的 logit 列表.
    """

    class _Encoding:
        def __init__(self, n):
            self.ids = list(range(n))
            self.attention_mask = [1] * n
            self.type_ids = [0] * n

    class _Tok:
        """最小 tokenizer stub — score_pairs 只用到 encode_batch.

        签名必须跟 tokenizers.Tokenizer.encode_batch 一致:
        encode_batch(sequence) where sequence 是 (sequence, pair) 二元组列表.
        """

        def encode_batch(self, pairs):
            # 真实签名: encode_batch(input) where each element is (str, str)
            assert isinstance(pairs, list), "必须传 (str, str) 二元组列表"
            for p in pairs:
                assert len(p) == 2, f"每个样本必须是 (sequence, pair): {p!r}"
                assert isinstance(p[0], str) and isinstance(p[1], str), (
                    f"二元组两边都必须是 str, 不能是 list: {p!r}"
                )
            return [_Encoding(4) for _ in pairs]

    class _Sess:
        def __init__(self):
            self.calls = 0

        def get_inputs(self):
            class _I:
                def __init__(self, name):
                    self.name = name

            # RoBERTa 系的 reranker 图会同时要 token_type_ids
            return [_I("input_ids"), _I("attention_mask"), _I("token_type_ids")]

        def run(self, _out, feed):
            for required in ("input_ids", "attention_mask", "token_type_ids"):
                assert required in feed, f"feed 缺少 {required}: {list(feed)}"
            self.calls += 1
            import numpy as np

            return [np.array(scores, dtype=np.float32).reshape(-1, 1)]

    return (_Sess(), _Tok(), "stub")


class TestFeatureFlag(unittest.TestCase):
    def test_default_is_off(self):
        # MEMORY_FEATURE_RERANK 未设置 → 灰度关闭
        #
        # rerank.RERANK_ENABLED is captured at IMPORT time, so removing
        # the variable from os.environ afterwards is not enough -- the
        # module has to be reloaded. The deployed container has had
        # MEMORY_FEATURE_RERANK=1 since 2026-10-21, so the previous
        # version of this test was asserting on the DEPLOYMENT rather
        # than on the default, and had been failing since then.
        import importlib
        import os as _os
        import rerank as _rr
        saved = _os.environ.pop("MEMORY_FEATURE_RERANK", None)
        try:
            importlib.reload(_rr)
            self.assertFalse(_rr.is_enabled())
        finally:
            if saved is not None:
                _os.environ["MEMORY_FEATURE_RERANK"] = saved
            importlib.reload(_rr)  # restore the module state for the rest

    def test_reset_session_is_idempotent(self):
        reset_session()
        reset_session()
        self.assertEqual(stats()["state"], "not_loaded")


class TestFailOpen(unittest.TestCase):
    def test_missing_model_preserves_recall_order(self):
        """模型不可用时必须原样返回粗排顺序, 且不抛错.

        用 MEMORY_RERANK_MODEL_PATH 指向一个不存在的目录来强制
        "unavailable" —— 不能依赖 session=None, 因为一旦开发机上真装了
        模型, get_session() 就会成功, 这个测试会失去意义 (它曾在本地
        装了 bge-reranker-base 之后真的失败了).
        """
        old = os.environ.get("MEMORY_RERANK_MODEL_PATH")
        os.environ["MEMORY_RERANK_MODEL_PATH"] = "/nonexistent/no-reranker-here"
        reset_session()
        try:
            cands = [("a", "alpha"), ("b", "beta"), ("c", "gamma")]
            out = rerank_ids("query", cands, top_k=3, session=None)
            self.assertEqual(out, ["a", "b", "c"])
        finally:
            if old is None:
                os.environ.pop("MEMORY_RERANK_MODEL_PATH", None)
            else:
                os.environ["MEMORY_RERANK_MODEL_PATH"] = old
            reset_session()

    def test_top_k_truncates_before_rerank(self):
        cands = [(f"id{i}", f"text {i}") for i in range(10)]
        # top_k=3 → 只有前 3 条进入精排, 后 7 条被丢弃
        out = rerank_ids("q", cands, top_k=3, session=_stub_session([0.1, 0.2, 0.3]))
        self.assertEqual(len(out), 3)
        self.assertEqual(set(out), {"id0", "id1", "id2"})

    def test_empty_candidates(self):
        self.assertEqual(rerank_ids("q", [], top_k=5), [])

    def test_score_count_mismatch_falls_back(self):
        cands = [("a", "x"), ("b", "y")]
        # 3 个分数对 2 个候选 → 视为异常, 保持粗排顺序
        out = rerank_ids("q", cands, top_k=2, session=_stub_session([0.1, 0.2, 0.3]))
        self.assertEqual(out, ["a", "b"])


class TestAlphaBlending(unittest.TestCase):
    def setUp(self):
        # RRF 让 a 排第一, CE 让 c 排第一 → 两路意见相反
        self.cands = [("a", "aaa"), ("b", "bbb"), ("c", "ccc")]
        self.rrf = {"a": 1.0, "b": 0.5, "c": 0.2}
        # CE 强烈偏好 c: logit +5 → sigmoid≈0.993; a/b 很低
        self.ce = _stub_session([-5.0, -5.0, 5.0])

    def test_pure_ce_puts_c_first(self):
        out = rerank_ids("q", self.cands, top_k=3, blend_rrf=None,
                         session=self.ce, alpha=1.0)
        self.assertEqual(out[0], "c")

    def test_pure_rrf_keeps_recall_order(self):
        out = rerank_ids("q", self.cands, top_k=3, blend_rrf=self.rrf,
                         session=self.ce, alpha=0.0)
        self.assertEqual(out, ["a", "b", "c"])

    def test_balanced_compromise(self):
        out = rerank_ids("q", self.cands, top_k=3, blend_rrf=self.rrf,
                         session=self.ce, alpha=0.5)
        # a: 0.5*0.0067 + 0.5*1.0 ≈ 0.503;  c: 0.5*0.993 + 0.5*0.2 ≈ 0.597
        # → c 反超 a, 但差距收窄; 只断言顺序反转发生了
        self.assertEqual(out[0], "c")

    def test_alpha_zero_ignores_ce(self):
        out = rerank_ids("q", self.cands, top_k=3, blend_rrf=None,
                         session=self.ce, alpha=0.0)
        # 无 rrf → rrf_norm 全 0 → 排序等价原顺序
        self.assertEqual(out, ["a", "b", "c"])


class TestOpsProbe(unittest.TestCase):
    def test_stats_shape(self):
        s = stats()
        for key in ("enabled", "state", "model", "max_query_len", "max_doc_len"):
            self.assertIn(key, s)
        self.assertIn(s["state"], (None, "not_loaded", "ready", "unavailable"))


class TestRerankUnavailableIsCatchable(unittest.TestCase):
    def test_exception_type(self):
        self.assertTrue(issubclass(RerankUnavailable, RuntimeError))


if __name__ == "__main__":
    unittest.main()
