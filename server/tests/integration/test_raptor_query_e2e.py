"""B-layer integration test: scenario 3.

Scenario 3 -- RAPTOR end-to-end query contract.

Cross-module:
  * raptor.build_tree       (Phase 2 #9)
  * raptor.query_with_raptor (Phase 2 #9)

We build a RAPTOR tree over two well-separated clusters (``hot`` vs
``cold``) and verify that a query embedding located inside one cluster
returns leaves from that cluster only -- top-down traversal correctly
descends the right branch of the tree.

The contract being verified is the one that production code depends on:
the tree built by ``build_tree`` and the search performed by
``query_with_raptor`` must agree on the *embedding space*. If the two
modules ever drift (e.g. one normalizes, the other does not), the
top-down walk will descend the wrong branch and this test fails.
"""

from __future__ import annotations

import unittest

from _shared import (  # type: ignore[import-not-found]
    DEFAULT_BEAM,
    DEFAULT_TOP_K,
    _chunk,
    _two_clusters,
    build_tree,
    np,
    query_with_raptor,
)


class TestRaptorQueryEndToEnd(unittest.TestCase):
    """build_tree + query_with_raptor contract: cluster locality."""

    def test_hot_query_returns_hot_leaves(self):
        # 8 chunks in two clusters, dim=4, well-separated (base=+5 vs -5).
        chunks = _two_clusters(n_per=4, dim=4, seed=1729)
        hot_ids = {c["id"] for c in chunks if c["id"].startswith("hot")}
        cold_ids = {c["id"] for c in chunks if c["id"].startswith("cold")}
        self.assertEqual(len(hot_ids), 4)
        self.assertEqual(len(cold_ids), 4)
        # Build tree. No summarize_fn -- default no-op stub is fine.
        tree = build_tree(chunks)
        # Sanity: every chunk becomes a leaf (RAPTOR uses chunks as the
        # bottom layer, then clusters upward). The tree has 8 leaves and
        # 1 root.
        self.assertEqual(len(tree.leaf_ids), 8)
        self.assertEqual(len(tree.root_id) > 0, True)
        # Query at hot-cluster center.
        hot_center = (np.ones(4) * 5.0).tolist()
        results = query_with_raptor(hot_center, tree, top_k=DEFAULT_TOP_K, beam=DEFAULT_BEAM)
        self.assertGreater(len(results), 0)
        # Contract: every returned leaf belongs to the hot cluster.
        returned_ids = [r["chunk_id"] for r in results]
        for cid in returned_ids:
            self.assertIn(
                cid, hot_ids,
                f"hot-query returned a cold leaf: {cid!r}. "
                "RAPTOR top-down traversal likely descended the wrong branch."
            )
        self.assertTrue(
            all(r["score"] >= 0.0 for r in results),
            "scores must be non-negative (cosine over L2-normalized vectors).",
        )

    def test_cold_query_returns_cold_leaves(self):
        # Mirror test: query at cold cluster center.
        chunks = _two_clusters(n_per=4, dim=4, seed=1729)
        cold_ids = {c["id"] for c in chunks if c["id"].startswith("cold")}
        tree = build_tree(chunks)
        cold_center = (np.ones(4) * -5.0).tolist()
        results = query_with_raptor(cold_center, tree, top_k=DEFAULT_TOP_K, beam=DEFAULT_BEAM)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertIn(
                r["chunk_id"], cold_ids,
                f"cold-query returned a hot leaf: {r['chunk_id']!r}.",
            )

    def test_top_k_truncation_contract(self):
        # top_k=2 must return at most 2 leaves, even though the tree has 4.
        chunks = _two_clusters(n_per=4, dim=4, seed=1729)
        tree = build_tree(chunks)
        hot_center = (np.ones(4) * 5.0).tolist()
        results = query_with_raptor(hot_center, tree, top_k=2, beam=DEFAULT_BEAM)
        self.assertLessEqual(len(results), 2)
        # And the top-1 by score must be a hot leaf (we still want locality
        # at top_k=2, which is the strictest bound on the contract).
        if results:
            self.assertIn(results[0]["chunk_id"], {c["id"] for c in chunks if c["id"].startswith("hot")})


if __name__ == "__main__":
    unittest.main()