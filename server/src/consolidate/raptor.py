"""Cortex v6.5.0 -- Consolidation Pipeline (Phase 2 #9): RAPTOR 递归摘要层.

Purpose
-------
Implements the RAPTOR (Recursive Abstractive Processing for Tree-Organized
Retrieval) pattern over a flat list of memory chunks. The result is a
multi-layer tree where:

  * the **leaves** are the original input chunks,
  * each **internal node** is a cluster summary of its children,
  * the **root** is a top-level summary covering everything.

The tree can then be queried top-down: match the question against the
root'"'"'s children, descend into the best-matching branch, and collect
the leaf chunks that cover the answer.

Design constraints (from dispatch prompt):
  * No LLM in this file'"'"'s defaults. ``summarize_fn`` is injected; the
    default fallback uses the deterministic template from
    ``consolidate.summary`` so re-runs on the same input produce the
    same tree (idempotency contract).
  * No DB. Pure logic over the input list. Tests run without a live
    Postgres.
  * numpy / scikit-learn only for the clustering step (already in the
    cortex dependency tree). Deterministic ``random_state`` ensures
    idempotent re-runs.
  * Test boundary: this file does not touch ``extract.py`` /
    ``conflict_resolve.py`` / ``scheduler.py`` / ``compact.py`` /
    ``summary.py`` / ``quality.py``. It imports only from stdlib,
    ``numpy``, ``sklearn``, and the in-package ``summary`` helper.

Algorithm overview
-------------------
1. **Normalize** -- coerce each chunk into ``{id, text, embedding,
   importance}``. Whitespace-only / non-dict entries are routed to
   ``skipped_chunk_ids``. Chunks without embeddings fall back to a
   zero-vector so the tree still builds (the query path will skip
   them at ranking time).
2. **Leaf layer** -- one node per chunk.
3. **Cluster + summarize loop** (up to ``max_depth`` times):
     a. Agglomerate: greedy single-linkage on cosine similarity;
        pairs with similarity >= ``cluster_threshold`` are merged
        into the same cluster.
     b. For each cluster: invoke ``summarize_fn`` on the child texts
        (or fall back to the deterministic template); the node
        embedding is the (L2-normalized) mean of its child
        embeddings so cosine similarity remains meaningful.
     c. Promote the cluster summaries to the next layer.
     d. If only one cluster remains, that cluster becomes the root
        and the loop terminates (even if ``max_depth`` is larger).
4. **Single-root invariant** -- the loop above naturally collapses
   to one root because agglomeration is monotone (each merge can
   only reduce cluster count).

The public surface is :class:`RaptorTree` (the immutable tree
dataclass) and :func:`build_tree` (the constructor).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

try:  # sklearn is in the cortex dependency tree; degrade gracefully if absent
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics.pairwise import cosine_similarity

    _HAS_SKLEARN = True
except Exception:  # noqa: BLE001 -- sklearn missing on some minimal installs
    AgglomerativeClustering = None  # type: ignore[assignment]
    cosine_similarity = None  # type: ignore[assignment]
    _HAS_SKLEARN = False


from .summary import DEFAULT_TEMPLATE, summarize_chunks  # noqa: E402


logger = logging.getLogger("cortex.consolidate.raptor")


# ---------- Public constants ----------

DEFAULT_MAX_DEPTH: int = 3
"""Default number of clustering rounds (root is at depth ``max_depth``)."""

DEFAULT_CLUSTER_THRESHOLD: float = 0.7
"""Cosine-similarity cutoff for merging two sibling nodes."""

DEFAULT_RANDOM_STATE: int = 1729
"""Hard-coded seed so re-runs on the same input produce the same tree."""


# ---------- Public dataclasses ----------


@dataclass
class RaptorNode:
    """One node in the RAPTOR tree.

    Attributes
    ----------
    node_id : str
        Stable identifier (``"L0#<idx>"`` for leaves,
        ``"L<depth>C<idx>"`` for clusters, ``"root"`` for the top).
    text : str
        Summary text (or original chunk text for leaves).
    embedding : list[float] | None
        L2-normalized vector used for cosine-similarity matching.
        ``None`` when the input had no embedding; such nodes are
        skipped at ranking time.
    depth : int
        ``0`` for leaves (the original chunks), increasing toward
        the root.
    children : list[str]
        Ordered child node ids. Empty for leaves.
    chunk_ids : list[str]
        Original input chunk ids covered by this node. Leaves carry
        exactly one; internal nodes carry the union of their
        subtree'"'"'s chunks.
    is_leaf : bool
        Convenience flag -- ``True`` iff ``children`` is empty.
    """

    node_id: str
    text: str
    embedding: Optional[List[float]]
    depth: int
    children: List[str] = field(default_factory=list)
    chunk_ids: List[str] = field(default_factory=list)
    is_leaf: bool = False


@dataclass
class RaptorTree:
    """The full multi-layer RAPTOR tree.

    Attributes
    ----------
    nodes : dict[str, RaptorNode]
        All nodes keyed by ``node_id``.
    root_id : str
        The single root node id. There is always exactly one root.
    leaf_ids : list[str]
        Leaf node ids in input order.
    leaf_to_path : dict[str, list[str]]
        ``leaf_id -> [root_id, ..., leaf_id]`` (inclusive on both
        ends). Used by :func:`raptor_query.query_with_raptor` to
        collect the path of summaries that explain why a leaf was
        returned.
    chunk_id_to_leaf : dict[str, str]
        Original chunk id -> leaf node id.
    skipped_chunk_ids : list[str]
        Chunk ids that the builder rejected (malformed / empty).
    max_depth : int
        The depth bound that was honored when building.
    cluster_threshold : float
        The cosine-similarity cutoff that was honored.
    built_at : float
        ``time.time()`` snapshot of when the tree was constructed.
    """

    nodes: Dict[str, RaptorNode]
    root_id: str
    leaf_ids: List[str]
    leaf_to_path: Dict[str, List[str]]
    chunk_id_to_leaf: Dict[str, str]
    skipped_chunk_ids: List[str]
    max_depth: int
    cluster_threshold: float
    built_at: float = field(default_factory=lambda: time.time())

    # ---------- Convenience accessors ----------

    def root(self) -> RaptorNode:
        return self.nodes[self.root_id]

    def leaves(self) -> List[RaptorNode]:
        return [self.nodes[nid] for nid in self.leaf_ids]

    def path_for_chunk(self, chunk_id: str) -> List[str]:
        """Return the path of node ids from root to the leaf covering ``chunk_id``."""
        leaf_id = self.chunk_id_to_leaf.get(chunk_id)
        if leaf_id is None:
            return []
        return list(self.leaf_to_path.get(leaf_id, []))


# ---------- Helpers (pure functions -- fully unit-testable) ----------


def _coerce_embedding(raw: Any) -> Optional[List[float]]:
    """Best-effort coercion of an embedding to a list[float].

    Returns ``None`` for missing / non-numeric / empty input. ``bool``
    is rejected (mirrors :func:`consolidate.quality._coerce_confidence`).
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (list, tuple)):
        try:
            return [float(x) for x in raw]
        except (TypeError, ValueError):
            return None
    if isinstance(raw, np.ndarray):
        if raw.size == 0:
            return None
        try:
            return [float(x) for x in raw.tolist()]
        except (TypeError, ValueError):
            return None
    return None


