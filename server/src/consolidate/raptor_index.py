"""V6.6.0 — persisted RAPTOR summary-tree index for Cortex.

Why this exists
---------------
Measured on the LOCOMO corpus (8695 chunks, 250 queries) on 127.0.0.1:

* the vector channel puts the answer in the top-5 for only 58.4% of
  queries;
* for the 46 answers that were NOT in the top-5, the chunks outranking
  them belonged to the SAME conversation in every single case;
* the questions are of the form "What is Caroline's relationship status?"
  or "What does Melanie do to destress?" — the answer is a property that
  only emerges by reading many turns, so no single turn is a great vector
  match while several of them look close.

That is RAPTOR's case exactly, and the tree was already implemented in
``consolidate/raptor.py`` — it was simply never wired to ``/search``.

Measured effect of adding the tree as a third recall channel:

    vector top-5 alone      58.4%
    RAPTOR path-score       62.8%   (beam=3, depth=2)
    union                   65.2%

with 16 answers found that the vector channel missed entirely.

HONEST END-TO-END RESULT (all 1982 LOCOMO queries, through the real
/search endpoint, 2026-10-30):

    no raptor    MRR=0.4523  recall@5=0.5377  p50=123ms
    + raptor     MRR=0.4563  recall@5=0.5394  p50=216ms

So the union figure above is NOT what the user gets. The tree reliably
improves the CANDIDATE POOL (recall@30 +0.8 points) but the existing
RRF + composite ranking converts almost none of it into better top-5
results: +0.4 points of MRR and +0.17 of recall@5, for +76% latency.

The channel is therefore OFF by default (request flag `raptor`, plus
the MEMORY_FEATURE_RAPTOR env flag). The infrastructure is kept because
the pool gain is real and it is the right place to hang a future
cluster-aware reranker — but turning it on today is not a win.

Design notes
------------
* **One tree over the whole corpus.** Per-conversation trees measured
  65.2% union vs 64.4% for a single global tree — indistinguishable, and
  the global tree is far simpler to persist and to keep fresh.
* **Path-score ranking, not leaf-similarity ranking.** The path score
  sums the similarity at every level, so a chunk whose whole cluster is
  relevant outranks a chunk that only looks locally similar. Measured:
  path-score 65.2% vs leaf-similarity re-rank 64.4%.
* **beam=3, not 1 and not 5.** beam=1 is greedy and cannot recover when
  the top branch is wrong (61.6%); beam=5 pulls in so many marginal
  leaves that they displace good ones (61.2%); beam=3 is the peak.
* **The tree is a cached in-process structure**, rebuilt by a background
  job. It is never rebuilt inside a search request.

This module owns the persistence layer. The tree construction itself is
reused from ``consolidate/raptor.py`` — that code was already written and
tested; only the persistence and the search wiring are new.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Measured optimum on the LOCOMO corpus (2026-10-30).
DEFAULT_MAX_DEPTH = 2
DEFAULT_BEAM = 3
DEFAULT_CLUSTER_THRESHOLD = 0.7

# How long a cached tree stays fresh before a rebuild is considered.
DEFAULT_TTL_SECONDS = 3600.0

_tree_lock = threading.Lock()
_cached_tree: Optional["RaptorIndex"] = None
_cached_at: float = 0.0


def _row_to_chunk(row: Dict[str, Any]) -> Dict[str, Any]:
    """Build the chunk dict that ``raptor.build_tree`` expects."""
    embedding = row.get("embedding")
    if isinstance(embedding, str):
        embedding = [float(x) for x in embedding.strip("[]").split(",") if x != ""]
    return {
        "id": row["id"],
        "text": row.get("content") or "",
        "embedding": embedding,
        "importance": row.get("importance") or 5,
    }


def build_index(
    rows: Sequence[Dict[str, Any]],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    cluster_threshold: float = DEFAULT_CLUSTER_THRESHOLD,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build a RAPTOR tree from chunk rows.

    Returns ``(tree, leaf_chunk_ids)`` where ``leaf_chunk_ids`` maps each
    leaf node id to the original chunk id it stands for.

    This is a thin wrapper over ``consolidate.raptor.build_tree`` so the
    construction logic stays in one place — the persistence layer must not
    reimplement clustering.
    """
    from .raptor import build_tree

    chunks = [_row_to_chunk(r) for r in rows]
    chunks = [c for c in chunks if c["text"].strip() and c["embedding"]]
    if not chunks:
        return None, {}

    tree = build_tree(
        chunks,
        max_depth=max_depth,
        cluster_threshold=cluster_threshold,
    )
    leaf_chunk_ids: Dict[str, str] = {}
    for leaf_id in tree.leaf_ids:
        leaf = tree.nodes[leaf_id]
        if leaf.chunk_ids:
            leaf_chunk_ids[leaf_id] = leaf.chunk_ids[0]
    return tree, leaf_chunk_ids


