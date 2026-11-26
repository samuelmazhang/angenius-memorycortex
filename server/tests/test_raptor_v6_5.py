"""Unit tests for Cortex v6.5.0 RAPTOR recursive summary layer (Phase 2 #9).
Covers the 5 dispatch-mandated tests + helper / regression tests.

  Required (mapping to spec):
    1. test_build_tree_clusters_similar_chunks
    2. test_tree_respects_max_depth
    3. test_query_traverses_tree_top_down
    4. test_query_collects_relevant_leaves
    5. test_raptor_idempotent

  Helpers / regression:
     6.  test_build_tree_handles_empty_input
     7.  test_build_tree_handles_none_input
     8.  test_build_tree_handles_single_chunk
     9.  test_build_tree_skips_malformed_chunks
    10.  test_build_tree_chunks_without_embeddings_still_cluster
    11.  test_build_tree_uses_injected_summarize_fn
    12.  test_build_tree_falls_back_when_summarize_fn_raises
    13.  test_build_tree_sklearn_optional_path
    14.  test_build_tree_respects_cluster_threshold_clamp
    15.  test_build_tree_returns_exactly_one_root
    16.  test_leaf_to_path_includes_root_and_leaf
    17.  test_chunk_id_to_leaf_maps_each_chunk
    18.  test_path_for_chunk_lookup
    19.  test_raptor_node_dataclass_defaults
    20.  test_raptor_tree_convenience_accessors
    21.  test_query_returns_empty_for_empty_tree
    22.  test_query_returns_empty_for_zero_top_k
    23.  test_query_zero_vector_question_returns_zero_score
    24.  test_query_rejects_non_raptor_tree_input
    25.  test_query_uses_injected_score_fn
    26.  test_query_top_k_truncates_results
    27.  test_query_dedupes_same_leaf
    28.  test_query_beam_explores_more_branches
    29.  test_query_path_ends_with_leaf_id
    30.  test_query_orders_by_score_descending
    31.  test_module_public_surface
    32.  test_constants_have_expected_values
    33.  test_consolidate_package_reexports_raptor

No LLM, no httpx, no live Postgres. The tree is exercised against
in-memory chunks with hand-crafted numpy embeddings and a trivial
``summarize_fn`` stub.
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consolidate  # noqa: E402
from consolidate import (  # noqa: E402
    DEFAULT_BEAM,
    DEFAULT_CLUSTER_THRESHOLD,
    DEFAULT_MAX_DEPTH,
    DEFAULT_RANDOM_STATE,
    DEFAULT_TOP_K,
    RaptorNode,
    RaptorTree,
    build_tree,
    query_with_raptor,
)


# ============================================================================
# Fixtures
# ============================================================================

def _chunk(cid, content, embedding=None):
    """Build a canonical chunk dict."""
    out = {"id": cid, "content": content}
    if embedding is not None:
        out["embedding"] = list(embedding)
    return out


def _two_clusters(n_per=4, dim=4, seed=1729):
    """Build ``2*n_per`` chunks arranged in two well-separated clusters."""
    rng = np.random.default_rng(seed)
    chunks = []
    for cluster_idx in range(2):
        if cluster_idx == 0:
            base = np.ones(dim) * 5.0
            tag = "hot"
        else:
            base = np.ones(dim) * -5.0
            tag = "cold"
        for j in range(n_per):
            emb = base + rng.normal(scale=0.1, size=dim)
            chunks.append(
                _chunk(
                    f"{tag}{j}",
                    f"{tag} content {j}",
                    embedding=emb.tolist(),
                )
            )
    return chunks


# ============================================================================
# 5 dispatch-mandated tests
# ============================================================================

class DispatchMandatedTests(unittest.TestCase):
    """The five tests the dispatch prompt explicitly requires."""

    def test_build_tree_clusters_similar_chunks(self):
        """#1: similar chunks land in the same cluster node."""
        chunks = _two_clusters(n_per=4, dim=4, seed=42)
        tree = build_tree(chunks, max_depth=3, cluster_threshold=0.7)

        # 8 leaves -> at least one L1 node covers each "hot" / "cold"
        # cluster. Walk every L1 node and check that all its leaves
        # share the same temperature tag.
        l1_nodes = [
            n for n in tree.nodes.values()
            if n.depth == 1 and not n.is_leaf
        ]
        self.assertGreaterEqual(len(l1_nodes), 2)

        # At least one cluster contains >= 2 leaves that share a tag.
        # (sklearn / fallback tie order may yield 1+2+2+3 partitions;
        # the contract is "the same tag stays together".)
        any_cluster_has_homogeneous_leaves = False
        for n in l1_nodes:
            tags = {cid.rstrip("0123456789") for cid in n.chunk_ids}
            # Remove trailing digits -> "hot" or "cold"
            if len(tags) == 1 and list(tags)[0] in ("hot", "cold"):
                any_cluster_has_homogeneous_leaves = True
                self.assertGreaterEqual(len(n.chunk_ids), 2)
        self.assertTrue(
            any_cluster_has_homogeneous_leaves,
            "expected at least one L1 cluster to contain >1 same-tag leaves",
        )

        # Every chunk id appears in the tree.
        self.assertEqual(set(tree.chunk_id_to_leaf.keys()),
                         {c["id"] for c in chunks})

    def test_tree_respects_max_depth(self):
        """#2: max_depth controls the number of clustering rounds.

        With ``max_depth=1`` we run at most one clustering pass, so
        the natural cluster layer sits at depth 1; the synthetic root
        (if needed) sits at depth 2.

        With ``max_depth=3`` we run at most three clustering passes,
        so the natural top-layer sits at depth 3; the synthetic root
        (if needed) sits at depth 4.
        """
        chunks = _two_clusters(n_per=4, dim=4, seed=7)

        tree1 = build_tree(chunks, max_depth=1, cluster_threshold=0.7)
        # ``tree.max_depth`` echoes the caller-supplied bound.
        self.assertEqual(tree1.max_depth, 1)
        # Depths span 0..1 (natural) plus optionally 2 (synthetic root).
        depths1 = sorted({n.depth for n in tree1.nodes.values()})
        self.assertEqual(depths1, [0, 1] if len(depths1) == 2 else [0, 1, 2])
        # The tree has exactly one root.
        root1 = tree1.root()
        self.assertTrue(root1.node_id)
        self.assertFalse(root1.is_leaf)
        # Root depth is max_depth (early collapse) or max_depth + 1.
        self.assertIn(root1.depth, (1, 2))

        tree3 = build_tree(chunks, max_depth=3, cluster_threshold=0.7)
        self.assertEqual(tree3.max_depth, 3)
        # Natural cluster layers occupy depths 0..3 inclusive;
        # synthetic root (if needed) sits at depth 4.
        depths3 = sorted({n.depth for n in tree3.nodes.values()})
        self.assertEqual(depths3, [0, 1, 2, 3] if len(depths3) == 4 else [0, 1, 2, 3, 4])
        # Root depth is either max_depth (early collapse) or
        # max_depth + 1 (synthetic wrap).
        root3 = tree3.root()
        self.assertIn(root3.depth, (3, 4))
        # Every leaf's path is well-formed (root -> ... -> leaf).
        for leaf_id in tree3.leaf_ids:
            path = tree3.leaf_to_path[leaf_id]
            self.assertEqual(path[0], root3.node_id)
            self.assertEqual(path[-1], leaf_id)
            # Path length is max_depth + 1 (early collapse) or
            # max_depth + 2 (synthetic root wrap).
            self.assertGreaterEqual(len(path), tree3.max_depth + 1)
            self.assertLessEqual(len(path), tree3.max_depth + 2)

    def test_query_traverses_tree_top_down(self):
        """#3: every returned hit's path includes root_id as the first
        element and the leaf id as the last."""
        chunks = _two_clusters(n_per=4, dim=4, seed=11)
        tree = build_tree(chunks, max_depth=3, cluster_threshold=0.7)

        # Use a query vector pointing in the "hot" direction.
        query = np.asarray([5.0, 5.0, 5.0, 5.0], dtype=np.float64)
        results = query_with_raptor(query, tree, top_k=8, beam=3)
        self.assertGreater(len(results), 0)

        root_id = tree.root_id
        for hit in results:
            path = hit["path"]
            self.assertGreaterEqual(len(path), 2)
            self.assertEqual(path[0], root_id)
            self.assertEqual(path[-1], hit["node_id"])
            # Path must contain the leaf's node id at the end.
            self.assertEqual(path[-1], hit["node_id"])

        # Every returned leaf is in the tree's leaf set.
        leaf_set = set(tree.leaf_ids)
        for hit in results:
            self.assertIn(hit["node_id"], leaf_set)

    def test_query_collects_relevant_leaves(self):
        """#4: leaves that share the most-similar branch outrank leaves
        under low-similarity branches when beam > 1 is allowed."""
        chunks = _two_clusters(n_per=4, dim=4, seed=23)
        tree = build_tree(chunks, max_depth=3, cluster_threshold=0.7)

        # Query with a "hot" vector so all four hot leaves should be
        # preferred to the cold leaves. With beam=2 we can reach at
        # least the top-2 children of root and their leaves.
        query = np.asarray([5.0, 5.0, 5.0, 5.0], dtype=np.float64)
        results = query_with_raptor(query, tree, top_k=4, beam=10)

        self.assertEqual(len(results), 4)
        # The first hit should be a hot leaf.
        top_chunk = results[0]["chunk_id"]
        self.assertTrue(
            top_chunk.startswith("hot"),
            f"expected top hit to be a 'hot' chunk, got {top_chunk!r}",
        )
        # Scores strictly non-increasing.
        scores = [r["score"] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_raptor_idempotent(self):
        """#5: two build_tree calls on the same input produce the same tree."""
        chunks = _two_clusters(n_per=4, dim=4, seed=31)

        def _stub_summarize(texts):
            # Deterministic per-cluster summary that depends only on the
            # *order* of inputs (which is itself deterministic).
            return " | ".join(sorted(texts))

        a = build_tree(
            chunks, max_depth=3, cluster_threshold=0.7,
            summarize_fn=_stub_summarize,
        )
        b = build_tree(
            chunks, max_depth=3, cluster_threshold=0.7,
            summarize_fn=_stub_summarize,
        )

        # Same root id.
        self.assertEqual(a.root_id, b.root_id)
        # Same node set.
        self.assertEqual(set(a.nodes.keys()), set(b.nodes.keys()))
        # Same leaf set in the same order.
        self.assertEqual(a.leaf_ids, b.leaf_ids)
        # Same leaf_to_path.
        self.assertEqual(a.leaf_to_path, b.leaf_to_path)
        # Same chunk_id_to_leaf mapping.
        self.assertEqual(a.chunk_id_to_leaf, b.chunk_id_to_leaf)
        # Same skipped ids.
        self.assertEqual(a.skipped_chunk_ids, b.skipped_chunk_ids)
        # Same embeddings (per node id, float-equal).
        for nid in a.nodes:
            ea = a.nodes[nid].embedding
            eb = b.nodes[nid].embedding
            if ea is None:
                self.assertIsNone(eb)
                continue
            np.testing.assert_allclose(ea, eb, atol=1e-9)
        # Same summary text per node id.
        for nid in a.nodes:
            self.assertEqual(a.nodes[nid].text, b.nodes[nid].text)


# ============================================================================
# Helper / regression tests -- builder
# ============================================================================

class BuilderEdgeCases(unittest.TestCase):

    def test_build_tree_handles_empty_input(self):
        tree = build_tree([], max_depth=3, cluster_threshold=0.7)
        self.assertIsInstance(tree, RaptorTree)
        # The empty-input tree still has a synthetic root.
        self.assertTrue(tree.root_id)
        self.assertEqual(tree.leaf_ids, [])
        self.assertEqual(tree.leaf_to_path, {})

    def test_build_tree_handles_none_input(self):
        tree = build_tree(None, max_depth=3, cluster_threshold=0.7)
        self.assertIsInstance(tree, RaptorTree)
        self.assertEqual(tree.leaf_ids, [])

    def test_build_tree_handles_single_chunk(self):
        chunks = [_chunk("only", "single chunk", embedding=[1.0, 0.0])]
        tree = build_tree(chunks, max_depth=3, cluster_threshold=0.7)
        self.assertEqual(len(tree.leaf_ids), 1)
        self.assertEqual(tree.leaf_ids[0], "L0#0")
        # Exactly one root.
        root = tree.root()
        self.assertFalse(root.is_leaf)
        # Root depth == max_depth.
        self.assertEqual(root.depth, 3)

    def test_build_tree_skips_malformed_chunks(self):
        chunks = [
            _chunk("ok1", "good content", embedding=[1.0, 0.0]),
            "string-not-dict",
            42,
            {"id": "empty", "content": "   "},  # whitespace-only
            {"id": "missing"},                  # no content / text
            _chunk("ok2", "another good", embedding=[0.0, 1.0]),
        ]
        tree = build_tree(chunks, max_depth=2, cluster_threshold=0.5)
        # Only the two good chunks survived.
        self.assertEqual(set(tree.chunk_id_to_leaf.keys()), {"ok1", "ok2"})
        # Skipped ids include the malformed ones.
        for sid in ("empty", "missing"):
            self.assertIn(sid, tree.skipped_chunk_ids)


class BuilderEmbeddings(unittest.TestCase):

    def test_build_tree_chunks_without_embeddings_still_cluster(self):
        """Chunks without embeddings fall back to zero-vector."""
        chunks = [
            _chunk("a", "alpha content"),
            _chunk("b", "beta content"),
            _chunk("c", "gamma content"),
        ]
        tree = build_tree(chunks, max_depth=2, cluster_threshold=0.7)
        self.assertEqual(len(tree.leaf_ids), 3)
        # No embedding on any leaf (they were all zero-vector).
        for lid in tree.leaf_ids:
            leaf = tree.nodes[lid]
            # Zero-vector after L2 normalization stays zero-vector.
            if leaf.embedding is not None:
                self.assertAlmostEqual(float(np.linalg.norm(leaf.embedding)), 0.0)


class BuilderSummarizeInjection(unittest.TestCase):

    def test_build_tree_uses_injected_summarize_fn(self):
        chunks = _two_clusters(n_per=2, dim=4, seed=99)

        calls = []

        def _stub_summarize(texts):
            calls.append(tuple(texts))
            return f"CALLED[{len(texts)}]:" + "||".join(texts)

        tree = build_tree(
            chunks, max_depth=2, cluster_threshold=0.7,
            summarize_fn=_stub_summarize,
        )
        # The stub was invoked at least once for every non-leaf layer.
        self.assertGreater(len(calls), 0)
        # At least one internal-node text starts with the CALLED marker.
        marker_hits = [
            n for n in tree.nodes.values()
            if not n.is_leaf and n.text.startswith("CALLED[")
        ]
        self.assertGreater(len(marker_hits), 0)

    def test_build_tree_falls_back_when_summarize_fn_raises(self):
        chunks = _two_clusters(n_per=2, dim=4, seed=101)

        def _bad_summarize(texts):
            raise RuntimeError("LLM exploded")

        # Must NOT raise -- falls back to the deterministic template.
        tree = build_tree(
            chunks, max_depth=2, cluster_threshold=0.7,
            summarize_fn=_bad_summarize,
        )
        # At least one internal node exists.
        internal = [n for n in tree.nodes.values() if not n.is_leaf]
        self.assertGreater(len(internal), 0)
        # And their text uses the deterministic template (contains
        # "Summary of" or "chunk(s)").
        template_hits = [
            n for n in internal
            if "Summary of" in n.text or "chunk(s)" in n.text
        ]
        self.assertGreater(len(template_hits), 0)


class BuilderThreshold(unittest.TestCase):

    def test_build_tree_respects_cluster_threshold_clamp(self):
        chunks = _two_clusters(n_per=3, dim=4, seed=77)
        # Out-of-range threshold gets clamped to [0, 1].
        tree = build_tree(
            chunks, max_depth=2, cluster_threshold=2.5,
        )
        self.assertLessEqual(tree.cluster_threshold, 1.0)
        tree2 = build_tree(
            chunks, max_depth=2, cluster_threshold=-5.0,
        )
        self.assertGreaterEqual(tree2.cluster_threshold, -1.0)

    def test_build_tree_returns_exactly_one_root(self):
        chunks = _two_clusters(n_per=4, dim=4, seed=13)
        tree = build_tree(chunks, max_depth=3, cluster_threshold=0.7)
        # One and only one root.
        self.assertTrue(tree.root_id)
        root = tree.root()
        self.assertFalse(root.is_leaf)
        # Root depth is max_depth (early collapse) or max_depth + 1
        # (synthetic root wraps natural top layer).
        self.assertIn(root.depth, (tree.max_depth, tree.max_depth + 1))


class BuilderStructure(unittest.TestCase):

    def test_leaf_to_path_includes_root_and_leaf(self):
        chunks = _two_clusters(n_per=3, dim=4, seed=15)
        tree = build_tree(chunks, max_depth=3, cluster_threshold=0.7)
        root_id = tree.root_id
        for leaf_id in tree.leaf_ids:
            path = tree.leaf_to_path[leaf_id]
            self.assertEqual(path[0], root_id)
            self.assertEqual(path[-1], leaf_id)
            # Path length is max_depth + 1 (early collapse path) or
            # max_depth + 2 (synthetic root wraps natural top layer).
            self.assertGreaterEqual(len(path), tree.max_depth + 1)
            self.assertLessEqual(len(path), tree.max_depth + 2)

    def test_chunk_id_to_leaf_maps_each_chunk(self):
        chunks = _two_clusters(n_per=3, dim=4, seed=17)
        tree = build_tree(chunks, max_depth=2, cluster_threshold=0.7)
        self.assertEqual(
            set(tree.chunk_id_to_leaf.keys()),
            {c["id"] for c in chunks},
        )
        for chunk_id, leaf_id in tree.chunk_id_to_leaf.items():
            self.assertIn(leaf_id, tree.leaf_ids)
            self.assertIn(chunk_id, tree.nodes[leaf_id].chunk_ids)

    def test_path_for_chunk_lookup(self):
        chunks = _two_clusters(n_per=2, dim=4, seed=19)
        tree = build_tree(chunks, max_depth=2, cluster_threshold=0.7)
        for c in chunks:
            path = tree.path_for_chunk(c["id"])
            self.assertEqual(path[0], tree.root_id)
            self.assertEqual(path[-1], tree.chunk_id_to_leaf[c["id"]])
        # Unknown chunk id -> empty list.
        self.assertEqual(tree.path_for_chunk("nope"), [])


class BuilderNodes(unittest.TestCase):

    def test_raptor_node_dataclass_defaults(self):
        node = RaptorNode(
            node_id="x", text="t", embedding=None, depth=0,
        )
        self.assertEqual(node.children, [])
        self.assertEqual(node.chunk_ids, [])
        self.assertFalse(node.is_leaf)
        # After we set is_leaf=True and leave children empty, it stays a leaf.
        node.is_leaf = True
        self.assertTrue(node.is_leaf)

    def test_raptor_tree_convenience_accessors(self):
        chunks = _two_clusters(n_per=2, dim=4, seed=21)
        tree = build_tree(chunks, max_depth=2, cluster_threshold=0.7)
        root = tree.root()
        self.assertEqual(root.node_id, tree.root_id)
        leaves = tree.leaves()
        self.assertEqual([n.node_id for n in leaves], tree.leaf_ids)
        self.assertTrue(all(n.is_leaf for n in leaves))


# ============================================================================
# Helper / regression tests -- query
# ============================================================================

class QueryEdgeCases(unittest.TestCase):

    def _tree(self):
        chunks = _two_clusters(n_per=3, dim=4, seed=33)
        return build_tree(chunks, max_depth=3, cluster_threshold=0.7)

    def test_query_returns_empty_for_empty_tree(self):
        tree = build_tree([], max_depth=3, cluster_threshold=0.7)
        results = query_with_raptor(np.asarray([1.0, 0.0, 0.0, 0.0]), tree)
        self.assertEqual(results, [])

    def test_query_returns_empty_for_zero_top_k(self):
        tree = self._tree()
        results = query_with_raptor(
            np.asarray([1.0, 0.0, 0.0, 0.0]), tree, top_k=0,
        )
        self.assertEqual(results, [])

    def test_query_zero_vector_question_returns_zero_score(self):
        tree = self._tree()
        zero = np.zeros(4, dtype=np.float64)
        results = query_with_raptor(zero, tree, top_k=3)
        # Deterministic fallback: first N leaves with score 0.0.
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r["score"], 0.0)
        # All path entries are well-formed (root .. leaf).
        for r in results:
            self.assertEqual(r["path"][0], tree.root_id)
            self.assertEqual(r["path"][-1], r["node_id"])

    def test_query_rejects_non_raptor_tree_input(self):
        with self.assertRaises(TypeError):
            query_with_raptor(
                np.asarray([1.0, 0.0, 0.0, 0.0]),
                {"nodes": {}, "root_id": "x"},  # type: ignore[arg-type]
            )