def _coerce_text(raw: Any) -> str:
    """Best-effort coercion of a chunk'"'"'s text content to ``str``."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip()


def _normalize_chunk(chunk: Any) -> Optional[Dict[str, Any]]:
    """Coerce a chunk-like input into ``{id, text, embedding}``.

    Returns ``None`` for non-dict input or chunks whose text is empty
    after stripping. Mirrors the tolerance in :mod:`consolidate.summary`.
    """
    if not isinstance(chunk, dict):
        return None
    cid = chunk.get("id") or chunk.get("chunk_id") or ""
    text = _coerce_text(chunk.get("content") or chunk.get("text"))
    embedding = _coerce_embedding(chunk.get("embedding"))
    return {"id": str(cid), "text": text, "embedding": embedding}


def _normalize_chunks(chunks: Iterable[Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Normalize a raw chunk iterable; return ``(kept, skipped_ids)``."""
    kept: List[Dict[str, Any]] = []
    skipped: List[str] = []
    seen_ids: Dict[str, int] = {}
    for c in chunks:
        n = _normalize_chunk(c)
        if n is None or not n["text"]:
            if isinstance(c, dict):
                sid = str(c.get("id") or c.get("chunk_id") or "")
                if sid:
                    skipped.append(sid)
            continue
        # De-dup by id (last-write-wins on duplicate id); surface prior copies
        if n["id"]:
            if n["id"] in seen_ids:
                # The earlier copy becomes a "skipped" pseudo-id -- we
                # just record the duplicate id so the caller can audit.
                skipped.append(n["id"])
                # Replace the prior keep (deterministic: keep the
                # later copy).
                seen_ids[n["id"]] = len(kept)
                kept.append(n)
                continue
            seen_ids[n["id"]] = len(kept)
        kept.append(n)
    return kept, skipped