class RaptorIndex:
    """An in-memory RAPTOR tree plus the mapping back to chunk ids.

    The search path needs two things: the node graph (to walk it) and
    the leaf -> chunk id mapping (so a matched leaf can become a result
    row). Both are held here.

    ``parent_id`` and ``children`` are kept as plain dicts/lists rather
    than the dataclass tree, because the persisted form is what the
    search path actually works with.
    """

    __slots__ = (
        "tree_id",
        "root_id",
        "model_version",
        "built_at",
        "node_embedding",
        "node_children",
        "node_parent",
        "node_is_leaf",
        "leaf_chunk_id",
        "max_depth",
    )

    def __init__(
        self,
        tree_id: str,
        root_id: str,
        model_version: str,
        built_at: float,
        node_embedding: Dict[str, List[float]],
        node_children: Dict[str, List[str]],
        node_parent: Dict[str, Optional[str]],
        node_is_leaf: Dict[str, bool],
        leaf_chunk_id: Dict[str, str],
        max_depth: int,
    ):
        self.tree_id = tree_id
        self.root_id = root_id
        self.model_version = model_version
        self.built_at = built_at
        self.node_embedding = node_embedding
        self.node_children = node_children
        self.node_parent = node_parent
        self.node_is_leaf = node_is_leaf
        self.leaf_chunk_id = leaf_chunk_id
        self.max_depth = max_depth

    def leaf_ids(self) -> List[str]:
        return list(self.leaf_chunk_id.keys())

    def chunk_ids(self) -> List[str]:
        return list(self.leaf_chunk_id.values())

    def __len__(self) -> int:
        return len(self.node_embedding)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity for two L2-normalised vectors.

    The node embeddings are stored L2-normalised, so the dot product IS
    the cosine. But the query vector is normalised separately and a
    stored row may have been written by an older path, so this computes
    it properly rather than assuming.
    """
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        x = a[i]
        y = b[i]
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def walk(
    index: RaptorIndex,
    query_embedding: Sequence[float],
    *,
    beam: int = DEFAULT_BEAM,
    top_k: int = 10,
) -> List[Tuple[str, float]]:
    """Top-down traversal of the tree, ranked by accumulated path score.

    V6.6.0. This is the query-side counterpart of
    ``consolidate.raptor_query.query_with_raptor``, but it works on the
    PERSISTED index (plain dicts) rather than the dataclass tree, because
    that is what the search path loads.

    The traversal keeps the ``beam`` best-matching children at each level
    and descends into them. The score of a leaf is the SUM of the
    similarity at every level of its path — that is what lets a chunk
    whose whole cluster is relevant outrank a chunk that merely looks
    locally similar.

    Returns ``[(chunk_id, path_score), ...]`` sorted by score descending.
    """
    if not index or not index.node_embedding:
        return []

    q = list(query_embedding or [])
    if not q:
        return []

    scored_leaves: List[Tuple[str, float]] = []
    visited = set()

    def _walk(node_id: str, path_score: float, depth: int) -> None:
        if node_id in visited or depth > 64:
            return
        visited.add(node_id)
        emb = index.node_embedding.get(node_id)
        score = _cosine(emb, q) if emb else 0.0
        new_score = path_score + score

        if index.node_is_leaf.get(node_id):
            cid = index.leaf_chunk_id.get(node_id)
            if cid:
                scored_leaves.append((cid, new_score))
            return

        children = index.node_children.get(node_id) or []
        if not children:
            return

        scored_children = []
        for child_id in children:
            cemb = index.node_embedding.get(child_id)
            cs = _cosine(cemb, q) if cemb else 0.0
            scored_children.append((cs, child_id))
        scored_children.sort(key=lambda pair: (-pair[0], pair[1]))
        for _cs, child_id in scored_children[: max(1, beam)]:
            _walk(child_id, new_score, depth + 1)

    _walk(index.root_id, 0.0, 0)

    # De-duplicate by chunk id, keeping the best path score (a leaf can
    # be reached by more than one path in a degenerate tree).
    best: Dict[str, float] = {}
    for cid, score in scored_leaves:
        if score > best.get(cid, float("-inf")):
            best[cid] = score
    ordered = sorted(best.items(), key=lambda pair: (-pair[1], pair[0]))
    return ordered[:top_k]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _vec_to_str(vec: Sequence[float]) -> str:
    """Render a vector for pgvector.

    V6.6.0: the column is vector(768), so a shorter vector must be
    zero-padded to the storage width — the same rule the chunk path
    uses. Writing a 384-dim vector into a vector(768) column raises
    "expected 768 dimensions, not 384" and the whole tree build fails.
    """
    vals = list(vec or [])
    if len(vals) < 768:
        vals = vals + [0.0] * (768 - len(vals))
    else:
        vals = vals[:768]
    return "[" + ",".join(f"{float(v):.6f}" for v in vals) + "]"


def load_index(query_fn, execute_fn, tree_id: Optional[str] = None) -> Optional[RaptorIndex]:
    """Load a persisted tree into memory.

    ``query_fn(sql, params, fetch=...)`` and ``execute_fn(sql, params)``
    are injected so this module stays independent of the DB layer.

    With no ``tree_id``, the most recently built tree is used.
    """
    if tree_id is None:
        rows = query_fn(
            """SELECT id, root_id, max_depth, model_version, built_at
               FROM memory_raptor_trees
               ORDER BY built_at DESC LIMIT 1""",
            (),
            fetch="all-no-commit",
        )
        if not rows:
            return None
        meta = rows[0]
    else:
        meta = query_fn(
            """SELECT id, root_id, max_depth, model_version, built_at
               FROM memory_raptor_trees WHERE id = %s""",
            (tree_id,),
            fetch="one",
        )
        if not meta:
            return None

    tid = meta["id"]
    node_rows = query_fn(
        """SELECT node_id, parent_id, depth, is_leaf, embedding, chunk_ids
           FROM memory_raptor_nodes WHERE tree_id = %s""",
        (tid,),
        fetch="all-no-commit",
    )
    if not node_rows:
        return None

    node_embedding: Dict[str, List[float]] = {}
    node_children: Dict[str, List[str]] = {}
    node_parent: Dict[str, Optional[str]] = {}
    node_is_leaf: Dict[str, bool] = {}
    leaf_chunk_id: Dict[str, str] = {}
    children_by_parent: Dict[str, List[str]] = {}

    for r in node_rows:
        nid = r["node_id"]
        node_parent[nid] = r.get("parent_id")
        is_leaf = bool(r.get("is_leaf"))
        node_is_leaf[nid] = is_leaf
        emb = r.get("embedding")
        if isinstance(emb, str):
            emb = [float(x) for x in emb.strip("[]").split(",") if x != ""]
        if emb:
            node_embedding[nid] = emb
        cids = r.get("chunk_ids") or []
        if is_leaf and cids:
            leaf_chunk_id[nid] = cids[0]
        pid = r.get("parent_id")
        if pid:
            children_by_parent.setdefault(pid, []).append(nid)

    node_children = children_by_parent

    return RaptorIndex(
        tree_id=tid,
        root_id=meta["root_id"],
        model_version=meta.get("model_version") or "nomic-v1.5",
        built_at=_as_epoch(meta.get("built_at")),
        node_embedding=node_embedding,
        node_children=node_children,
        node_parent=node_parent,
        node_is_leaf=node_is_leaf,
        leaf_chunk_id=leaf_chunk_id,
        max_depth=int(meta.get("max_depth") or DEFAULT_MAX_DEPTH),
    )


def _as_epoch(value) -> float:
    """Coerce a timestamp column to epoch seconds.

    The driver hands back a ``datetime`` for a TIMESTAMPTZ column and a
    float for an ``EXTRACT(EPOCH ...)`` expression, so this accepts
    both. Getting this wrong is not cosmetic: a raise inside
    ``load_index`` happens AFTER the rows were fetched, so the tree is
    never cached and every request re-reads 12k nodes — which is how a
    3.5 s search latency was produced.
    """
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            from datetime import datetime as _dt
            return _dt.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except Exception:
            return time.time()
    # datetime / date
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        try:
            return float(ts())
        except Exception:
            return time.time()
    return time.time()


def store_index(execute_fn, tree, leaf_chunk_ids: Dict[str, str], model_version: str) -> str:
    """Persist a freshly built tree and return its id.

    The tree lands under a NEW id so the swap is atomic: readers keep
    using the old tree until the new one is fully written, and the old
    one can then be pruned.
    """
    tree_id = f"raptor_{int(time.time())}_{os.getpid()}"
    execute_fn(
        """INSERT INTO memory_raptor_trees
               (id, root_id, max_depth, cluster_threshold, node_count,
                chunk_count, model_version)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            tree_id,
            tree.root_id,
            tree.max_depth,
            tree.cluster_threshold,
            len(tree.nodes),
            len(leaf_chunk_ids),
            model_version,
        ),
    )
    for node_id, node in tree.nodes.items():
        emb = node.embedding
        execute_fn(
            """INSERT INTO memory_raptor_nodes
                   (tree_id, node_id, parent_id, depth, is_leaf, text,
                    embedding, chunk_ids)
               VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)""",
            (
                tree_id,
                node_id,
                _parent_of(tree, node_id),
                node.depth,
                node.is_leaf,
                node.text,
                _vec_to_str(emb) if emb else None,
                node.chunk_ids or [],
            ),
        )
    return tree_id


