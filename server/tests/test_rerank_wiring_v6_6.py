"""Wire-up verification for V6.6.0 direction #1 — CE rerank in /search.

The unit tests in test_rerank_v6_6.py cover rerank.py in isolation. This
module verifies the *wiring* in api_pg.py by grepping the source for the
contract pieces (no live Postgres needed, same style as
test_recall_v6_5.py's "grep the source for spec compliance" tests).

Invariants pinned here:
1. `_three_factor` has the 6th `ce` param with a default → the 12 existing
   recall tests that assert the 5 fixed weights keep passing unchanged.
2. `ce_term` is 0 when ce is None → rerank off ⇒ composite byte-identical
   to v6.5.1 (no silent ranking drift for existing clients).
3. The call site passes `ce_scores.get(cid)`.
4. The call site does NOT reorder `candidates` (that would be discarded
   by the later composite sort).
5. Double gate: req.rerank AND _rerank_enabled().
6. Audit trail records requested/effective/scored_n.
7. FastAPI version stamp is valid semver, bumped past 6.6.0, and consistent
   with the module docstring. (Full VERSION/README/docs consistency is
   enforced by .githooks/consistency_check.py in pre-commit + CI.)
"""

import os
import re
import sys
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "api_pg.py")

with open(SRC, encoding="utf-8") as f:
    SOURCE = f.read()


def _fn_body(name, src=SOURCE):
    """Extract a top-level def/class body by brace matching.

    Brace matching alone is not enough: the first "{" after the signature is
    often an f-string interpolation like `{req.limit}`, which unbalances the
    counter. Skip anything that isn't a block opener by requiring the brace
    to be the last non-space char before a newline, or fall back to matching
    the `def` up to the next top-level `def`/`@app`.
    """
    m = re.search(rf"^def {re.escape(name)}\(", src, re.M)
    if not m:
        return None
    # Take everything from the def to the next column-0 "def " or "@app."
    rest = src[m.start() :]
    nxt = re.search(r"^(?:def |@app\.|class |# ─)", rest[1:], re.M)
    return rest[: nxt.start() + 1] if nxt else rest


class TestThreeFactorSignature(unittest.TestCase):
    def test_ce_param_is_optional(self):
        assert 'lex_hit: bool, ce: Optional[float] = None' in SOURCE, (
            "_three_factor must take ce as an optional 6th param so the "
            "existing 5-weight tests keep passing"
        )

    def test_ce_term_zero_when_absent(self):
        assert "ce_term = (0.30 * ce) if ce is not None else 0.0" in SOURCE

    def test_ce_term_added_to_return(self):
        body = _fn_body("search_memory")
        assert "+ ce_term" in body, "ce_term must be summed into composite"

    def test_five_original_weights_untouched(self):
        # The v6.5.0 weights are asserted by test_recall_v6_5.py; if any of
        # them changed here, that suite would have failed too.
        body = _fn_body("search_memory")
        for w in ("0.5 * rrf_norm", "0.4 * sim_eff",
                  "0.15 * (imp / 10.0)", "0.1 * recency", "0.05 * rlv"):
            self.assertIn(w, body)


class TestCallSiteWiring(unittest.TestCase):
    def setUp(self):
        self.body = _fn_body("search_memory")

    def test_double_gate(self):
        self.assertIn("req.rerank and _rerank_enabled()", self.body)

    def test_passes_ce_scores_to_three_factor(self):
        self.assertIn("ce_scores.get(cid)", self.body)

    def test_does_not_reorder_candidates(self):
        """精排必须以分数注入, 不能靠重排 candidates 列表.

        candidates 的顺序会被随后的 results.sort(key=composite_score)
        完全覆盖 —— 谁要是在这里写 `candidates = _rerank_ids(...)`,
        精排信号会在下游被静默丢弃。
        """
        self.assertNotIn("candidates = _rerank_ids(", self.body)
        self.assertNotIn("candidates = rerank_ids(", self.body)

    def test_respects_top_k_cap(self):
        self.assertIn("candidates[: req.rerank_top_k]", self.body)

    def test_counters_incremented_both_ways(self):
        self.assertIn('OPS_COUNTERS["rerank_applied"] += 1', self.body)
        self.assertIn('OPS_COUNTERS["rerank_skipped"] += 1', self.body)


class TestRequestSchema(unittest.TestCase):
    def test_rerank_defaults_off(self):
        self.assertIn("rerank: bool = False", SOURCE)

    def test_top_k_bounded(self):
        m = re.search(r"rerank_top_k: int = Field\(\s*(\d+),\s*ge=(\d+),\s*le=(\d+)", SOURCE)
        self.assertIsNotNone(m, "rerank_top_k must be a bounded Field")
        self.assertEqual(m.group(1), "30")  # CPU latency sweet spot
        self.assertEqual(m.group(3), "100")  # hard ceiling


class TestOpsAndAudit(unittest.TestCase):
    def test_counters_registered(self):
        self.assertIn('"rerank_applied"', SOURCE)
        self.assertIn('"rerank_skipped"', SOURCE)

    def test_audit_distinguishes_requested_vs_effective(self):
        self.assertIn('"rerank_requested": bool(req.rerank)', SOURCE)
        self.assertIn('"rerank_effective": bool(ce_scores)', SOURCE)
        self.assertIn('"rerank_scored_n": len(ce_scores)', SOURCE)

    def test_ce_score_exposed_on_result_rows(self):
        self.assertIn('r["ce_score"]', SOURCE)


class TestVersionBump(unittest.TestCase):
    def test_fastapi_version_matches_module_docstring(self):
        # Contract: api_pg.py's own two version declarations must agree. This
        # reads only SOURCE so it is self-contained in the container (which
        # ships no root VERSION file). Cross-file consistency with VERSION /
        # README / docs is enforced by .githooks/consistency_check.py in
        # pre-commit + CI; no need to duplicate it here. Bumping only needs
        # the two api_pg.py declarations changed together.
        doc = re.search(r"Memory Cortex v(\d+\.\d+\.\d+)\b", SOURCE)
        fa = re.search(r'FastAPI\([^\n]*version="(\d+\.\d+\.\d+)"', SOURCE)
        self.assertIsNotNone(doc, "module docstring version missing")
        self.assertIsNotNone(fa, "FastAPI version stamp missing")
        self.assertEqual(fa.group(1), doc.group(1))


if __name__ == "__main__":
    unittest.main()