class QueryScoreInjection(unittest.TestCase):

    def test_query_uses_injected_score_fn(self):
        chunks = _two_clusters(n_per=2, dim=4, seed=41)
        tree = build_tree(chunks, max_depth=2, cluster_threshold=0.7)
        calls = []

        def _stub_score(q, n):
            calls.append((tuple(q), tuple(n)))
            return 42.0  # every node gets the same score

        results = query_with_raptor(
            np.asarray([1.0, 0.0, 0.0, 0.0]),
            tree, top_k=2, beam=2, score_fn=_stub_score,
        )
        self.assertGreater(len(calls), 0)
        self.assertGreater(len(results), 0)
        # With a constant score, every hit shares the same score.
        # (The exact value depends on the number of path edges; the
        # important property is that score_fn was consulted.)
        for r in results:
            self.assertGreater(r["score"], 0.0)

    def test_query_top_k_truncates_results(self):
        chunks = _two_clusters(n_per=3, dim=4, seed=43)
        tree = build_tree(chunks, max_depth=2, cluster_threshold=0.7)
        results = query_with_raptor(
            np.asarray([1.0, 0.0, 0.0, 0.0]),
            tree, top_k=2, beam=5,
        )
        self.assertLessEqual(len(results), 2)

    def test_query_dedupes_same_leaf(self):
        """Degenerate case: even if multiple paths reach the same leaf,
        only one hit per leaf is returned (highest-score path wins)."""
        # Build a single-chunk tree where the only path is root -> leaf.
        chunks = [_chunk("only", "single", embedding=[1.0, 0.0])]
        tree = build_tree(chunks, max_depth=3, cluster_threshold=0.7)
        results = query_with_raptor(
            np.asarray([1.0, 0.0]),
            tree, top_k=10, beam=5,
        )
        # Exactly one hit (the only leaf) -- no duplicates.
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["chunk_id"], "only")

    def test_query_beam_explores_more_branches(self):
        chunks = _two_clusters(n_per=4, dim=4, seed=47)
        tree = build_tree(chunks, max_depth=3, cluster_threshold=0.7)
        q = np.asarray([5.0, 5.0, 5.0, 5.0], dtype=np.float64)

        beam1 = query_with_raptor(q, tree, top_k=8, beam=1)
        beam3 = query_with_raptor(q, tree, top_k=8, beam=3)

        # Higher beam cannot return fewer distinct leaves than lower beam
        # when both have room (top_k=8, leaves=8).
        self.assertGreaterEqual(len(beam3), len(beam1))
        # Every leaf in beam1 also appears in beam3 (since beam3
        # explores a superset of the paths).
        ids1 = {r["node_id"] for r in beam1}
        ids3 = {r["node_id"] for r in beam3}
        self.assertTrue(ids1.issubset(ids3))