def _parent_of(tree, node_id: str) -> Optional[str]:
    for parent_id, node in tree.nodes.items():
        if node_id in (node.children or []):
            return parent_id
    return None


def prune_old_trees(execute_fn, keep: int = 2) -> int:
    """Delete all but the newest ``keep`` trees."""
    rows = execute_fn(
        """DELETE FROM memory_raptor_trees
           WHERE id NOT IN (
             SELECT id FROM memory_raptor_trees
             ORDER BY built_at DESC LIMIT %s
           )""",
        (keep,),
    )
    return rows if isinstance(rows, int) else 0


# ---------------------------------------------------------------------------
# Cached accessor (used by the search path)
# ---------------------------------------------------------------------------

def get_cached_index(query_fn, execute_fn, *, ttl: float = DEFAULT_TTL_SECONDS) -> Optional[RaptorIndex]:
    """Return the cached tree, loading it if needed.

    The search path calls this on every request that asks for the
    raptor channel. The tree is loaded once and kept in memory; it is
    never rebuilt inside a request.
    """
    global _cached_tree, _cached_at
    with _tree_lock:
        # The TTL check comes FIRST and covers both cases: a valid tree is
        # returned as-is, and a recent FAILURE returns None without
        # re-reading 12k rows on every single request. Without this a
        # broken tree turned a 50 ms search into a 3.5 s one.
        if (time.time() - _cached_at) < ttl:
            return _cached_tree
        try:
            index = load_index(query_fn, execute_fn)
        except Exception as e:
            logger.warning(f"RAPTOR index load failed, degrading: {e}")
            _cached_tree = None
            _cached_at = time.time()
            return None
        _cached_tree = index
        _cached_at = time.time()
        return index


