"""Regression tests for the V6.6.0 rerank padding fix.

Symptom (measured live on 127.0.0.1, 2026-10-30)
--------------------------------------------------
``/search`` with ``rerank=true`` took 1.6-3.7 s while the same search
without rerank took 69-200 ms. The Cross-Encoder was padding every pair to
a fixed 512 tokens, so each pair cost a 512-token forward pass.

Worse, the int8 reranker is *sensitive to how much padding a batch
carries*: the same 37-token document scored

    pad_len= 64 -> -4.7157
    pad_len=128 -> -4.4423
    pad_len=256 -> -4.7628
    pad_len=512 -> -3.8361     (spread 0.93 logits)

and the whole batch was off by up to 1.36 logits against a no-padding
reference — enough to reorder results.

Fix
---
``score_pairs`` now gives each pair exactly its own token count (no padding
columns at all), grouping pairs by exact length so each ONNX call sees a
uniform shape.

These tests use a stub session/tokenizer, so they need neither onnxruntime
nor the model files.
"""
from __future__ import annotations

import os
import sys
import types
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RERANK = os.path.join(SRC_DIR, "rerank.py")


def _load_rerank():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rerank_under_test", RERANK)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover
        raise unittest.SkipTest(f"rerank could not be imported here: {exc}")
    return mod


MOD = _load_rerank()

with open(RERANK, encoding="utf-8") as f:
    SOURCE = f.read()


class _Enc:
    """Mimics a tokenizers.Encoding.

    Real tokenizers pad on the RIGHT with pad_id and set attention_mask to 0
    on the padded positions — only the real tokens have a 1. The production
    code derives each pair's true length from the attention mask, so the
    stub must do the same or it would not exercise the real code path.
    """

    def __init__(self, ids, real_len):
        self.ids = ids
        self.attention_mask = [1] * real_len + [0] * (len(ids) - real_len)
        self.type_ids = [0] * len(ids)


class _StubTokenizer:
    """Encodes each pair into a fixed-length (padded) row, like the real
    tokenizer does, and records the ONNX calls it is asked to make."""

    def __init__(self, pad_len=512):
        self.pad_len = pad_len
        self.calls = []

    def encode_batch(self, pairs):
        rows = []
        for q, d in pairs:
            # deterministic fake tokenisation: 1 token per 4 chars
            n = max(1, (len(q) + len(d)) // 4)
            ids = list(range(1, n + 1)) + [0] * (self.pad_len - n)
            rows.append(_Enc(ids, n))
        return rows


class _StubSession:
    """Records the shapes it was called with and returns a deterministic
    logit derived from the row's first token.

    It MUST return exactly one score per row — the production code raises
    (fail-open, fall back to the coarse order) when the count does not
    match, because a short/long score list would otherwise be applied to
    the wrong candidates.
    """

    def __init__(self):
        self.shapes = []

    def get_inputs(self):
        return [types.SimpleNamespace(name="input_ids"),
                types.SimpleNamespace(name="attention_mask")]

    def run(self, _out, feed):
        ids = feed["input_ids"]
        self.shapes.append(tuple(ids.shape))
        # score depends only on the real content of each row.
        # ONNX Runtime returns a LIST of output tensors and the production
        # code takes [0] (the logits tensor), so the stub must return a
        # list containing the array — not the array itself.
        import numpy as np
        rows = np.array(
            [[float(ids[i][0])] for i in range(ids.shape[0])], dtype=np.float32
        )
        return [rows]


class TestScorePairsPadding(unittest.TestCase):
    def test_no_padding_columns_survive(self):
        """Every ONNX call must receive rows of the group's exact length —
        never the tokenizer's padded width."""
        sess = _StubSession()
        tok = _StubTokenizer(pad_len=512)
        MOD.score_pairs("q", ["short doc", "a much much longer document here"],
                        session=(sess, tok, "stub"))
        self.assertTrue(sess.shapes)
        for shape in sess.shapes:
            self.assertLessEqual(shape[1], 64,
                                 "a call received padded width, not real length")

    def test_rows_are_grouped_by_exact_length(self):
        sess = _StubSession()
        tok = _StubTokenizer(pad_len=512)
        # two identical-length docs + one different -> 2 groups
        MOD.score_pairs("q", ["abcd", "efgh", "a much longer document"],
                        session=(sess, tok, "stub"))
        self.assertEqual(len(sess.shapes), 2,
                         "rows must be grouped by exact token length")

    def test_scores_match_per_row_scoring(self):
        """A row's score must not depend on which other rows share its batch."""
        sess_a = _StubSession()
        tok_a = _StubTokenizer(pad_len=512)
        docs = ["abcd", "a much longer document text"]
        together = MOD.score_pairs("q", docs, session=(sess_a, tok_a, "stub"))

        alone = []
        for d in docs:
            s = _StubSession()
            t = _StubTokenizer(pad_len=512)
            alone.extend(MOD.score_pairs("q", [d], session=(s, t, "stub")))
        self.assertEqual(together, alone)

    def test_empty_input(self):
        self.assertEqual(MOD.score_pairs("q", []), [])

    def test_result_length_matches_input(self):
        sess = _StubSession()
        tok = _StubTokenizer(pad_len=512)
        docs = ["a", "bb", "ccc", "dddd"]
        out = MOD.score_pairs("q", docs, session=(sess, tok, "stub"))
        self.assertEqual(len(out), len(docs))


class TestScorePairsSource(unittest.TestCase):
    def test_groups_by_exact_length(self):
        self.assertIn("by_len", SOURCE)
        self.assertIn("sum(enc.attention_mask)", SOURCE,
                      "the real token length must come from the attention mask")

    def test_no_longer_pads_to_max_doc_len_in_the_call(self):
        seg = SOURCE[SOURCE.index("def score_pairs("):]
        seg = seg[: seg.index("def rerank_scores(")]
        self.assertNotIn(
            "enable_padding", seg,
            "score_pairs must not re-enable fixed-length padding")

    def test_docstring_records_the_measurement(self):
        seg = SOURCE[SOURCE.index("def score_pairs("):]
        seg = seg[: seg.index("def rerank_scores(")]
        self.assertIn("pad_len", seg)
        self.assertIn("no padding", seg.lower())


if __name__ == "__main__":
    unittest.main()