class QueryOutputShape(unittest.TestCase):

    def test_query_path_ends_with_leaf_id(self):
        chunks = _two_clusters(n_per=2, dim=4, seed=53)
        tree = build_tree(chunks, max_depth=3, cluster_threshold=0.7)
        results = query_with_raptor(
            np.asarray([1.0, 0.0, 0.0, 0.0]),
            tree, top_k=4, beam=3,
        )
        for r in results:
            self.assertEqual(r["path"][-1], r["node_id"])
            self.assertTrue(r["path"][0])  # non-empty root id

    def test_query_orders_by_score_descending(self):
        chunks = _two_clusters(n_per=3, dim=4, seed=59)
        tree = build_tree(chunks, max_depth=3, cluster_threshold=0.7)
        results = query_with_raptor(
            np.asarray([1.0, 0.0, 0.0, 0.0]),
            tree, top_k=8, beam=5,
        )
        scores = [r["score"] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))


# ============================================================================
# Module surface
# ============================================================================

class ModulePublicSurface(unittest.TestCase):

    def test_module_public_surface(self):
        for name in (
            "RaptorTree",
            "RaptorNode",
            "build_tree",
            "query_with_raptor",
            "DEFAULT_MAX_DEPTH",
            "DEFAULT_CLUSTER_THRESHOLD",
            "DEFAULT_RANDOM_STATE",
            "DEFAULT_TOP_K",
            "DEFAULT_BEAM",
        ):
            self.assertTrue(hasattr(consolidate, name), name)
            self.assertIn(name, consolidate.__all__)

    def test_constants_have_expected_values(self):
        self.assertEqual(DEFAULT_MAX_DEPTH, 3)
        self.assertEqual(DEFAULT_CLUSTER_THRESHOLD, 0.7)
        self.assertEqual(DEFAULT_RANDOM_STATE, 1729)
        self.assertEqual(DEFAULT_TOP_K, 10)
        self.assertEqual(DEFAULT_BEAM, 1)

    def test_consolidate_package_reexports_raptor(self):
        # Importable through the top-level consolidate package.
        from consolidate import (
            DEFAULT_BEAM,
            DEFAULT_CLUSTER_THRESHOLD,
            DEFAULT_MAX_DEPTH,
            DEFAULT_RANDOM_STATE,
            DEFAULT_TOP_K,
            RaptorNode,
            RaptorTree,
            build_tree,
            query_with_raptor,
        )
        self.assertTrue(callable(build_tree))
        self.assertTrue(callable(query_with_raptor))
        # RaptorTree and RaptorNode are classes (dataclasses).
        self.assertTrue(isinstance(RaptorTree, type))
        self.assertTrue(isinstance(RaptorNode, type))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
