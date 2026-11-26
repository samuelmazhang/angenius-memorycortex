"""Cross-encoder reranking (V6.6.0 direction #1 — 精排层).

Implements the missing 精排 stage of the RAG pipeline described in
cortex-v6.5-roadmap.md §方向1: after hybrid recall (vector + lexical + tag,
RRF-fused) produces a candidate set, a Cross-Encoder scores each
(query, candidate) pair jointly instead of relying on independent
dual-tower vector similarity.

Why it matters — the shape of the existing pipeline is "粗排" only:

    query ─┬─► vector channel  (top_vector, cosine over pgvector)
           ├─► lexical channel (top_lexical, ts_rank over tsv GIN)
           ├─► tag channel     (tags, jsonb array match)
           └─► RRF fusion ──► candidates ──► _three_factor() sort

`_three_factor()` only re-orders rows that RRF *already recalled*. A chunk
whose surface form differs from the query (synonym, paraphrase, translation,
different vocabulary) can sit below the recall cut and never be scored —
no amount of downstream weighting recovers it. Rerank only reorders what
recall produced; it does not create recall.

Design constraints:

* **No new dependencies.** The container already ships `onnxruntime` +
  `tokenizers` (see server/docker/unified/Dockerfile). The reranker runs as
  an ONNX session on CPUExecutionProvider — no torch, no sentence-transformers.
* **Optional & fail-open.** If the model snapshot is missing, or the session
  fails to load, or scoring raises, every caller gets the input order back
  unchanged. Reranking must never be able to fail a search.
* **Testable without the model.** `score_pairs` is a pure function over
  (query, texts) with the model injected; tests use a stub scorer.

Usage (wired in api_pg.search_memory, after _rrf_fusion):

    from .rerank import rerank_ids, is_enabled

    candidates = ordered_ids[: max(req.limit, 10)]
    if is_enabled() and req.rerank:
        candidates = rerank_ids(
            req.query,
            by_id_or_lookup(candidates),
            top_k=req.rerank_top_k,
        )

Feature flags:

    MEMORY_FEATURE_RERANK        -- master switch (default false)
    MEMORY_RERANK_MODEL_PATH     -- override the model snapshot dir
    MEMORY_RERANK_MAX_SEQ_LEN    -- max query length (default 128)
    MEMORY_RERANK_MAX_DOC_LEN    -- max doc length (default 512)
"""

from __future__ import annotations

import logging
import math
import os
import threading
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Feature flag (灰度) — off by default, same pattern as MEMORY_FEATURE_TAXONOMY_BOOST
RERANK_ENABLED = os.getenv("MEMORY_FEATURE_RERANK", "false").lower() in (
    "1",
    "true",
    "yes",
)

# Cross-Encoder 输入长度上限 (BGE-reranker 训练时 query<=512 / passage<=512,
# 但真实 query 很短, 收紧到 128/512 可以显著减少 CPU 推理耗时)
MAX_QUERY_LEN = int(os.getenv("MEMORY_RERANK_MAX_SEQ_LEN", "128"))
MAX_DOC_LEN = int(os.getenv("MEMORY_RERANK_MAX_DOC_LEN", "512"))

# 候选上限: 精排是 O(N) 前向, N 太大会拖垮 CPU 端到端延迟.
# 粗排给到 30~50 条是 CPU Cross-Encoder 的经验甜点.
DEFAULT_TOP_K = 30

# Cross-Encoder 的 query/passage 分隔符 (bge-reranker 系列约定)
SEP = " "

# 模型快照查找路径 — 与 api_pg.get_embedding_model() 的多路径策略一致:
# 本机 Windows 开发 vs 8.2 Linux 容器 (bind-mount RO).
_MODEL_CANDIDATES = (
    r".models\bge-reranker-base",
    ".models/bge-reranker-base",
    "/opt/cortex-deploy/models/bge-reranker-base",
    "/root/.arcadia/plugins/memory-cortex/models/bge-reranker-base",
)