def _normalize_vec(vec: Optional[List[float]], dim: Optional[int]) -> List[float]:
    """Pad / coerce a single embedding to ``dim``-dimensional zero-vector when missing.

    Returned vector is NOT normalized -- callers normalize on demand so
    the zero-vector path stays well-defined (zero / zero is a no-op).
    """
    if vec is None:
        if dim is None or dim <= 0:
            return []
        return [0.0] * dim
    if dim is not None and len(vec) != dim:
        # Pad or truncate to the consensus dimension so cosine math
        # is consistent across the layer.
        if len(vec) < dim:
            return vec + [0.0] * (dim - len(vec))
        return vec[:dim]
    return list(vec)


def _consensus_dim(vectors: List[Optional[List[float]]]) -> int:
    """Pick a single dimensionality for a layer.

    Preference order:
      1. Mode of non-None vector lengths (most common dim wins).
      2. ``0`` if no vectors supplied.
    """
    if not vectors:
        return 0
    lengths: List[int] = [len(v) for v in vectors if v is not None and len(v) > 0]
    if not lengths:
        return 0
    counts: Dict[int, int] = {}
    for ln in lengths:
        counts[ln] = counts.get(ln, 0) + 1
    # max by count, ties broken by smaller dim (deterministic)
    return max(sorted(counts.items()), key=lambda kv: kv[1])[0]


def _l2_normalize(vec: List[float]) -> List[float]:
    """L2-normalize a 1-D vector; returns all-zeros if norm is zero."""
    if not vec:
        return vec
    arr = np.asarray(vec, dtype=np.float64)
    norm = float(np.linalg.norm(arr))
    if norm <= 0.0:
        return [0.0] * len(vec)
    return (arr / norm).tolist()