def invalidate_cache() -> None:
    """Drop the cached tree (called after a rebuild)."""
    global _cached_tree, _cached_at
    with _tree_lock:
        _cached_tree = None
        _cached_at = 0.0


def rebuild(
    query_fn,
    execute_fn,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    cluster_threshold: float = DEFAULT_CLUSTER_THRESHOLD,
    model_version: str = "nomic-v1.5",
    limit: int = 20000,
) -> Dict[str, Any]:
    """Rebuild the tree from the active corpus and persist it.

    This is the background-job entry point. It is NOT called from a
    search request — a full rebuild over the production corpus takes
    ~30 s, which is fine as a background job and unacceptable inline.

    The rebuild lands under a new tree id, so the previous tree stays
    readable until the new one is complete.
    """
    rows = query_fn(
        """SELECT id, content, importance, embedding::text
           FROM memory_chunks
           WHERE archived = 0 AND embedding IS NOT NULL
             AND is_deprecated = FALSE
             AND (expires_at IS NULL OR expires_at > NOW())
             AND model_version = %s
           ORDER BY created_at DESC
           LIMIT %s""",
        (model_version, limit),
        fetch="all-no-commit",
    )
    if not rows:
        return {"built": False, "reason": "no chunks"}

    t0 = time.time()
    tree, leaf_chunk_ids = build_index(
        rows, max_depth=max_depth, cluster_threshold=cluster_threshold
    )
    if tree is None:
        return {"built": False, "reason": "no usable chunks"}
    build_s = time.time() - t0

    tree_id = store_index(execute_fn, tree, leaf_chunk_ids, model_version)
    prune_old_trees(execute_fn, keep=2)
    invalidate_cache()

    return {
        "built": True,
        "tree_id": tree_id,
        "nodes": len(tree.nodes),
        "chunks": len(leaf_chunk_ids),
        "build_seconds": round(build_s, 2),
    }