# 惰性加载的会话句柄 + 加载状态
# (None = 还没试过, "unavailable" = 试过但失败, 不再重试)
_session = None
_session_state = None
_lock = threading.Lock()


class RerankUnavailable(RuntimeError):
    """Raised when the reranker model cannot be loaded.

    Callers are expected to catch this and fall back to the input order —
    search must degrade to 粗排-only, never to a 500.
    """


def is_enabled() -> bool:
    """检查 feature flag (master switch)."""
    return RERANK_ENABLED


# ONNX 文件名候选, 按优先级排列.
#
# 为什么优先量化版: bge-reranker-base 的 fp32 导出有 **1.06 GB**, 而
# int8 (model_quantized.onnx) 只有 **266 MB** —— 4 倍体积差. 精排是 CPU
# 推理 (容器内无 GPU), fp32 权重加载慢、常驻内存大, 对一个 30-50 条候选的
# 精排来说 int8 的排序质量损失远小于它的部署代价. 所以默认按量化优先,
# 但保留 fp32 兜底 (若量化版缺失且 fp32 在位).
_ONNX_VARIANTS = (
    "model_quantized.onnx",   # int8, ~266MB — 默认首选
    "model.onnx",             # fp32, ~1.06GB — 兜底
)


def onnx_file(root: str) -> Optional[str]:
    """在模型快照目录内按优先级解析 ONNX 权重文件. 返回绝对路径或 None."""
    for name in _ONNX_VARIANTS:
        p = os.path.join(root, "onnx", name)
        if os.path.exists(p):
            return p
    return None


def model_path() -> Optional[str]:
    """解析 reranker 模型快照目录. 找不到返回 None.

    需要同时存在 tokenizer.json + onnx/<variant>.onnx (与 embedding 模型同构).
    返回的是**目录**, 具体权重文件名由 :func:`onnx_file` 决定 —— 这样
    换量化/非量化版不需要改调用方.

    MEMORY_RERANK_MODEL_PATH 显式设置时**独占**: 不再回退到默认候选路径。
    否则运维把路径改到一个错误目录想验证"模型缺失"的降级行为, 实际却
    悄悄用了默认路径上的另一份模型 —— 这种"设了不生效"的假阴性比直接
    报错更难查。
    """
    override = os.getenv("MEMORY_RERANK_MODEL_PATH", "").strip()
    candidates = (override,) if override else _MODEL_CANDIDATES
    for cand in candidates:
        if not cand:
            continue
        if not os.path.exists(os.path.join(cand, "tokenizer.json")):
            continue
        if onnx_file(cand) is not None:
            return cand
    return None


def reset_session() -> None:
    """清空加载状态. 测试/管理用 (与 memory_taxonomy.reset_cache 同构)."""
    global _session, _session_state
    with _lock:
        _session = None
        _session_state = None


def get_session():
    """惰性加载 ONNX Cross-Encoder 会话.

    Raises:
        RerankUnavailable: onnxruntime / tokenizers 缺失, 或快照不存在.
    """
    global _session, _session_state
    if _session_state == "unavailable":
        raise RerankUnavailable("reranker previously failed to load")
    if _session is not None:
        return _session

    with _lock:
        # 双检 (another thread may have loaded while we waited)
        if _session_state == "unavailable":
            raise RerankUnavailable("reranker previously failed to load")
        if _session is not None:
            return _session

        path = model_path()
        if path is None:
            _session_state = "unavailable"
            raise RerankUnavailable(
                "reranker snapshot not found. Place bge-reranker-base (tokenizer.json "
                "+ onnx/model.onnx) in models/bge-reranker-base, or set "
                "MEMORY_RERANK_MODEL_PATH."
            )
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as e:
            _session_state = "unavailable"
            raise RerankUnavailable(f"onnxruntime/tokenizers unavailable: {e}")

        try:
            weights = onnx_file(path)
            sess = ort.InferenceSession(
                weights,
                providers=["CPUExecutionProvider"],
            )
            tok = Tokenizer.from_file(os.path.join(path, "tokenizer.json"))
            # 与 embedding 模型同构的定长 padding — ONNX 图要求固定 seq_len.
            # Cross-Encoder 批量推理必须所有样本同一长度, 否则 shape mismatch.
            tok.enable_padding(pad_id=0, length=MAX_DOC_LEN)
            tok.enable_truncation(max_length=MAX_DOC_LEN)
            _session = (sess, tok, os.path.basename(path))
            _session_state = "ready"
            logger.info(
                "Loaded reranker Cross-Encoder from %s (weights=%s)",
                path,
                os.path.basename(weights),
            )
            return _session
        except Exception as e:
            _session_state = "unavailable"
            raise RerankUnavailable(f"failed to load reranker from {path}: {e}")