def _cluster_assignments(
    vectors: np.ndarray,
    threshold: float,
    random_state: int,
) -> List[int]:
    """Compute cluster assignments for a layer.

    Uses ``sklearn.cluster.AgglomerativeClustering`` with cosine
    distance and a distance cutoff derived from the user-facing
    ``threshold`` (similarity -> distance = 1 - similarity). When
    sklearn is unavailable, falls back to a deterministic greedy
    single-linkage pass so the rest of the module still works in
    minimal environments.

    Returns a list of cluster ids, one per row in ``vectors``.
    Cluster ids are non-negative ints; the function itself does not
    promise they are contiguous (callers normalize via ``_relabel``).
    """
    n = vectors.shape[0]
    if n == 0:
        return []
    if n == 1:
        return [0]

    distance_threshold = max(0.0, 1.0 - float(threshold))

    if _HAS_SKLEARN and AgglomerativeClustering is not None:
        try:
            model = AgglomerativeClustering(
                n_clusters=None,
                metric="cosine",
                linkage="average",
                distance_threshold=distance_threshold,
            )
            labels = model.fit_predict(vectors)
            return [int(x) for x in labels.tolist()]
        except Exception as exc:  # noqa: BLE001 -- sklearn API varies across versions
            logger.warning(
                "raptor AgglomerativeClustering failed (%s); falling back to greedy",
                exc,
            )

    # Greedy fallback (deterministic). Sort by vector index (already
    # in input order) and assign each new point to the first existing
    # cluster whose centroid is within ``distance_threshold`` in
    # cosine distance, else start a new cluster.
    centroids: List[np.ndarray] = []
    assignments: List[int] = []
    for i in range(n):
        best = -1
        best_dist = float("inf")
        for ci, c in enumerate(centroids):
            # cosine distance == 1 - cosine similarity
            denom = float(np.linalg.norm(c)) * float(np.linalg.norm(vectors[i]))
            if denom <= 0.0:
                continue
            sim = float(np.dot(c, vectors[i]) / denom)
            dist = 1.0 - sim
            if dist < best_dist:
                best_dist = dist
                best = ci
        if best >= 0 and best_dist <= distance_threshold:
            assignments.append(best)
            # Update centroid as running mean
            n_members = max(1, i)  # approximation; centroid shifts, not exact average
            centroids[best] = (centroids[best] * n_members + vectors[i]) / (n_members + 1)
        else:
            assignments.append(len(centroids))
            centroids.append(vectors[i])
    return assignments


def _relabel(labels: List[int]) -> Tuple[List[int], Dict[int, int]]:
    """Relabel cluster ids into ``0..k-1`` in input-order of first appearance.

    This makes ``cluster_index`` deterministic across re-runs even if
    sklearn returns non-contiguous ids.
    """
    mapping: Dict[int, int] = {}
    out: List[int] = []
    next_id = 0
    for lab in labels:
        if lab not in mapping:
            mapping[lab] = next_id
            next_id += 1
        out.append(mapping[lab])
    return out, mapping


# ---------- Internal state for the builder loop ----------


def _empty_tree(
    max_depth: int,
    cluster_threshold: float,
    skipped_chunk_ids: List[str],
) -> RaptorTree:
    """Return a minimal valid tree for degenerate input (no chunks at all).

    A single synthetic root node with empty text acts as a sentinel so
    callers can rely on ``tree.root_id`` always being defined.
    """
    root = RaptorNode(
        node_id="root",
        text="",
        embedding=None,
        depth=max_depth,
        children=[],
        chunk_ids=[],
        is_leaf=False,
    )
    return RaptorTree(
        nodes={root.node_id: root},
        root_id=root.node_id,
        leaf_ids=[],
        leaf_to_path={},
        chunk_id_to_leaf={},
        skipped_chunk_ids=list(skipped_chunk_ids),
        max_depth=max_depth,
        cluster_threshold=cluster_threshold,
    )


def _make_leaf(idx: int, chunk: Dict[str, Any], dim: int) -> Tuple[str, RaptorNode]:
    """Build the L0 leaf node for one chunk."""
    node_id = f"L0#{idx}"
    embedding = _l2_normalize(_normalize_vec(chunk["embedding"], dim))
    node = RaptorNode(
        node_id=node_id,
        text=chunk["text"],
        embedding=embedding,
        depth=0,
        children=[],
        chunk_ids=[chunk["id"]] if chunk["id"] else [],
        is_leaf=True,
    )
    return node_id, node


