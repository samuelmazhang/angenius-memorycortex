"""Unit tests for v6.5.0 direction #2 — Recall formula fix.

Covers 4 changes in server/src/api_pg.py:
1. composite_score weights (0.5 / 0.4 / 0.15 / 0.10 / 0.05)
2. _relevance_scores() cold-start pre-warming (0.3 base + 0.05*imp + 0.2[note] + count/10)
3. MemorySearchRequest.min_importance Pydantic Field(0, ge=0, le=10)
4. _expand_graph_neighbors_filtered(..., min_strength=0.3, cap=10, lex_hit_ids=...)

Tests are designed to NOT need a live Postgres. For DB-backed logic
(_relevance_scores SQL, _expand_graph_neighbors_filtered SQL), we either
replicate the Python-side formula or grep the source for spec compliance.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestCompositeScoreWeights(unittest.TestCase):
    """验证 composite_score 公式权重 = 0.5/0.4/0.15/0.10/0.05."""

    def _build_three_factor(self):
        def _three_factor(vsim, imp, age_days, rlv, rrf_norm, lex_hit=False):
            recency = 1.0 / (1.0 + 0.05 * age_days)
            sim_eff = max(vsim, 0.5) if lex_hit else vsim
            return (0.5 * rrf_norm + 0.4 * sim_eff
                    + 0.15 * (imp / 10.0) + 0.1 * recency + 0.05 * rlv)
        return _three_factor

    def test_weights_match_v6_5_spec(self):
        tf = self._build_three_factor()
        score = tf(vsim=1.0, imp=10, age_days=0, rlv=1.0, rrf_norm=1.0)
        self.assertAlmostEqual(score, 1.20, places=4)

    def test_imp_weight_increased_to_15pct(self):
        tf = self._build_three_factor()
        s_high = tf(vsim=0.5, imp=10, age_days=10, rlv=0.5, rrf_norm=0.5)
        s_low = tf(vsim=0.5, imp=0, age_days=10, rlv=0.5, rrf_norm=0.5)
        diff = s_high - s_low
        self.assertAlmostEqual(diff, 0.15, places=4)

    def test_rrf_weight_decreased_to_50pct(self):
        tf = self._build_three_factor()
        s_high = tf(vsim=0.5, imp=5, age_days=10, rlv=0.5, rrf_norm=1.0)
        s_low = tf(vsim=0.5, imp=5, age_days=10, rlv=0.5, rrf_norm=0.0)
        diff = s_high - s_low
        self.assertAlmostEqual(diff, 0.5, places=4)

    def test_rlv_weight_decreased_to_5pct(self):
        tf = self._build_three_factor()
        s_high = tf(vsim=0.5, imp=5, age_days=10, rlv=1.0, rrf_norm=0.5)
        s_low = tf(vsim=0.5, imp=5, age_days=10, rlv=0.0, rrf_norm=0.5)
        diff = s_high - s_low
        self.assertAlmostEqual(diff, 0.05, places=4)


class TestRecallFormulaImpWeightIncreased(unittest.TestCase):
    def test_imp_5_vs_10_difference_is_3x_larger(self):
        def v65(imp):
            return 0.15 * (imp / 10.0)
        def v63(imp):
            return 0.05 * (imp / 10.0)
        diff_65 = v65(10) - v65(5)
        diff_63 = v63(10) - v63(5)
        self.assertAlmostEqual(diff_65, 0.075, places=4)
        self.assertAlmostEqual(diff_63, 0.025, places=4)
        self.assertAlmostEqual(diff_65 / diff_63, 3.0, places=4)


class TestMinImportanceValidation(unittest.TestCase):
    def _get_request_class(self):
        from pydantic import BaseModel, Field
        from typing import Optional, List

        class MemorySearchRequest(BaseModel):
            query: str
            limit: int = 10
            type: Optional[str] = None
            min_importance: int = Field(0, ge=0, le=10)
            tags: Optional[List[str]] = None
        return MemorySearchRequest

    def test_default_zero_is_valid(self):
        Cls = self._get_request_class()
        req = Cls(query="test")
        self.assertEqual(req.min_importance, 0)

    def test_explicit_zero_is_valid(self):
        Cls = self._get_request_class()
        req = Cls(query="test", min_importance=0)
        self.assertEqual(req.min_importance, 0)

    def test_valid_in_range(self):
        Cls = self._get_request_class()
        for v in [1, 5, 7, 10]:
            req = Cls(query="test", min_importance=v)
            self.assertEqual(req.min_importance, v)

    def test_negative_one_rejected(self):
        from pydantic import ValidationError
        Cls = self._get_request_class()
        with self.assertRaises(ValidationError):
            Cls(query="test", min_importance=-1)

    def test_eleven_rejected(self):
        from pydantic import ValidationError
        Cls = self._get_request_class()
        with self.assertRaises(ValidationError):
            Cls(query="test", min_importance=11)

    def test_twenty_rejected(self):
        from pydantic import ValidationError
        Cls = self._get_request_class()
        with self.assertRaises(ValidationError):
            Cls(query="test", min_importance=20)


class TestColdStartRelevanceScore(unittest.TestCase):
    def _extract_relevance_logic(self):
        def compute(meta_by_id, cnt_by_id, ids):
            out = {}
            for mid, cnt in cnt_by_id.items():
                meta = meta_by_id.get(mid) or {}
                imp = float(meta.get("importance") or 5)
                rlv = (
                    0.3
                    + 0.05 * imp
                    + (0.2 if meta.get("type") == "note" else 0.0)
                    + min(cnt, 10) / 10.0
                )
                out[mid] = min(1.0, rlv)
            for mid in ids:
                if mid in out:
                    continue
                meta = meta_by_id.get(mid) or {}
                imp = float(meta.get("importance") or 5)
                rlv = (
                    0.3
                    + 0.05 * imp
                    + (0.2 if meta.get("type") == "note" else 0.0)
                )
                out[mid] = min(1.0, rlv)
            return out
        return compute

    def test_new_chunk_not_zero(self):
        compute = self._extract_relevance_logic()
        meta = {"c1": {"importance": 5, "type": "fact"}}
        out = compute(meta, {}, ["c1"])
        self.assertAlmostEqual(out["c1"], 0.55, places=4)
        self.assertGreater(out["c1"], 0.0)

    def test_note_type_bonus(self):
        compute = self._extract_relevance_logic()
        meta = {"c1": {"importance": 5, "type": "note"}}
        out = compute(meta, {}, ["c1"])
        self.assertAlmostEqual(out["c1"], 0.75, places=4)

    def test_high_importance_boost(self):
        compute = self._extract_relevance_logic()
        meta = {"c1": {"importance": 10, "type": "fact"}}
        out = compute(meta, {}, ["c1"])
        self.assertAlmostEqual(out["c1"], 0.8, places=4)

    def test_count_increases_relevance(self):
        compute = self._extract_relevance_logic()
        meta = {"c1": {"importance": 5, "type": "fact"}}
        out = compute(meta, {"c1": 3}, ["c1"])
        self.assertAlmostEqual(out["c1"], 0.85, places=4)

    def test_capped_at_one(self):
        compute = self._extract_relevance_logic()
        meta = {"c1": {"importance": 10, "type": "note"}}
        out = compute(meta, {"c1": 100}, ["c1"])
        self.assertEqual(out["c1"], 1.0)


class TestIncludeRelatedMinStrengthLower(unittest.TestCase):
    def test_weak_neighbor_passes_with_lower_threshold(self):
        MIN_STRENGTH_V65 = 0.3
        MIN_STRENGTH_V63 = 0.5
        sim = 0.35
        self.assertGreaterEqual(sim, MIN_STRENGTH_V65)
        self.assertLess(sim, MIN_STRENGTH_V63)

    def test_lex_hit_neighbor_always_kept(self):
        lex_hit_ids = {"lex_chunk_a"}
        score = 0.05
        neighbor_id = "lex_chunk_a"
        keep = (score >= 0.3) or (neighbor_id in lex_hit_ids)
        self.assertTrue(keep)


class TestSourceFormulaIntegrity(unittest.TestCase):
    SRC_PATH = os.path.join(
        os.path.dirname(__file__), "..", "src", "api_pg.py"
    )

    def test_composite_weights_in_source(self):
        with open(self.SRC_PATH, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("0.5 * rrf_norm + 0.4 * sim_eff", src)
        self.assertIn("0.15 * (imp / 10.0)", src)
        self.assertIn("0.1 * recency + 0.05 * rlv", src)
        self.assertNotIn("0.6 * rrf_norm + 0.4 * sim_eff", src)
        self.assertNotIn("0.05 * (imp / 10.0) + 0.1 * recency + 0.1 * rlv", src)

    def test_min_importance_field_in_source(self):
        with open(self.SRC_PATH, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("min_importance: int = Field(0, ge=0, le=10)", src)

    def test_min_importance_check_uses_gt_zero(self):
        with open(self.SRC_PATH, encoding="utf-8") as f:
            src = f.read()
        self.assertGreaterEqual(src.count("if req.min_importance > 0:"), 2)

    def test_relevance_scores_has_cold_start(self):
        with open(self.SRC_PATH, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("0.05 * imp", src)
        self.assertIn('"note"', src)
        self.assertGreaterEqual(src.count("0.3"), 2)

    def test_include_related_min_strength_0_3(self):
        with open(self.SRC_PATH, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("min_strength=0.3, cap=10", src)
        self.assertIn("lex_hit_ids=lexical_set", src)

    def test_second_pass_importance_filter_in_source(self):
        """There must be a second-pass importance filter on the neighbours
        that graph expansion added.

        V6.6.0 (2026-10-31): the threshold was the hardcoded literal 6.
        It is now the request parameter `graph_neighbor_min_importance`
        (default 6), because importance is not a uniform property of a
        corpus -- 42% of the production chunks that HAVE relations sit at
        importance 5 and were therefore never expandable, and on the
        LOCOMO corpus (a flat import at importance 5) 96% of them were
        blocked. The assertion follows the INTENT, not the literal.
        """
        with open(self.SRC_PATH, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("added_neighbors", src)
        self.assertIn('int(r.get("importance") or 0) < req.graph_neighbor_min_importance',
                      src)
        self.assertIn("graph_neighbor_min_importance", src)


if __name__ == "__main__":
    unittest.main()