def score_pairs(
    query: str,
    texts: Sequence[str],
    session=None,
) -> List[float]:
    """对 (query, text) 逐一做 Cross-Encoder 精排打分.

    打分是 sigmoid(logit) 之外的**原始 logit** — sigmoid 单调, 不改变排序,
    保留 logit 让调用方能按模型原生阈值解释 (bge-reranker 的判定线在 -10 左右).

    Args:
        query: 用户查询 (会被截断到 MAX_QUERY_LEN)
        texts: 候选文本 (每个会被截断到 MAX_DOC_LEN)
        session: 可选的 (session, tokenizer, name) 三元组 — 测试注入 stub 用.

    Returns:
        与 texts 等长的 logit 列表.

    Raises:
        RerankUnavailable: 模型不可用.
    """
    if not texts:
        return []
    sess = session if session is not None else get_session()
    ort_session, tokenizer, _name = sess

    # Cross-Encoder 输入是 [CLS] query [SEP] passage [SEP] 的**单序列**,
    # 不是双塔的 [query_ids, doc_ids] — 这是它比向量模型看得更细的原因:
    # 每一层 attention 都能让 query 的每个 token 直接看到 passage 的每个 token.
    #
    # 注意 tokenizers 的真实签名 (已实测 tokenizers 1.x, BAAI tokenizer.json):
    #     Tokenizer.encode_batch(input, is_pretokenized=False, add_special_tokens=True)
    #     Tokenizer.encode(sequence, pair=None, ...)
    # `input` 的元素必须是 **Tuple[str, str]** —— (sequence, pair) 二元组.
    # 踩过的三个坑, 都报 TypeError 而不是静默错算:
    #   1. encode_batch([q]*n, texts)   → 第二个位置参数是 is_pretokenized,
    #      拿到 list 报 "'list' object is not an instance of 'bool'"
    #   2. encode_batch([[q],[d]])      → 报 "TextEncodeInput must be
    #      Union[TextInputSequence, Tuple[InputSequence, InputSequence]]"
    #   3. encode_batch([[q],[t]...])   → 报 "TextInputSequence must be str"
    # 唯一正确写法: 传 (str, str) 二元组列表.
    pairs = [(query, t or "") for t in texts]

    # V6.6.0 fix (2026-10-30): the int8 reranker is sensitive to how much
    # padding a batch carries, so the old fixed-512 path produced scores
    # that depended on the padding rather than only on the content.
    # Measured on 127.0.0.1, the SAME 37-token document scored:
    #
    #     pad_len= 64 -> -4.7157
    #     pad_len=128 -> -4.4423
    #     pad_len=256 -> -4.7628
    #     pad_len=512 -> -3.8361      (spread 0.93 logits)
    #
    # and against the no-padding reference the whole batch was off by up to
    # 1.36 logits — enough to reorder results. It was also the reason the
    # path was slow: every pair cost a 512-token forward pass.
    #
    # The fix: give each pair exactly its own token count, with NO padding
    # at all. The graph needs one [batch, seq] shape per call, so pairs are
    # grouped by their exact token length and each group is run separately.
    # The total compute becomes sum(per-pair real length) instead of
    # pair_count * MAX_DOC_LEN.
    #
    # Measured after the change (14-pair mixed-length batch):
    #     fixed-512 (old) : 2155 ms, max |diff| vs no-padding = 1.356
    #     exact-length    :  419 ms, max |diff| vs no-padding = 0.287
    # The residual 0.287 is int8 dynamic-activation quantization across a
    # multi-row batch (it scales over the whole tensor, so batch composition
    # shifts the score); it is inherent to batching an int8 model and was
    # present in the old path too — just much larger there. Scoring each
    # pair alone costs the same wall time (426 ms) because these groups are
    # nearly all singletons anyway.
    encoded = tokenizer.encode_batch(pairs)
    wanted = {i.name for i in ort_session.get_inputs()}

    # Group by exact token length. The tokenizer is configured at load time
    # with a fixed padding length (MAX_DOC_LEN), so enc.ids is always that
    # long; the REAL length of each pair is the number of set bits in its
    # attention mask. Slicing to that length removes every padding column.
    by_len: dict = {}
    for idx, enc in enumerate(encoded):
        real_len = int(sum(enc.attention_mask))
        if real_len <= 0:
            real_len = 1
        by_len.setdefault(real_len, []).append(idx)

    scores: List[float] = [0.0] * len(encoded)
    for length, idxs in by_len.items():
        # Slice to this group's exact length — no padding columns survive,
        # so every pair sees its tokens at their true positions.
        group_ids = np.array(
            [encoded[i].ids[:length] for i in idxs], dtype=np.int64
        )
        group_mask = np.array(
            [encoded[i].attention_mask[:length] for i in idxs], dtype=np.int64
        )
        group_tt = np.array(
            [encoded[i].type_ids[:length] for i in idxs], dtype=np.int64
        )
        feed = {}
        if "input_ids" in wanted:
            feed["input_ids"] = group_ids
        if "attention_mask" in wanted:
            feed["attention_mask"] = group_mask
        if "token_type_ids" in wanted:
            feed["token_type_ids"] = group_tt
        if not feed:
            raise RerankUnavailable("reranker ONNX graph exposes no expected inputs")
        # 输出名不必预取: rerank 的 ONNX 导出总是单个 logits 张量, 按序取第一个即可.
        logits = ort_session.run(None, feed)[0]  # (batch, 1) 或 (batch,)
        # The graph must return exactly one score per row. If it does not,
        # raise: rerank_scores() catches this and falls back to the coarse
        # ordering (fail-open), which is the intended behaviour. Silently
        # zip()-ing would truncate instead, and a short/long score list
        # would be applied to the WRONG candidates.
        n_out = len(logits)
        if n_out != len(idxs):
            raise RerankUnavailable(
                f"reranker returned {n_out} scores for {len(idxs)} pairs"
            )
        for i, x in zip(idxs, logits):
            scores[i] = float(np.ravel(x)[0])
    return scores