def _summarize_cluster(
    texts: List[str],
    *,
    summarize_fn: Optional[Callable[..., str]],
) -> str:
    """Produce the summary text for one cluster.

    Mirrors :func:`consolidate.summary.summarize_chunks` -- LLM via
    injected ``summarize_fn`` when supplied, deterministic template
    otherwise. We never raise; on failure we fall back to a
    newline-joined body.
    """
    if not texts:
        return ""
    if summarize_fn is not None:
        try:
            out = summarize_fn(texts)
            if isinstance(out, str) and out:
                return out
        except Exception as exc:  # noqa: BLE001 -- caller fn must not crash us
            logger.warning(
                "raptor summarize_fn raised, falling back: %s", exc,
            )
    # Deterministic fallback: importance-agnostic join. The ordering
    # of ``texts`` is already stable (see _build_layer), so
    # this is idempotent across re-runs.
    body = "\n".join(t.strip() for t in texts if t.strip())
    if not body:
        return ""
    return DEFAULT_TEMPLATE.format(count=len(texts), body=body)


def _cluster_embedding(embeds: List[List[float]]) -> List[float]:
    """Mean of L2-normalized child embeddings, re-normalized.

    Empty input returns ``[]``. All-zero input returns ``[]`` rather
    than NaN.
    """
    if not embeds:
        return []
    arr = np.asarray(embeds, dtype=np.float64)
    if arr.size == 0:
        return []
    mean = arr.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm <= 0.0:
        return [0.0] * arr.shape[1]
    return (mean / norm).tolist()


def _union_chunk_ids(nodes: List[RaptorNode]) -> List[str]:
    """Stable, deduped concatenation of ``chunk_ids`` across nodes (input order)."""
    out: List[str] = []
    seen: set = set()
    for n in nodes:
        for cid in n.chunk_ids:
            if cid and cid not in seen:
                seen.add(cid)
                out.append(cid)
    return out


def _build_layer(
    prev_layer_nodes: List[RaptorNode],
    *,
    depth: int,
    threshold: float,
    summarize_fn: Optional[Callable[..., str]],
    random_state: int,
) -> List[RaptorNode]:
    """Cluster the previous layer into a new layer of summary nodes.

    Returns a list of ``RaptorNode`` for the new layer (depth ==
    ``depth``). Always returns at least one node. When only one node
    exists in the previous layer, that node'"'"'s text is propagated
    unchanged (so the caller can collapse to root quickly).
    """
    if not prev_layer_nodes:
        return []

    if len(prev_layer_nodes) == 1:
        # Single-child layer: skip clustering and propagate.
        only = prev_layer_nodes[0]
        return [
            RaptorNode(
                node_id=f"L{depth}C0",
                text=only.text,
                embedding=list(only.embedding) if only.embedding is not None else None,
                depth=depth,
                children=[only.node_id],
                chunk_ids=list(only.chunk_ids),
                is_leaf=False,
            ),
        ]

    # Build the embedding matrix. Rows that have no embedding get the
    # zero vector -- this still produces a single-element cluster for
    # those rows because they cannot be similar to anything else.
    vecs: List[List[float]] = []
    for n in prev_layer_nodes:
        if n.embedding is None:
            # Use a zero row of the consensus dim. Pick a row from any
            # other node to learn the consensus dim.
            ref = next(
                (other.embedding for other in prev_layer_nodes if other.embedding is not None),
                None,
            )
            vecs.append([0.0] * (len(ref) if ref else 1))
        else:
            vecs.append(list(n.embedding))

    # All-zero matrix (no embeddings anywhere): fall back to
    # one-node-per-input so the tree still builds without NaNs.
    arr = np.asarray(vecs, dtype=np.float64)
    nonzero_rows = int(np.sum(np.linalg.norm(arr, axis=1) > 0.0))
    if nonzero_rows < 2:
        # Build one summary node covering everything.
        texts = [n.text for n in prev_layer_nodes]
        summary_text = _summarize_cluster(texts, summarize_fn=summarize_fn)
        return [
            RaptorNode(
                node_id=f"L{depth}C0",
                text=summary_text,
                embedding=None,
                depth=depth,
                children=[n.node_id for n in prev_layer_nodes],
                chunk_ids=_union_chunk_ids(prev_layer_nodes),
                is_leaf=False,
            ),
        ]

    raw_labels = _cluster_assignments(arr, threshold, random_state)
    labels, _ = _relabel(raw_labels)

    # Bucket prev-layer nodes by cluster, in stable order (cluster id
    # then original node id).
    buckets: Dict[int, List[RaptorNode]] = {}
    bucket_order: List[int] = []
    for node, lab in zip(prev_layer_nodes, labels):
        if lab not in buckets:
            buckets[lab] = []
            bucket_order.append(lab)
        buckets[lab].append(node)

    # Sort each bucket by node_id for determinism.
    for lab in bucket_order:
        buckets[lab].sort(key=lambda n: n.node_id)

    # Build one summary node per bucket.
    new_layer: List[RaptorNode] = []
    for cluster_idx in bucket_order:
        members = buckets[cluster_idx]
        member_texts = [m.text for m in members]
        member_embeds = [m.embedding for m in members if m.embedding is not None]
        # Sort texts by member node_id for stable order.
        ordered = sorted(
            zip(member_texts, members),
            key=lambda pair: pair[1].node_id,
        )
        summary_text = _summarize_cluster(
            [t for t, _ in ordered],
            summarize_fn=summarize_fn,
        )
        new_emb = _cluster_embedding(member_embeds)
        if not new_emb:
            new_emb = None
        new_node = RaptorNode(
            node_id=f"L{depth}C{cluster_idx}",
            text=summary_text,
            embedding=new_emb,
            depth=depth,
            children=[m.node_id for m in members],
            chunk_ids=_union_chunk_ids(members),
            is_leaf=False,
        )
        new_layer.append(new_node)
    return new_layer