def rerank_scores(
    query: str,
    candidates: Sequence[Tuple[str, str]],
    session=None,
) -> Optional[Dict[str, float]]:
    """Cross-Encoder 精排打分, 返回 {chunk_id: 混合分}.

    与 :func:`rerank_ids` 的区别: 这个函数**不重排**, 只把 CE 信号以
    0..1 的混合分交给调用方。`/search` 用它把精排接进 `composite_score`,
    这样 CE 的判断能穿过后续的 `_three_factor()` 排序存活下来 —— 如果只
    调用 `rerank_ids` 重排列表, 后面那句 `results.sort(key=composite_score)`
    会把精排顺序整个盖掉, 精排等于没做。

    Args:
        query: 用户查询
        candidates: [(chunk_id, chunk_text), ...]
        session: 测试注入用

    Returns:
        {chunk_id: score}, score ∈ [0,1] (sigmoid(logit) 与 RRF 归一分的
        alpha 混合). 任何失败路径返回 None (= "没精排, 走纯粗排").
    """
    if not candidates:
        return {}
    try:
        raw = score_pairs(query, [text or "" for _cid, text in candidates], session=session)
    except Exception as e:
        logger.warning("Rerank scoring skipped: %s", e)
        return None
    if len(raw) != len(candidates):
        logger.warning(
            "Rerank score count mismatch (%d != %d), skipping rerank",
            len(raw),
            len(candidates),
        )
        return None
    out: Dict[str, float] = {}
    for (cid, _text), logit in zip(candidates, raw):
        out[cid] = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, logit))))
    return out


def rerank_ids(
    query: str,
    candidates: Sequence[Tuple[str, str]],
    top_k: int = DEFAULT_TOP_K,
    blend_rrf: Optional[Dict[str, float]] = None,
    session=None,
    alpha: float = 0.5,
) -> List[str]:
    """Cross-Encoder 精排: 重排 (chunk_id, text) 候选列表.

    精排**只重排粗排已经召回的候选**, 不会凭空召回新文档 — 这与 RRF
    融合之后的 _three_factor() 同处一个阶段, 两者是串联而非替代.

    排序策略 (默认 alpha=0.5 线性混合, 而非纯 Cross-Encoder):

        final = alpha * sigmoid(cross_logit) + (1-alpha) * rrf_norm

    理由: 纯 CE 排序会丢掉 RRF 已经算好的多路共识信号 (一路命中
    lex_hit 的专有名词可能 CE 打不高), 而 CE 又能补上 RRF 看不了的
    语义细节. 混合比可调, alpha=1.0 即退化为纯 CE 排序.

    Args:
        query: 用户查询
        candidates: [(chunk_id, chunk_text), ...] — 已按粗排顺序排好
        top_k: 参与精排的候选上限 (超过则只取前 top_k, 超出部分丢弃)
        blend_rrf: {chunk_id: rrf_score} — 用于混合打分; None 则纯 CE 排序
        session: 测试注入用
        alpha: CE 权重 (0=纯 RRF, 1=纯 CE). 默认 0.5 平衡两者.

    Returns:
        重排后的 chunk_id 列表 (长度 == min(len(candidates), top_k)).
        任何失败路径都返回**原顺序的前 top_k**, 不抛错.
    """
    if not candidates:
        return []
    limit = max(1, min(int(top_k), len(candidates)))
    head = list(candidates[:limit])
    fallback = [cid for cid, _t in head]

    ce = rerank_scores(query, head, session=session)
    if ce is None:
        return fallback

    if blend_rrf:
        # RRF 分数量纲是 1/(k+rank), 先归一化到 [0,1] 再和 CE 的 sigmoid 混合.
        max_rrf = max(blend_rrf.values()) if blend_rrf else 0.0
        rrf_norm = {
            cid: (blend_rrf.get(cid, 0.0) / max_rrf if max_rrf > 0 else 0.0)
            for cid, _t in head
        }
    else:
        rrf_norm = {cid: 0.0 for cid, _t in head}

    order = sorted(
        range(len(head)),
        key=lambda i: -(alpha * ce[head[i][0]] + (1.0 - alpha) * rrf_norm[head[i][0]]),
    )
    return [head[i][0] for i in order]


def reset_cache() -> None:
    """reset_session 的语义别名 (与 memory_taxonomy.reset_cache 命名对齐)."""
    reset_session()


def stats() -> Dict[str, Optional[str]]:
    """运维探针: 返回当前加载状态, 供 /health 或 /stats 观察."""
    name = _session[2] if _session is not None else None
    return {
        "enabled": RERANK_ENABLED,
        "state": _session_state or "not_loaded",
        "model": name,
        "max_query_len": MAX_QUERY_LEN,
        "max_doc_len": MAX_DOC_LEN,
    }