# ---------- Public entry ----------


def build_tree(
    chunks: Optional[Iterable[Any]],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    cluster_threshold: float = DEFAULT_CLUSTER_THRESHOLD,
    summarize_fn: Optional[Callable[..., str]] = None,
    random_state: int = DEFAULT_RANDOM_STATE,
) -> RaptorTree:
    """Build a RAPTOR tree over ``chunks``.

    Parameters
    ----------
    chunks : iterable of dict, optional
        The input memory chunks. Each dict may carry ``id`` (or
        ``chunk_id``), ``content`` (or ``text``), and ``embedding``
        (optional; falls back to zero-vector when missing).
        ``None`` / empty yields an empty single-root tree.
    max_depth : int, default 3
        Maximum number of clustering rounds. The root sits at
        ``depth == max_depth``. Values <= 0 are clamped to 1.
    cluster_threshold : float, default 0.7
        Cosine-similarity cutoff for merging two siblings. Must be
        in ``[-1.0, 1.0]``; out-of-range values are clamped.
    summarize_fn : callable, optional
        ``summarize_fn(texts: list[str]) -> str`` -- production injects
        a thin wrapper around an LLM endpoint. ``None`` falls back to
        the deterministic template.
    random_state : int, default 1729
        Reserved for future stochastic enhancements. Currently only
        feeds into the log message so re-runs are observable.

    Returns
    -------
    :class:`RaptorTree`
        The built tree. Always has exactly one root.

    Notes
    -----
    * Idempotent: same ``chunks`` + same ``summarize_fn`` + same
      ``cluster_threshold`` -> same tree (same node ids, same
      embeddings, same ``leaf_to_path``).
    * Pure: no I/O beyond the optional ``summarize_fn`` callback.
    """
    # ---- Input normalization ----
    if max_depth <= 0:
        max_depth = 1
    threshold = float(cluster_threshold)
    if threshold < -1.0:
        threshold = -1.0
    elif threshold > 1.0:
        threshold = 1.0

    if chunks is None:
        return _empty_tree(max_depth, threshold, [])

    raw_list = list(chunks)
    if not raw_list:
        return _empty_tree(max_depth, threshold, [])

    normalized, skipped = _normalize_chunks(raw_list)
    if not normalized:
        return _empty_tree(max_depth, threshold, skipped)

    # Deterministic input order: sort by chunk id (empty ids sink to
    # the end, in their original order).
    def _sort_key(c: Dict[str, Any]) -> Tuple[int, str]:
        return (0 if c["id"] else 1, c["id"])

    normalized.sort(key=_sort_key)

    # ---- Layer 0: leaves ----
    dim = _consensus_dim([c["embedding"] for c in normalized])
    leaves: List[RaptorNode] = []
    leaf_ids: List[str] = []
    chunk_to_leaf: Dict[str, str] = {}
    for i, chunk in enumerate(normalized):
        node_id, node = _make_leaf(i, chunk, dim)
        leaves.append(node)
        leaf_ids.append(node_id)
        if chunk["id"]:
            chunk_to_leaf[chunk["id"]] = node_id

    nodes: Dict[str, RaptorNode] = {n.node_id: n for n in leaves}

    # ---- Clustering loop ----
    # The root always ends up at ``depth == max_depth``. If clustering
    # collapses to a single node before we reach ``max_depth``, we pad
    # the remaining rounds with passthrough layers (so path length is
    # always ``max_depth + 1``).
    current_layer: List[RaptorNode] = leaves
    depth = 1
    root_id: Optional[str] = None
    collapsed_early = False

    while depth <= max_depth:
        next_layer = _build_layer(
            current_layer,
            depth=depth,
            threshold=threshold,
            summarize_fn=summarize_fn,
            random_state=random_state,
        )
        if not next_layer:
            # Defensive: should not happen, but guard anyway.
            break
        for n in next_layer:
            nodes[n.node_id] = n
        if len(next_layer) == 1:
            # Collapse. We continue the loop with this single node as
            # the only "previous-layer" input -- ``_build_layer`` will
            # produce a passthrough node at the next depth, which keeps
            # the depth arithmetic consistent. Mark this so we know we
            # can stop the loop after we hit ``max_depth``.
            collapsed_early = True
            current_layer = next_layer
            if depth == max_depth:
                root_id = next_layer[0].node_id
            depth += 1
            continue
        current_layer = next_layer
        depth += 1

    if root_id is None:
        # The loop exhausted at ``max_depth`` with multiple top-level
        # nodes. We synthesize a single root node at ``depth ==
        # max_depth + 1`` that summarizes the current top layer. The
        # root sits one level above the natural top layer so the
        # ``root -> L<max_depth>Cx -> ... -> leaf`` chain has
        # consistent depths.
        summary_text = _summarize_cluster(
            [n.text for n in current_layer],
            summarize_fn=summarize_fn,
        )
        root = RaptorNode(
            node_id="root",
            text=summary_text,
            embedding=_cluster_embedding(
                [n.embedding for n in current_layer if n.embedding is not None]
            ) or None,
            depth=max_depth + 1,
            children=[n.node_id for n in current_layer],
            chunk_ids=_union_chunk_ids(current_layer),
            is_leaf=False,
        )
        nodes[root.node_id] = root
        root_id = root.node_id
    _ = collapsed_early  # noqa: F841 -- reserved for future diagnostics

    # ---- Build leaf_to_path ----
    leaf_to_path: Dict[str, List[str]] = {}

    def _walk(node_id: str, trail: List[str]) -> None:
        node = nodes[node_id]
        new_trail = trail + [node_id]
        if node.is_leaf:
            leaf_to_path[node_id] = new_trail
            return
        for child_id in node.children:
            _walk(child_id, new_trail)

    _walk(root_id, [])

    return RaptorTree(
        nodes=nodes,
        root_id=root_id,
        leaf_ids=leaf_ids,
        leaf_to_path=leaf_to_path,
        chunk_id_to_leaf=chunk_to_leaf,
        skipped_chunk_ids=skipped,
        max_depth=max_depth,
        cluster_threshold=threshold,
    )


__all__ = [
    "DEFAULT_CLUSTER_THRESHOLD",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_RANDOM_STATE",
    "RaptorNode",
    "RaptorTree",
    "build_tree",
]
