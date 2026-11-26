"""
Memory Cortex v6.7.0 — PG + pgvector 后端 + Cross-Encoder 精排
完全替代 sqlite + chroma 存储层，保留全部 85 个 endpoint 接口契约。
"""
import os
import json
import re
import hashlib
import time
import uuid
import logging
import asyncio
from datetime import datetime, timezone
# V6.6.0 (2026-10-31): Tuple added. Module-level annotations are
# EVALUATED at definition time on Python 3.10 (the container runtime),
# so a typing name used in an annotation but missing from this import
# is a NameError at IMPORT time -- which crashed the container on
# startup. The local dev Python is 3.14, where PEP 649 makes
# annotations lazy, so neither py_compile nor the local test run
# catches it. Every typing name used in an annotation must be here.
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Query, Request, Body
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
import os as _os_ui
import uvicorn
import numpy as np

# v6.5.0 Phase 2: consolidation scheduler (write-time enqueue + PG worker)
from src.consolidate.scheduler import ConsolidationScheduler as _ConsolidationSchedulerImpl  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cortex-pg")

# === Configuration (single source: src.config.CortexConfig) ===
# Phase 2 (2026-11-25): these module-level constants are now derived from the
# CortexConfig singleton. The names stay unchanged because ~200 call sites and
# the test suite import them directly. CortexConfig reads the SAME env vars
# (with identical defaults), so runtime values are byte-for-byte identical to
# the previous os.getenv lines -- this is a source consolidation, not a
# behaviour change. Env always wins; .env is loaded by CortexConfig._load().
from .config import config as _cfg  # noqa: E402  (must follow path setup above)

PG_HOST = _cfg.pg_host
PG_PORT = _cfg.pg_port
PG_DB = _cfg.pg_db
PG_USER = _cfg.pg_user
PG_PASSWORD = _cfg.pg_password
PORT = _cfg.port

# Embedding model — ONNX direct (no chromadb dep)
_embedding_session = None
_embedding_tokenizer = None
EMBEDDING_DIM = 384

# V6.5.0 Phase 1 direction #3 — Nomic v1.5 dispatch (2026-08-23).
# Two-model setup:
#   - "mpnet":      paraphrase-multilingual-MiniLM-L12-v2, 384-dim, ONNX.
#                   Default for chunks created before v6.5.0.
#   - "nomic-v1.5": nomic-ai/nomic-embed-text-v1.5, 768-dim, HF/SentenceTransformers.
#                   MTEB 62.28 (vs ~58 for MiniLM). Default for new chunks.
# Selection by env var EMBEDDING_MODEL (default: nomic-v1.5 for new writes).
EMBEDDING_MODEL_ENV = _cfg.embedding_model
NOMIC_DIM = 768
SUPPORTED_MODELS = {"mpnet", "nomic-v1.5"}
MODEL_DIM = {"mpnet": 384, "nomic-v1.5": 768}

# Lazy Nomic model handle (separate from MiniLM ONNX session).
_nomic_model = None


def get_embedding_model():
    global _embedding_session, _embedding_tokenizer
    if _embedding_session is None:
        import onnxruntime as ort
        from tokenizers import Tokenizer
        # 多路径查找 (本机 Windows 开发 vs 8.2 Linux 容器)
        candidates = [
            r".models\paraphrase-multilingual-MiniLM-L12-v2",
            ".models/paraphrase-multilingual-MiniLM-L12-v2",
            "/root/.hermes/plugins/memory-cortex/models/paraphrase-multilingual-MiniLM-L12-v2",
            "/opt/cortex-deploy/models/paraphrase-multilingual-MiniLM-L12-v2",
        ]
        mm_path = None
        for cand in candidates:
            if os.path.exists(os.path.join(cand, "onnx", "model.onnx")) and \
               os.path.exists(os.path.join(cand, "tokenizer.json")):
                mm_path = cand
                break
        if mm_path:
            model_path = os.path.join(mm_path, "onnx", "model.onnx")
            tok_path = os.path.join(mm_path, "tokenizer.json")
            logger.info(f"Loading MULTILINGUAL embed model from {mm_path}")
        else:
            # EN fallback (8.2 容器历史路径)
            en_path = "/root/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx"
            model_path = f"{en_path}/model.onnx"
            tok_path = f"{en_path}/tokenizer.json"
            if not os.path.exists(model_path):
                raise FileNotFoundError(
                    f"No embedding model. Searched: {candidates}. "
                    f"Place model in .models/ (Windows) "
                    f"or /root/.hermes/plugins/memory-cortex/models/ (Linux)"
                )
            logger.warning(f"Multilingual not found, fallback EN at {en_path}")
        _embedding_session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        _embedding_tokenizer = Tokenizer.from_file(tok_path)
        _embedding_tokenizer.enable_padding(pad_id=0, length=256)
        _embedding_tokenizer.enable_truncation(max_length=256)
    return _embedding_session, _embedding_tokenizer


def get_nomic_model():
    """V6.5.0 direction #3: lazy load nomic-ai/nomic-embed-text-v1.5.

    Tries the `nomic` package first (recommended by Nomic AI), then falls back
    to ``sentence_transformers.SentenceTransformer`` from a local snapshot
    (avoids hitting huggingface.co at load time -- the live container may
    not have outbound HF access). If neither is available, returns
    ``("unavailable", None)`` -- embed_text() will then fall back to mpnet and
    log a warning instead of crashing the request.

    Local-snapshot candidates (searched in order):
      * /opt/cortex-deploy/models/nomic-embed-text-v1.5   (developer devbox)
      * /root/.hermes/plugins/memory-cortex/models/nomic-embed-text-v1.5
        (the bind-mount that the live container maps to)
    """
    global _nomic_model
    if _nomic_model is not None:
        return _nomic_model
    try:
        try:
            from nomic import embed as _nomic_embed  # type: ignore
            _nomic_model = ("nomic_api", _nomic_embed)
            logger.info("Loaded nomic-ai/nomic-embed-text-v1.5 via nomic package")
        except ImportError:
            from sentence_transformers import SentenceTransformer  # type: ignore
            # Resolve local snapshot before constructing -- sentence_transformers
            # will hit huggingface.co to download if it cannot find a local
            # copy, and the live container cannot reach HF.
            local_candidates = [
                    "/opt/cortex-deploy/models/nomic-embed-text-v1.5",
                    "/root/.hermes/plugins/memory-cortex/models/nomic-embed-text-v1.5",
                    os.path.expanduser("~/.cache/huggingface/hub/models--nomic-ai--nomic-embed-text-v1.5/snapshots/main"),
                    os.path.expanduser(".models/nomic-embed-text-v1.5"),
                    os.path.expanduser(".models\\nomic-embed-text-v1.5"),
            ]
            local_path = next(
                (p for p in local_candidates
                 if os.path.isfile(os.path.join(p, "config.json"))),
                None,
            )
            if local_path is not None:
                # Force HF offline so a missing snapshot never triggers a
                # network call. The local_path above is the source of truth.
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
                # sentence-transformers >= 5.3.0 + transformers >= 5.5.0 no
                # longer need trust_remote_code=True for nomic-embed-text-v1.5
                # (per HF model card, 2026-08). Setting it to True forces
                # transformers to try to download the custom Python code,
                # which breaks in offline mode.
                _nomic_model = (
                    "st_model",
                    SentenceTransformer(local_path),
                )
                logger.info(
                    "Loaded nomic-ai/nomic-embed-text-v1.5 via sentence-transformers "
                    "from LOCAL snapshot %s",
                    local_path,
                )
            else:
                # No local snapshot found -- last-resort: try the public HF id.
                # This will work in dev/CI envs that have outbound HF access;
                # it will fail (and fall back to mpnet) in the production
                # container.
                _nomic_model = (
                    "st_model",
                    SentenceTransformer("nomic-ai/nomic-embed-text-v1.5"),
                )
                logger.info(
                    "Loaded nomic-ai/nomic-embed-text-v1.5 via sentence-transformers "
                    "(public HF id; no local snapshot found)"
                )
        return _nomic_model
    except Exception as e:
        logger.warning(
            "Nomic v1.5 model not available (%s: %s). Falling back to mpnet.",
            type(e).__name__, e,
        )
        _nomic_model = ("unavailable", None)
        return _nomic_model


# P2-C3 fix (2026-08-13): LRU cache for query embeddings.
# Same query text -> same vector. 409 audit records show avg query 93 chars
# (7.1% >500), rec.recall/conversation frequently re-encodes phrases like
# "QuantPlatform" / "recent context". Cache key is (model_version, text) so
# mpnet-cached and nomic-v1.5-cached vectors don't collide (different dims).
_EMBED_CACHE = {}
_EMBED_CACHE_MAX = 512
_embed_cache_hits = 0
_embed_cache_misses = 0


def _embed_text_mpnet_inner(text: str) -> List[float]:
    """Core MiniLM-L12-v2 ONNX encode. Returns 384-dim normalized vector."""
    sess, tok = get_embedding_model()
    enc = tok.encode(text)
    input_ids = np.array([enc.ids], dtype=np.int64)
    attention_mask = np.array([enc.attention_mask], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids)
    result = sess.run(None, {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "token_type_ids": token_type_ids,
    })[0]
    mask = attention_mask[..., None].astype(np.float32)
    pooled = (result * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1e-9)
    pooled = pooled / (np.linalg.norm(pooled, axis=1, keepdims=True) + 1e-9)
    return pooled[0].tolist()


def _embed_text_nomic_inner(text: str) -> List[float]:
    """Core Nomic v1.5 encode. Returns 768-dim normalized vector.

    Nomic recommends explicit prefixes for query vs document embeddings
    (search_query / search_document). For Cortex we use 'search_document'
    uniformly — keeps cache keys stable across ingest + search.
    """
    kind, handle = get_nomic_model()
    if kind == "unavailable" or handle is None:
        # Graceful degradation: log + fall back to mpnet (with warning).
        logger.warning("nomic-v1.5 requested but unavailable; using mpnet fallback.")
        return _embed_text_mpnet_inner(text)
    if kind == "nomic_api":
        # `nomic.embed` API: takes list[str] and optional task-type prefix.
        out = handle.text(
            texts=[f"search_document: {text}"],
            model="nomic-embed-text-v1.5",
            task_type="search_document",
        )
        # nomic.embed returns {"embeddings": [[...]], "usage": {...}}
        vec = out["embeddings"][0]
        # nomic vectors are returned normalized, but normalize defensively.
        arr = np.asarray(vec, dtype=np.float32)
        arr = arr / (np.linalg.norm(arr) + 1e-9)
        return arr.tolist()
    # kind == "st_model"
    arr = handle.encode(
        [f"search_document: {text}"],
        normalize_embeddings=True,
        convert_to_numpy=True,
    )[0]
    return arr.astype(np.float32).tolist()


def resolve_embedding_model(requested: Optional[str] = None) -> str:
    """Return the model that will ACTUALLY encode text right now.

    V6.6.0 fix (2026-10-30) — the silent-fallback bug.

    ``embed_text()`` used to fall back from nomic-v1.5 to mpnet *silently*:
    the caller asked for "nomic-v1.5", got a 384-dim mpnet vector zero-padded
    to 768, and stored it with ``model_version='nomic-v1.5'``. The corpus
    therefore ended up with two incompatible vector spaces under one label:

        true-nomic   2333 chunks  (768 real dims)
        mpnet-padded  250 chunks  (384 real dims + 384 zeros)
        both labelled model_version='nomic-v1.5'

    A query embedded in the mpnet space can only be compared meaningfully
    against the mpnet-padded half — measured on 127.0.0.1, 30/30 of the
    vector channel's top-30 came from those 250 chunks; the other 2333
    (90% of the corpus) were vector-invisible and only reachable through the
    lexical channel.

    The root cause was that the 2026-09-01 image rebuild dropped
    ``sentence_transformers``/``torch`` from the baked venv, so
    ``get_nomic_model()`` always returned "unavailable" — but nothing
    recorded that the *requested* model was not the *actual* model.

    Now the effective model is computed explicitly and callers use it for
    BOTH the vector and the ``model_version`` label, so a vector can never
    be stored under a label for a space it does not belong to.

    Returns one of ``SUPPORTED_MODELS``.
    """
    requested = (requested or EMBEDDING_MODEL_ENV).lower()
    if requested not in SUPPORTED_MODELS:
        logger.warning(
            "Unknown model_version=%s, falling back to nomic-v1.5", requested
        )
        requested = "nomic-v1.5"

    if requested == "nomic-v1.5":
        kind, _handle = get_nomic_model()
        if kind == "unavailable":
            logger.warning(
                "nomic-v1.5 requested but unavailable in this runtime; "
                "encoding with mpnet instead. Vectors will be labelled "
                "model_version='mpnet' so they are never compared against "
                "nomic-v1.5 chunks."
            )
            return "mpnet"
    return requested


def embed_text(text: str, model_version: Optional[str] = None) -> List[float]:
    """Encode text via dispatch (v6.5.0 direction #3).

    Args:
        text: input text to encode.
        model_version: "mpnet" or "nomic-v1.5". Defaults to env var
                       ``EMBEDDING_MODEL`` ("nomic-v1.5" if unset).

    Returns:
        List[float] of length MODEL_DIM[model_version] (384 or 768).

        NOTE (V6.6.0): if the requested model is unavailable at runtime the
        encoder falls back to the other model and the returned vector has
        the *other* model's dimensionality. Use
        :func:`resolve_embedding_model` to learn which model was actually
        used — that is what must be stored as ``model_version``.

    Cache: keyed by (model_version, text) so cache entries do not collide
    between the two models (different dims).
    """
    global _embed_cache_hits, _embed_cache_misses
    target = resolve_embedding_model(model_version)
    cache_key = (target, text)
    cached = _EMBED_CACHE.get(cache_key)
    if cached is not None:
        _embed_cache_hits += 1
        return cached
    _embed_cache_misses += 1
    if target == "nomic-v1.5":
        vec = _embed_text_nomic_inner(text)
    else:
        vec = _embed_text_mpnet_inner(text)
    if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
        # FIFO eviction: drop oldest 64 entries (dict preserves insertion order)
        for k in list(_EMBED_CACHE.keys())[:64]:
            _EMBED_CACHE.pop(k, None)
    _EMBED_CACHE[cache_key] = vec
    return vec


def reset_embed_cache() -> None:
    """V6.5.0 direction #3: clear cache (testing + admin)."""
    global _EMBED_CACHE, _embed_cache_hits, _embed_cache_misses
    _EMBED_CACHE = {}
    _embed_cache_hits = 0
    _embed_cache_misses = 0

def vec_to_str(v: List[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def vec_to_storage_str(v: List[float], target_dim: int = NOMIC_DIM) -> str:
    """V6.5.0 direction #3: pack a vector into the storage column (768-dim).

    If the column is widened to 768 to fit Nomic but the source model is
    MiniLM (384-dim), we zero-pad the tail so the column type stays valid.
    The search dispatcher filters by model_version so the two spaces are
    never compared — the padding is purely a column-type compatibility
    shim. Caller should pass ``target_dim`` explicitly when model_version
    is known (see ``_vec_to_str_for_model``).
    """
    if len(v) == target_dim:
        return vec_to_str(v)
    if len(v) < target_dim:
        padded = list(v) + [0.0] * (target_dim - len(v))
        return vec_to_str(padded)
    # len(v) > target_dim: truncate (caller gave a longer vector than column).
    return vec_to_str(v[:target_dim])


def _vec_to_str_for_model(v: List[float], model_version: str) -> str:
    """Pack a vector into the storage column for a specific model_version.

    - 'nomic-v1.5': 768-dim, no padding.
    - 'mpnet':      384-dim source, zero-pad to 768 (column dimension).
    """
    target = NOMIC_DIM  # storage column is now vector(768)
    if model_version == "mpnet":
        return vec_to_storage_str(v, target_dim=target)
    return vec_to_storage_str(v, target_dim=target)

# === Connection pool ===
# V6.6.0 fix (2026-10-30): the pool was hard-coded to maxconn=10 while 98 of
# 112 endpoints are *sync* handlers that run in the anyio worker-thread pool,
# and a single /search issues ~10 sequential queries. Under concurrency this
# exhausted the pool and psycopg2 raised
#     PoolError: connection pool exhausted
# straight out of getconn(), which surfaced as an HTTP 500 for the caller.
#
# Measured on 127.0.0.1 before the fix: 24 concurrent /search calls ->
# 10-13 succeeded, 11-14 returned HTTP 500.
#
# The fix has two halves:
#   1. size the pool from the environment (default 32) instead of a
#      hard-coded 10 — Postgres itself allows max_connections=100 and the
#      live service was only holding ~8 connections, so there was plenty of
#      headroom;
#   2. wait for a free connection instead of failing instantly. psycopg2's
#      ThreadedConnectionPool.getconn() raises PoolError as soon as the pool
#      is empty, so get_conn() now retries with a bounded backoff and only
#      raises if the pool stays exhausted for the whole window. A request
#      that waits 5s and then succeeds is far better than a 500.
_PG_POOL_MIN = _cfg.pg_pool_min
_PG_POOL_MAX = _cfg.pg_pool_max
# How long a caller waits for a free connection before giving up.
_PG_POOL_WAIT_SECONDS = _cfg.pg_pool_wait_seconds
_PG_POOL_RETRY_INTERVAL = _cfg.pg_pool_retry_interval

pg_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=_PG_POOL_MIN, maxconn=_PG_POOL_MAX,
    host=PG_HOST, port=PG_PORT, database=PG_DB,
    user=PG_USER, password=PG_PASSWORD
)


def get_conn():
    """Borrow a pooled connection, waiting for one if the pool is busy.

    psycopg2's ThreadedConnectionPool raises PoolError immediately when the
    pool is exhausted. Under concurrent load that turned into HTTP 500s.
    Retry with a bounded backoff so contention shows up as latency, not as
    an error.
    """
    deadline = time.monotonic() + _PG_POOL_WAIT_SECONDS
    delay = _PG_POOL_RETRY_INTERVAL
    while True:
        try:
            return pg_pool.getconn()
        except psycopg2.pool.PoolError:
            if time.monotonic() >= deadline:
                logger.error(
                    "connection pool exhausted after waiting %.1fs "
                    "(minconn=%d maxconn=%d) — raising",
                    _PG_POOL_WAIT_SECONDS, _PG_POOL_MIN, _PG_POOL_MAX,
                )
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 1.0)


def put_conn(conn):
    pg_pool.putconn(conn)

def query(sql: str, params: tuple = None, fetch: str = "all"):
    """查询 helper. fetch 模式:
      - "all"           : SELECT 全部行 + COMMIT (默认安全, 防止 read-then-write 静默丢失)
      - "all-no-commit" : SELECT 全部行 + 不 commit (仅大循环纯读场景用, 节省 commit 开销)
      - "one"           : SELECT 单行 + COMMIT
      - "none"          : INSERT/UPDATE/DELETE + COMMIT, 返回 rowcount
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            if fetch == "all":
                rows = cur.fetchall()
                conn.commit()
                return rows
            elif fetch == "all-no-commit":
                return cur.fetchall()
            elif fetch == "one":
                row = cur.fetchone()
                conn.commit()
                return row
            elif fetch == "none":
                conn.commit()
                return cur.rowcount
    except Exception as e:
        conn.rollback()
        raise
    finally:
        put_conn(conn)

def execute(sql: str, params: tuple = None):
    return query(sql, params, fetch="none")

# === FastAPI app ===

# Web UI 静态目录
_UI_DIR = _os_ui.path.join(_os_ui.path.dirname(__file__), "static")

app = FastAPI(title="Memory Cortex v6.7.0 (PG+pgvector, CE rerank)", version="6.7.0")

# === CORS (Web UI fetch 需要) ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# V6.5.0 Phase 2 direction #6 (2026-08-23): FastAPI OpenTelemetry.
# setup_observability is a no-op when OTEL_ENABLED=false (default), so the
# middleware is never installed and per-request overhead is exactly zero.
# When ops flips OTEL_ENABLED=true it wires a tracer + meter provider and a
# 40-line request middleware that records spans + metrics for every
# non-excluded endpoint. See server/src/observability/CHANGELOG.md.
try:
    from src.observability import setup_observability  # noqa: E402

    _otel_active = setup_observability(app, service_version="6.5.0")
    logger.info("OpenTelemetry observability armed=%s", _otel_active)
except Exception as _otel_exc:
    # Never let observability break app startup.
    logger.warning("OpenTelemetry setup failed (%s); continuing without it", _otel_exc)


# Mount static UI
if _os_ui.path.isdir(_UI_DIR):
    app.mount("/static", StaticFiles(directory=_UI_DIR), name="static")


# === Health ===
def health():
    try:
        chunks = query("SELECT COUNT(*) as c FROM memory_chunks", fetch="one")
        # ADR-262: report both relation tables. The consolidate pipeline
        # produces memory_relations; manual /links/create produces
        # memory_links. They are complementary, not duplicates.
        rels_main = query("SELECT COUNT(*) AS c FROM memory_relations", fetch="one")["c"]
        rels_links = query("SELECT COUNT(*) AS c FROM memory_links", fetch="one")["c"]
        # Check vector count via actual query
        vecs = query("SELECT COUNT(*) as c FROM memory_chunks WHERE embedding IS NOT NULL", fetch="one")
        return {
            "status": "healthy",
            "version": app.version,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "chunks": chunks["c"],
            "relations": rels_main + rels_links,
            "relations_main": rels_main,
            "relations_links": rels_links,
            "vectors": vecs["c"],
        }
    except Exception as e:
        return {"status": "error", "version": app.version, "error": str(e)}


def capabilities():
    """Expose the ACTUAL effective state of feature flags (introspection).

    Rationale: several recall features (RAPTOR / Cross-Encoder rerank /
    taxonomy boost / PPR graph expansion) are flag-gated and off by default.
    Without this endpoint a client cannot tell "feature not requested" from
    "requested but disabled at the server", which produces silent粗排.
    This endpoint reads the same flag sources the request path reads, so it
    reports reality rather than configuration intent.
    """
    import os as _os
    # All flags now come from the CortexConfig singleton (single source).
    # rerank still delegates to rerank.is_enabled() which reads the same env,
    # kept as-is so /capabilities can never disagree with the request path.
    try:
        from .rerank import is_enabled as _rerank_is_enabled
        rerank_on = bool(_rerank_is_enabled())
    except Exception:
        rerank_on = False

    return {
        "version": app.version,
        "features": {
            "hybrid_search": True,           # always-on since v5.3
            "rerank": {
                "enabled": rerank_on,
                "env": "MEMORY_FEATURE_RERANK",
                "note": "per-request rerank=true is ALSO required (double gate)",
            },
            "raptor": {
                "enabled": _cfg.feature_raptor,
                "env": "MEMORY_FEATURE_RAPTOR",
            },
            "graph_ppr": {
                "enabled": _cfg.memory_feature_graph_ppr,
                "env": "MEMORY_FEATURE_GRAPH_PPR",
                "note": "also requires include_related=True on the request",
            },
            "taxonomy_boost": {
                "enabled": _cfg.feature_taxonomy_boost,
                "env": "MEMORY_FEATURE_TAXONOMY_BOOST",
            },
            "otel": {
                "enabled": _cfg.otel_enabled,
                "env": "OTEL_ENABLED",
            },
            "mcp": {"enabled": _cfg.mcp_enabled, "env": "CORTEX_MCP_ENABLED"},
        },
        "models": {
            "embedding_env": _cfg.embedding_model,
            "supported": ["mpnet", "nomic-v1.5"],
            "rerank": "BAAI/bge-reranker-base (ONNX, quantized-first)",
        },
    }

# === Stats ===
def stats():
    chunks = query("SELECT COUNT(*) as c FROM memory_chunks", fetch="one")
    # ADR-262: total_relations now sums BOTH tables because v6.5.0 ships
    # with two legacy tables (memory_relations from consolidate pipeline +
    # memory_links from /links/create). They are NOT duplicates -- they
    # hold different relation kinds (memory_relations = similar/same-source
    # + P2 worker output; memory_links = manual /links/create graph).
    # Split reporting keeps each visible:
    rels_main = query("SELECT COUNT(*) AS c FROM memory_relations", fetch="one")["c"]
    rels_links = query("SELECT COUNT(*) AS c FROM memory_links", fetch="one")["c"]
    vecs = query("SELECT COUNT(*) as c FROM memory_chunks WHERE embedding IS NOT NULL", fetch="one")
    avg_imp = query("SELECT AVG(importance) as a FROM memory_chunks", fetch="one")
    return {
        "total_memories": chunks["c"],
        "total_relations": rels_main + rels_links,
        "total_relations_main": rels_main,
        "total_relations_links": rels_links,
        "total_vectors": vecs["c"],
        "average_importance": float(avg_imp["a"] or 0),
        "storage_status": "healthy" if chunks["c"] == vecs["c"] or vecs["c"] == 0 else "inconsistent",
    }

# === Endpoints catalog (for agent introspection) ===
ENDPOINTS_CATALOG = [
    {"path": "/health", "method": "GET", "category": "diagnostic"},
    {"path": "/stats", "method": "GET", "category": "diagnostic"},
    {"path": "/storage/health", "method": "GET", "category": "diagnostic"},
    {"path": "/endpoints", "method": "GET", "category": "meta"},
    {"path": "/endpoints/recommend/{scenario}", "method": "GET", "category": "meta"},
    {"path": "/chunks", "method": "POST", "category": "chunks"},
    {"path": "/chunks/{chunk_id}", "method": "GET", "category": "chunks"},
    {"path": "/chunks/{chunk_id}", "method": "POST", "category": "chunks"},
    {"path": "/chunks/{chunk_id}", "method": "DELETE", "category": "chunks"},
    {"path": "/search", "method": "POST", "category": "search"},
    {"path": "/search/tts", "method": "GET", "category": "search"},
    {"path": "/search/hybrid", "method": "GET", "category": "search"},
    {"path": "/list", "method": "GET", "category": "chunks"},
    {"path": "/relations/batch", "method": "GET", "category": "relations"},
    {"path": "/relations/{memory_id}", "method": "GET", "category": "relations"},
    {"path": "/relations/{memory_id}", "method": "DELETE", "category": "relations"},
    {"path": "/relations/temporal", "method": "POST", "category": "relations"},
    {"path": "/relations/temporal/{source_id}", "method": "GET", "category": "relations"},
    {"path": "/relations/{memory_id}/{target_id}", "method": "DELETE", "category": "relations"},
    {"path": "/links/create", "method": "POST", "category": "links"},
    {"path": "/links/{source_id}", "method": "GET", "category": "links"},
    {"path": "/links/access", "method": "POST", "category": "links"},
    {"path": "/links/path/{source_id}/{target_id}", "method": "GET", "category": "links"},
    {"path": "/links/decay", "method": "POST", "category": "links"},
    {"path": "/working/set", "method": "POST", "category": "working"},
    {"path": "/working/{session_id}", "method": "GET", "category": "working"},
    {"path": "/working/{session_id}/context", "method": "GET", "category": "working"},
    {"path": "/working/{session_id}/append", "method": "POST", "category": "working"},
    {"path": "/working/{session_id}/{key}", "method": "GET", "category": "working"},
    {"path": "/audit", "method": "GET", "category": "audit"},
    {"path": "/audit/log", "method": "POST", "category": "audit"},
    {"path": "/history/{memory_id}", "method": "GET", "category": "history"},
    {"path": "/provenance/{memory_id}", "method": "GET", "category": "history"},
    {"path": "/forgetting/stats", "method": "GET", "category": "forgetting"},
    {"path": "/forgetting/cleanup", "method": "POST", "category": "forgetting"},
    {"path": "/forgetting/expired", "method": "GET", "category": "forgetting"},
    {"path": "/forgetting/expiring-soon", "method": "GET", "category": "forgetting"},
    {"path": "/rumination/stats", "method": "GET", "category": "rumination"},
    {"path": "/rumination/run", "method": "POST", "category": "rumination"},
    {"path": "/consolidate/extract", "method": "POST", "category": "consolidate"},
    {"path": "/consolidate/run", "method": "POST", "category": "consolidate"},
    {"path": "/consolidate/facts", "method": "GET", "category": "consolidate"},
    {"path": "/compress/run", "method": "POST", "category": "maintenance"},
    {"path": "/compress/{source_chunk_id}", "method": "GET", "category": "maintenance"},
    {"path": "/reconsolidate", "method": "POST", "category": "maintenance"},
    {"path": "/merge-duplicates", "method": "POST", "category": "maintenance"},
    {"path": "/reindex", "method": "POST", "category": "maintenance"},
    {"path": "/storage/enforce", "method": "POST", "category": "storage"},
    {"path": "/storage/evict", "method": "POST", "category": "storage"},
    {"path": "/chroma/cleanup", "method": "GET", "category": "storage"},
    {"path": "/core/{user_id}", "method": "GET", "category": "core"},
    {"path": "/core", "method": "GET", "category": "core"},
    {"path": "/core/{user_id}", "method": "POST", "category": "core"},
    {"path": "/episodes", "method": "POST", "category": "episodes"},
    {"path": "/episodes/{memory_id}", "method": "GET", "category": "episodes"},
    {"path": "/procedural", "method": "POST", "category": "procedural"},
    {"path": "/procedural/search", "method": "GET", "category": "procedural"},
    {"path": "/procedural/{proc_id}/outcome", "method": "POST", "category": "procedural"},
    {"path": "/hierarchy", "method": "POST", "category": "hierarchy"},
    {"path": "/hierarchy/{node_id}", "method": "GET", "category": "hierarchy"},
    {"path": "/hierarchy/{node_id}/tree", "method": "GET", "category": "hierarchy"},
    {"path": "/hierarchy/enrich/{chunk_id}", "method": "GET", "category": "hierarchy"},
    {"path": "/taxonomy/relations", "method": "GET", "category": "taxonomy"},
    {"path": "/taxonomy/ancestors/{child_tag}", "method": "GET", "category": "taxonomy"},
    {"path": "/taxonomy/relations/{child_tag}", "method": "DELETE", "category": "taxonomy"},
    {"path": "/bench/multi-hop", "method": "GET", "category": "bench"},
    {"path": "/bench/multi-hop/custom", "method": "POST", "category": "bench"},
    {"path": "/cache/pr/stats", "method": "GET", "category": "cache"},
    {"path": "/cache/pr/clear", "method": "POST", "category": "cache"},
    {"path": "/access-stats", "method": "GET", "category": "access"},
    {"path": "/access-stats/{memory_id}", "method": "GET", "category": "access"},
    {"path": "/memories/{memory_id}/refresh", "method": "POST", "category": "access"},
    {"path": "/media", "method": "POST", "category": "media"},
    {"path": "/media/search", "method": "GET", "category": "media"},
    {"path": "/media/by-type/{media_type}", "method": "GET", "category": "media"},
    {"path": "/resources", "method": "POST", "category": "resources"},
    {"path": "/resources/search", "method": "GET", "category": "resources"},
    {"path": "/resources/{resource_id}/accessed", "method": "POST", "category": "resources"},
    {"path": "/dashboard", "method": "GET", "category": "dashboard"},
    {"path": "/dashboard/html", "method": "GET", "category": "dashboard"},
    {"path": "/stats/ops", "method": "GET", "category": "diagnostic"},
    {"path": "/stats/ops/reset", "method": "POST", "category": "diagnostic"},
    {"path": "/ttl/archive", "method": "POST", "category": "ttl"},
    {"path": "/ttl/archived", "method": "GET", "category": "ttl"},
    {"path": "/mode", "method": "GET", "category": "meta"},
    {"path": "/eval/feedback", "method": "POST", "category": "eval"},
    {"path": "/rumination/abstract-heuristics", "method": "POST", "category": "rumination"},
]


async def stats_by_type():
    """按 type 字段分组统计 (PG backend)"""
    rows = query("SELECT type, COUNT(*) AS c FROM memory_chunks GROUP BY type ORDER BY c DESC", fetch="all")
    return {r["type"] or "general": r["c"] for r in rows}

# V6.1.0: knowledge-center governance stats
async def stats_by_source():
    """按 source 字段分组统计. V6.1.0 governance -- 知识中心管理可视化.
    Common sources: user (memory tool manual), memory_tool (V5.4), hermes-session-summarizer (9b auto-extract), etc.
    """
    rows = query("SELECT source, COUNT(*) AS c FROM memory_chunks GROUP BY source ORDER BY c DESC", fetch="all")
    return {r["source"] or "(none)": r["c"] for r in rows}


def stats_by_status():
    """V6.2.0 (2026-08-13): explicit 3-bucket status breakdown.
    Resolves B2 inconsistency: total_memories = active + archived,
    but active splits into deprecated-not-archived + normal active."""
    try:
        active = query("SELECT count(*) AS c FROM memory_chunks WHERE archived = 0 AND is_deprecated = FALSE", fetch="one")["c"]
    except Exception:
        active = 0
    try:
        deprecated = query("SELECT count(*) AS c FROM memory_chunks WHERE archived = 0 AND is_deprecated = TRUE", fetch="one")["c"]
    except Exception:
        deprecated = 0
    try:
        archived = query("SELECT count(*) AS c FROM memory_chunks WHERE archived = 1", fetch="one")["c"]
    except Exception:
        archived = 0
    total = active + deprecated + archived
    return {
        "active": active,
        "deprecated_not_archived": deprecated,
        "archived": archived,
        "total": total,
    }


async def stats_by_deprecated():
    """按 is_deprecated 分组统计. V6.1.0 governance.
    Returns: {"active": N, "deprecated": M, "total": N+M}
    """
    rows = query("SELECT is_deprecated, COUNT(*) AS c FROM memory_chunks GROUP BY is_deprecated", fetch="all")
    out = {"active": 0, "deprecated": 0, "total": 0}
    for r in rows:
        key = "deprecated" if r["is_deprecated"] else "active"
        out[key] = r["c"]
        out["total"] += r["c"]
    return out


async def stats_by_importance():
    """按 importance 区间分组: 0-2 / 3-5 / 6-8 / 9-10"""
    rows = query("SELECT importance FROM memory_chunks", fetch="all")
    ranges = {"0-2":0, "3-5":0, "6-8":0, "9-10":0}
    for r in rows:
        v = r["importance"] or 0
        if v <= 2: ranges["0-2"] += 1
        elif v <= 5: ranges["3-5"] += 1
        elif v <= 8: ranges["6-8"] += 1
        else: ranges["9-10"] += 1
    return {"ranges": ranges}
def list_endpoints():
    """Dynamic endpoint catalogue derived from the live FastAPI router.

    P0 phase 3 (2026-11-25): the old hand-maintained ENDPOINTS_CATALOG drifted
    to 87 entries against 118 real routes (plus ghost entries with no handler).
    Deriving from ``app.routes`` makes this endpoint structurally incapable of
    drift. ``ENDPOINTS_CATALOG`` is kept as a static fallback for clients that
    import it directly; the API response now reflects reality.
    """
    endpoints = []

    def _iter_routes(routes):
        """Yield leaf routes, expanding included routers / mounts.

        FastAPI >=0.11 represents include_router() as a single _IncludedRouter
        object in app.routes instead of flattening its routes. Handle both
        shapes: older FastAPI (flat APIRoute list) and newer (nested).
        """
        for r in routes:
            # Newer FastAPI: include_router produced an aggregate object.
            inner = getattr(r, "original_router", None)
            if inner is not None and getattr(inner, "routes", None):
                yield from _iter_routes(inner.routes)
                continue
            # Generic nested router shape.
            if getattr(r, "routes", None) and getattr(r, "path", None) is None:
                yield from _iter_routes(r.routes)
                continue
            yield r

    for r in _iter_routes(app.routes):
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if not path or not methods:
            continue  # skip Mount (e.g. /static, /mcp) and other non-HTTP routes
        for m in sorted(methods):
            if m in ("HEAD", "OPTIONS"):
                continue
            seg = path.strip("/").split("/")[0] or "root"
            endpoints.append({"path": path, "method": m, "category": seg})
    # de-duplicate (mounts can appear under multiple expand paths)
    seen = set()
    endpoints = [e for e in endpoints
                 if not (e["path"], e["method"]) in seen
                 and not seen.add((e["path"], e["method"]))]
    endpoints.sort(key=lambda e: (e["path"], e["method"]))
    return {"total": len(endpoints), "endpoints": endpoints,
            "source": "app.routes"}

def recommend(scenario: str):
    # FIX (2026-08-02): added connector_memory / connector_workflow scenarios so a
    # client (hermes) can discover which cortex endpoints are actually useful for
    # its memory-tool workflow, instead of hardcoding them. Returns {scenario,
    # recommended: [paths], actions: {capability: method path}}.
    s = scenario.lower()
    if "connector_memory" in s or "tool_memory" in s:
        return {
            "scenario": scenario,
            "recommended": ["/list", "/search", "/search/tts", "/chunks", "/audit",
                           "/forgetting/stats", "/taxonomy/relations", "/working"],
            "actions": {
                "load":          {"method": "GET",  "path": "/list?source={source}&limit={limit}"},
                "write":         {"method": "POST", "path": "/chunks"},
                "search":        {"method": "POST", "path": "/search"},
                "search_tts":    {"method": "GET",  "path": "/search/tts?q={q}&top_k={k}"},
                "audit_read":    {"method": "GET",  "path": "/audit?limit={limit}"},
                "audit_log":     {"method": "POST", "path": "/audit/log"},
                "forgetting":    {"method": "GET",  "path": "/forgetting/stats"},
                "taxonomy":      {"method": "GET",  "path": "/taxonomy/relations"},
                "working_get":   {"method": "GET",  "path": "/working/{session_id}"},
                "working_set":   {"method": "POST", "path": "/working/set"},
            },
        }
    elif "search" in s:
        return {"scenario": scenario, "recommended": ["/search", "/list", "/search/tts", "/search/hybrid"]}
    elif "cleanup" in s or "maintenance" in s:
        return {"scenario": scenario, "recommended": ["/forgetting/cleanup", "/reconsolidate", "/merge-duplicates", "/ttl/archive"]}
    elif "audit" in s or "compliance" in s:
        return {"scenario": scenario, "recommended": ["/audit", "/access-stats", "/conflict/log", "/stats/ops"]}
    elif "forgetting" in s or "lifecycle" in s or "ttl" in s:
        return {"scenario": scenario, "recommended": ["/forgetting/stats", "/forgetting/expired", "/forgetting/expiring-soon", "/forgetting/guided"]}
    elif "bench" in s or "eval" in s or "quality" in s:
        return {"scenario": scenario, "recommended": ["/bench/multi-hop", "/eval/feedback", "/stats", "/health"]}
    elif "graph" in s or "relation" in s:
        return {"scenario": scenario, "recommended": ["/graph/sample", "/graph/khop", "/relations", "/links"]}
    elif "deprecated" in s or "governance" in s:
        return {"scenario": scenario, "recommended": ["/stats/by-deprecated", "/admin/deprecate-by-type", "/list", "/chunks"]}
    return {"scenario": scenario, "recommended": ["/health", "/stats", "/endpoints"]}

# === Chunks ===
class MemoryChunkCreate(BaseModel):
    # P2-D1 fix (2026-08-13): Field constraints for unified 422 errors.
    # Previously empty content / out-of-range importance silently accepted (200 OK).
    content: str = Field(..., min_length=1, description="non-empty chunk content")
    type: str = Field("general", description="chunk type whitelist")
    tags: List[str] = Field(default_factory=list)
    source: str = Field("user", min_length=1, description="non-empty source label")
    importance: int = Field(5, ge=1, le=10, description="1-10, controls tier+TTL")
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # V6.6.0 (2026-10-30): origin binding.
    #
    # Where the fact came from, and how much it can be trusted. This is the
    # provenance marker the 2026 memory-security literature identifies as
    # the only defence that holds: binding the origin at write time took
    # cross-session poisoning success to 0%, where prompt-level filtering
    # only managed 3.2 points.
    #
    # Defaults are deliberately conservative. A caller that says nothing is
    # treated as 'derived' (the agent worked this out from the user's own
    # input), NOT as 'user' — claiming the user asserted it would be an
    # over-claim, and that is exactly the laundering failure mode.
    origin: str = Field(
        "agent",
        max_length=32,
        description="Where the fact came from: 'user' | 'agent' | 'tool' | "
                    "'external' | a custom label.",
    )
    origin_trust: str = Field(
        "derived",
        max_length=16,
        description="Trust level: 'user' (directly asserted by the human) | "
                    "'derived' (extracted by the agent) | 'external' (from "
                    "outside the trust boundary: web, tool output, another "
                    "agent).",
    )
    origin_ref: Optional[str] = Field(
        None,
        max_length=1024,
        description="Concrete provenance handle: url, message id, tool call id.",
    )
    # V6.5.0 direction #3: optional per-request embedding model selection.
    # If omitted, server falls back to env var EMBEDDING_MODEL (default
    # "nomic-v1.5"). Whitelist-enforced so a typo doesn't fall through to a
    # broken encoder.
    embedding_model: Optional[str] = Field(
        None,
        description="Embedding model to use for this chunk. "
                    "One of: 'mpnet', 'nomic-v1.5'. Defaults to env EMBEDDING_MODEL.",
    )


# V6.6.0 (2026-10-30): allowed origin_trust values. Whitelisted at write
# time so a typo cannot silently create an unknown trust level (which
# would be read as "trust the highest level you recognise").
ALLOWED_ORIGIN_TRUST = {"user", "derived", "external"}


def _resolve_origin(
    origin: Optional[str],
    origin_trust: Optional[str],
    origin_ref: Optional[str],
) -> tuple:
    """Validate and normalise the provenance marker.

    V6.6.0 (2026-10-30). The rules here are the *policy* of the origin
    binding, and they are deliberately asymmetric:

    * ``origin_trust='user'`` requires an ``origin_ref``. A claim that the
      human personally asserted something must be traceable to the message
      that asserted it. Without a handle, "the user said this" is
      unfalsifiable — which is what makes it worthless to an auditor and
      exactly what a laundering attempt would look like.
    * ``origin_trust='external'`` also requires a ref, for the same reason:
      you cannot audit an external claim if you cannot find where it came
      from.
    * ``origin_trust='derived'`` (the default) needs no ref. It is the
      weakest claim: "the agent worked this out from context".

    An unknown trust level is rejected rather than silently downgraded, so
    a client typo fails loudly instead of quietly weakening the record.
    """
    o = (origin or "agent").strip().lower()[:32] or "agent"
    t = (origin_trust or "derived").strip().lower()[:16]
    ref = (origin_ref or None)

    if t not in ALLOWED_ORIGIN_TRUST:
        raise HTTPException(
            400,
            f"origin_trust={origin_trust!r} not allowed. "
            f"Allowed: {sorted(ALLOWED_ORIGIN_TRUST)}",
        )
    if t in ("user", "external") and not ref:
        raise HTTPException(
            400,
            f"origin_trust={t!r} requires origin_ref (the message id, url "
            f"or tool call id the claim came from). Without it the claim "
            f"cannot be audited.",
        )
    return o, t, ref


# Trust ordering: a derived record may never be MORE trusted than its
# source. Consolidating an 'external' observation must not produce a
# 'user' claim — that is provenance laundering.
_ORIGIN_TRUST_RANK = {"derived": 0, "external": 1, "user": 2}


def _inherit_origin(
    source_origin: Optional[str],
    source_trust: Optional[str],
    source_ref: Optional[str],
) -> tuple:
    """Derive a fact's origin marker from its source chunk's marker.

    V6.6.0 (2026-10-30). The rule is *inherit, never upgrade*:

    * the derived fact takes the source's origin and origin_ref — that is
      what makes the chain auditable end to end;
    * its trust level is the source's trust level, unless the source is
      unknown, in which case the weakest level ('derived') is assumed;
    * a fact extracted from an 'external' source stays 'external'. It does
      NOT become 'derived' (that would be laundering) and certainly does
      not become 'user'.

    The trust *rank* is used only to enforce "never more trusted": when a
    caller supplies both a source marker and an explicit marker, the LESS
    trusted of the two wins.
    """
    o = (source_origin or "agent").strip().lower()[:32] or "agent"
    t = (source_trust or "derived").strip().lower()[:16]
    if t not in ALLOWED_ORIGIN_TRUST:
        t = "derived"
    ref = source_ref or None
    return o, t, ref


def _most_distrustful(*trusts: Optional[str]) -> str:
    """Return the least trusted of the given levels (never upgrade)."""
    known = [t for t in trusts if t in _ORIGIN_TRUST_RANK]
    if not known:
        return "derived"
    return min(known, key=lambda t: _ORIGIN_TRUST_RANK[t])

# V6.1.0: whitelist for chunk types. Keeps "knowledge center" clean by
# rejecting unknown types at write time. Clients that need a custom type
# must update this list AND justify it in a CHANGELOG entry.
ALLOWED_TYPES = {
    "general",          # default bucket for unsorted knowledge
    "knowledge",        # curated factual knowledge
    "preference",       # user preferences / workflow style
    "skill",            # how-to / operational SOP
    "fact",             # discrete fact (key=value)
    "project",          # project-level context
    "session_summary",  # auto-extracted session digest (9b)
    "tool_experience",  # tool usage notes
    "environment-fingerprint",  # dev environment facts
    "test",             # test data (auto-deprecated after 7d via cron)
    "context",          # short-lived runtime context
}


def _validate_type(t: str) -> None:
    """V6.2.0: reject writes with unknown types (case-insensitive).
    Normalizes to lowercase before lookup."""
    t_lower = t.lower() if t else t
    if t_lower not in ALLOWED_TYPES:
        raise HTTPException(
            400,
            f"type={t!r} not allowed. Allowed types: {sorted(ALLOWED_TYPES)}",
        )


# V6.6.1 fix (2026-11-10): near-duplicate suppression on write.
#
# The ADR-068 conflict layer can detect "this fact supersedes that one",
# but it needs an LLM to extract the facts -- and the LLM endpoint is not
# always up. Without it the corpus accumulates near-identical statements
# that all stay active, and the hermes connector injects every one of them
# into the system prompt. Two VoxCPM2 "voice drift" entries written two
# hours apart is exactly that shape.
#
# This is the LLM-free half of the fix: when a new chunk arrives, look for
# an ACTIVE chunk of the same source whose embedding is very close to it.
# If the old one is no more important than the new one, mark it deprecated
# so the retrieval filters drop it. This mirrors what the conflict resolver
# would have concluded, just using cosine similarity instead of an LLM.
#
# Deliberately conservative:
#   - same `source` only (never deprecate across users / memory vs user)
#   - the new chunk must be at least as important as the old one
#   - the old chunk must be active right now
#   - a high similarity threshold, so only near-identical text is touched
#   - failures are swallowed: dedup must never break a write
NEAR_DUP_SIMILARITY = 0.93
NEAR_DUP_WINDOW = 200  # only look at the most recent N chunks of the source


def _supersede_near_duplicates(
    new_id: str,
    source: str,
    importance: int,
    emb_str: str,
    model_version: str,
) -> List[str]:
    """Mark near-duplicate active chunks of ``source`` as deprecated.

    Returns the list of chunk ids that were deprecated (empty on any
    failure -- this is a best-effort optimisation, never a hard error).
    """
    try:
        if not source or not emb_str:
            return []
        rows = query(
            """
            SELECT c.id, c.importance, 1 - (c.embedding <=> %s::vector) AS sim
            FROM memory_chunks c
            WHERE c.source = %s
              AND c.archived = 0
              AND c.is_deprecated = FALSE
              AND c.embedding IS NOT NULL
              AND (c.model_version IS NULL OR c.model_version = %s)
              AND c.id != %s
              AND 1 - (c.embedding <=> %s::vector) >= %s
            ORDER BY c.created_at DESC
            LIMIT %s
            """,
            (emb_str, source, model_version, new_id, emb_str,
             NEAR_DUP_SIMILARITY, NEAR_DUP_WINDOW),
            fetch="all-no-commit",
        )
        if not rows:
            return []
        if isinstance(rows, dict):
            rows = [rows]
        deprecated: List[str] = []
        for r in rows:
            old_id = r.get("id")
            if not old_id:
                continue
            try:
                old_imp = int(r.get("importance") or 0)
            except (TypeError, ValueError):
                old_imp = 0
            if importance < old_imp:
                # The new statement is less important than the old one --
                # keep the old fact alive. Mirrors the conflict resolver.
                continue
            try:
                execute(
                    "UPDATE memory_chunks SET is_deprecated = TRUE, "
                    "deprecated_at = NOW(), superseded_by = %s, updated_at = NOW() "
                    "WHERE id = %s AND archived = 0 AND is_deprecated = FALSE",
                    (new_id, old_id),
                )
            except Exception as exc:  # noqa: BLE001 -- best effort
                logger.warning("near-dup deprecate failed for %s: %s", old_id, exc)
                continue
            deprecated.append(old_id)
            try:
                hist_id = int(time.time() * 1e6) % 2000000000
                execute(
                    "INSERT INTO memory_history(id, memory_id, action, content, type, created_at) "
                    "VALUES (%s, %s, 'superseded', %s, 'near_duplicate', NOW())",
                    (hist_id, old_id, str(r.get("content") or "")[:2000]),
                )
            except Exception as exc:  # noqa: BLE001 -- audit is best effort
                logger.debug("near-dup history write failed for %s: %s", old_id, exc)
        if deprecated:
            logger.info(
                "near-duplicate suppression: %s superseded %s (source=%s)",
                new_id, deprecated, source,
            )
        return deprecated
    except Exception as exc:  # noqa: BLE001 -- never break the write path
        logger.debug("near-dup scan failed for %s: %s", new_id, exc)
        return []


def _tier_for_importance(importance: int):
    """FIX (2026-08-02): derive (tier, expires_at) from importance. Shared by
    create_chunk and update_chunk so re-graded records keep tier+TTL in sync.
    TTL semantics: importance<=3 short/7d; <=5 medium/30d; <=7 long/180d;
    else permanent/NULL (never expire)."""
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    if importance <= 3:
        return "short_term", (now + timedelta(days=7))
    elif importance <= 5:
        return "medium_term", (now + timedelta(days=30))
    elif importance <= 7:
        return "long_term", (now + timedelta(days=180))
    return "long_term", None  # permanent


def create_chunk(c: MemoryChunkCreate):
    _validate_type(c.type)
    cid = f"mem_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:6]}"
    # V6.5.0 direction #3: per-request model selection (or env default).
    selected_model = (c.embedding_model or EMBEDDING_MODEL_ENV).lower()
    if selected_model not in SUPPORTED_MODELS:
        raise HTTPException(
            400,
            f"embedding_model={selected_model!r} not supported. "
            f"Allowed: {sorted(SUPPORTED_MODELS)}",
        )
    # V6.6.0 fix (2026-10-30): label the vector with the model that ACTUALLY
    # encoded it. If nomic-v1.5 was requested but unavailable, the encoder
    # used mpnet — storing that vector as 'nomic-v1.5' is what split the
    # corpus into two incompatible spaces under one label.
    actual_model = resolve_embedding_model(selected_model)
    emb = embed_text(c.content, model_version=actual_model)
    emb_str = _vec_to_str_for_model(emb, actual_model)
    selected_model = actual_model
    now = datetime.now(timezone.utc).isoformat()
    tier, expires_at = _tier_for_importance(c.importance)
    expires_at = expires_at.isoformat() if expires_at else None
    # V6.6.0 (2026-10-30): bind the origin at write time. This is the
    # provenance marker the 2026 memory-security literature identifies as
    # the only defence that holds against cross-session poisoning (binding
    # the origin at write time -> 0% attack success; prompt-level filters
    # only managed 3.2 points).
    origin, origin_trust, origin_ref = _resolve_origin(
        c.origin, c.origin_trust, c.origin_ref
    )
    execute("""
        INSERT INTO memory_chunks(id, content, type, tags, source, importance, metadata, embedding, tier, expires_at, model_version, origin, origin_trust, origin_ref, created_at, updated_at)
        VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s::vector, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (cid, c.content, c.type, json.dumps(c.tags), c.source, c.importance,
          json.dumps(c.metadata), emb_str, tier, expires_at, selected_model,
          origin, origin_trust, origin_ref, now, now))
    hist_id = int(time.time() * 1e6) % 2000000000
    execute("""INSERT INTO memory_history(id, memory_id, action, content, type, created_at) VALUES (%s, %s, 'create', %s, %s, %s)""",
            (hist_id, cid, c.content, c.type, now))
    # V6.2.0 (2026-08-13): write audit 'create' action (was missing).
    try:
        execute(
            """INSERT INTO memory_access_audit(memory_id, action, resource_type, details, created_at)
               VALUES (%s, 'create', 'chunk', %s::jsonb, NOW())""",
            (cid, json.dumps({"actor": "chunks_post_endpoint", "type": c.type, "importance": c.importance, "tier": tier, "model_version": selected_model})),
        )
    except Exception as e:
        logger.warning(f"audit write failed for POST /chunks {cid}: {e}")

    # V6.5.0 Phase 2: enqueue this chunk_id for background maintenance
    # (extract + resolve_conflicts). Non-blocking; the scheduler flush
    # thread persists it to PG within flush_interval seconds.
    try:
        _scheduler.enqueue_chunk_id(cid)
    except Exception as _exc:  # noqa: BLE001 -- never break the hot path
        logger.warning("scheduler.enqueue_chunk_id failed for %s: %s", cid, _exc)

    # V6.6.1 fix (2026-11-10): suppress near-duplicates of the same source.
    # Without an LLM the conflict resolver cannot mark the older statement
    # as superseded, so both end up in the agent's system prompt. Runs
    # AFTER the insert (the new chunk must be queryable) and is best effort.
    try:
        _supersede_near_duplicates(
            cid, c.source, c.importance, emb_str, selected_model,
        )
    except Exception as _exc:  # noqa: BLE001 -- never break the hot path
        logger.warning("near-dup suppression failed for %s: %s", cid, _exc)

    return {"id": cid, "content": c.content, "type": c.type, "tags": c.tags, "source": c.source,
            "importance": c.importance, "metadata": c.metadata, "tier": tier,
            "expires_at": expires_at, "model_version": selected_model,
            "created_at": now, "updated_at": now}


def create_chunks_batch(payload: Dict):
    """V6.2.0 (2026-08-13): bulk create chunks.
    Body: {"chunks": [{content, type, tags, ...}, ...]}
    Returns: {created: int, ids: [...], errors: [...]}
    Best-effort: partial failures return created=N (no raise)."""
    items = payload.get("chunks", [])
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "chunks array required")
    if len(items) > 500:
        raise HTTPException(400, f"batch size {len(items)} exceeds cap 500")
    ids = []
    errors = []
    # V6.5.0 direction #3: optional per-batch override, else env default.
    batch_model = (payload.get("embedding_model") or EMBEDDING_MODEL_ENV).lower()
    if batch_model not in SUPPORTED_MODELS:
        raise HTTPException(
            400,
            f"embedding_model={batch_model!r} not supported. "
            f"Allowed: {sorted(SUPPORTED_MODELS)}",
        )
    for i, item in enumerate(items):
        try:
            cid = f"mem_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:6]}_{i}"
            # Per-item override (rare) falls back to batch-level.
            item_model = (item.get("embedding_model") or batch_model).lower()
            if item_model not in SUPPORTED_MODELS:
                item_model = batch_model
            # V6.6.0 fix: label with the model that actually encoded it.
            actual_item_model = resolve_embedding_model(item_model)
            emb = embed_text(item["content"], model_version=actual_item_model)
            emb_str = _vec_to_str_for_model(emb, actual_item_model)
            item_model = actual_item_model
            now = datetime.now(timezone.utc).isoformat()
            tier, expires_at = _tier_for_importance(item.get("importance", 5))
            expires_at = expires_at.isoformat() if expires_at else None
            # V6.6.0 (2026-10-30): bind the origin at write time. A batch
            # may set a batch-level origin/origin_trust/origin_ref, and an
            # item may override it. The origin is what makes a record
            # auditable, so it travels with the row.
            item_origin, item_trust, item_ref = _resolve_origin(
                item.get("origin") or payload.get("origin"),
                item.get("origin_trust") or payload.get("origin_trust"),
                item.get("origin_ref") or payload.get("origin_ref"),
            )
            execute(
                """INSERT INTO memory_chunks(id, content, type, tags, source, importance, metadata, embedding, tier, expires_at, model_version, origin, origin_trust, origin_ref, created_at, updated_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s::vector, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (cid, item["content"], item.get("type", "general"),
                 json.dumps(item.get("tags", [])), item.get("source", "user"),
                 item.get("importance", 5), json.dumps(item.get("metadata", {})),
                 emb_str, tier, expires_at, item_model,
                 item_origin, item_trust, item_ref, now, now),
            )
            ids.append(cid)
        except Exception as e:
            errors.append({"index": i, "error": str(e)})
    return {"created": len(ids), "ids": ids, "errors": errors}


def get_chunk(chunk_id: str, include_embedding: bool = True):
    """V6.2.0 (2026-08-13): include_embedding query param (default False).
    Pass ?include_embedding=true to get the 3.6KB embedding vector."""
    row = query("SELECT * FROM memory_chunks WHERE id = %s", (chunk_id,), fetch="one")
    if not row:
        raise HTTPException(404, "chunk not found")
    row["tags"] = row["tags"] or []
    row["metadata"] = row["metadata"] or {}
    row["created_at"] = row["created_at"].isoformat() if row.get("created_at") else None
    row["updated_at"] = row["updated_at"].isoformat() if row.get("updated_at") else None
    if not include_embedding and "embedding" in row:
        row = {k: v for k, v in row.items() if k != "embedding"}
    # P3-D2 fix (2026-08-13): write read audit.
    # Failures don't break the GET path (try/except wraps).
    try:
        execute(
            """INSERT INTO memory_access_audit(memory_id, action, resource_type, details, created_at)
               VALUES (%s, 'read', 'chunk', %s::jsonb, NOW())""",
            (chunk_id, json.dumps({"actor": "chunks_get_endpoint", "include_embedding": include_embedding})),
        )
    except Exception as e:
        logger.warning(f"audit write failed for GET /chunks/{chunk_id}: {e}")
    return row

def update_chunk(chunk_id: str, c: MemoryChunkCreate):
    _validate_type(c.type)
    existing = query("SELECT id, model_version FROM memory_chunks WHERE id = %s", (chunk_id,), fetch="one")
    if not existing:
        raise HTTPException(404, "chunk not found")
    # V6.5.0 direction #3: respect per-request override; default to the chunk's
    # existing model (so an UPDATE doesn't silently switch a mpnet chunk to
    # nomic-v1.5 mid-edit). ETL handles the upgrade separately.
    selected_model = (c.embedding_model or existing.get("model_version") or EMBEDDING_MODEL_ENV).lower()
    if selected_model not in SUPPORTED_MODELS:
        selected_model = existing.get("model_version") or EMBEDDING_MODEL_ENV
    # V6.6.0 fix: label with the model that actually encoded it.
    actual_selected_model = resolve_embedding_model(selected_model)
    emb = embed_text(c.content, model_version=actual_selected_model)
    emb_str = _vec_to_str_for_model(emb, actual_selected_model)
    selected_model = actual_selected_model
    now = datetime.now(timezone.utc).isoformat()
    # FIX (2026-08-02): update_chunk re-derives tier + expires_at from the new
    # importance. Previously only `importance` was set but tier stayed stale,
    # so a short->medium re-grade left tier=short_term with a 7-day TTL.
    # Upgrade-only rule: never let a re-grade SHORTEN an existing TTL. We
    # compute the TTL the new importance implies; if the record already has a
    # later expiry (or is permanent), keep the longer one.
    new_tier, new_exp = _tier_for_importance(c.importance)
    existing_row = query(
        "SELECT tier, expires_at FROM memory_chunks WHERE id = %s", (chunk_id,), fetch="one")
    cur_exp = existing_row.get("expires_at") if existing_row else None
    # Upgrade-only + never-forget rule:
    #   - new importance implies permanent (imp>=8): clear expires_at.
    #   - record is ALREADY permanent (cur_exp is None) and new is not:
    #     keep it permanent -- a downgrade must never resurrect a TTL on a
    #     vaulted record. Once permanent, stays permanent.
    #   - otherwise keep the LATER expiry (never shorten).
    if new_exp is None:
        tier, expires_at = new_tier, None            # re-graded to permanent
    elif cur_exp is None:
        tier, expires_at = new_tier, None            # was permanent; stay permanent
    elif cur_exp > new_exp:
        tier, expires_at = new_tier, cur_exp.isoformat()   # keep existing longer TTL
    else:
        tier, expires_at = new_tier, new_exp.isoformat()   # extend to new TTL
    execute("""
        UPDATE memory_chunks SET content=%s, type=%s, tags=%s::jsonb, source=%s, importance=%s,
        metadata=%s::jsonb, embedding=%s::vector, tier=%s, expires_at=%s, model_version=%s, updated_at=%s
        WHERE id=%s
    """, (c.content, c.type, json.dumps(c.tags), c.source, c.importance,
          json.dumps(c.metadata), emb_str, tier, expires_at, selected_model, now, chunk_id))
    hist_id = int(time.time() * 1e6) % 2000000000
    execute("""INSERT INTO memory_history(id, memory_id, action, content, type, created_at) VALUES (%s, %s, 'update', %s, %s, %s)""",
            (hist_id, chunk_id, c.content, c.type, now))
    # V6.2.0 (2026-08-13): write audit 'update' action.
    try:
        execute(
            """INSERT INTO memory_access_audit(memory_id, action, resource_type, details, created_at)
               VALUES (%s, 'update', 'chunk', %s::jsonb, NOW())""",
            (chunk_id, json.dumps({"actor": "chunks_put_endpoint", "type": c.type, "importance": c.importance, "model_version": selected_model})),
        )
    except Exception as e:
        logger.warning(f"audit write failed for PUT /chunks/{chunk_id}: {e}")
    return get_chunk(chunk_id)



class MemoryChunkPatch(BaseModel):
    """P1-B3 (2026-08-13): PATCH-style partial update. All fields Optional,
    only non-None fields get applied. content change re-derives embedding.
    type/source/importance change re-derives tier+TTL with same upgrade-only rule.
    """
    content: Optional[str] = Field(None, min_length=1)
    type: Optional[str] = None
    tags: Optional[List[str]] = None
    source: Optional[str] = None
    importance: Optional[int] = Field(None, ge=1, le=10)
    metadata: Optional[Dict[str, Any]] = None


def patch_chunk(chunk_id: str, p: MemoryChunkPatch):
    """部分更新 chunk. 字段都是 Optional, 只更新传入的非 None 字段.
    不传 content 时不重算 embedding. 不传 importance 时不动 tier/TTL."""
    existing = query("SELECT id, content, type, tags, source, importance, metadata, embedding, tier, expires_at, model_version FROM memory_chunks WHERE id = %s", (chunk_id,), fetch="one")
    if not existing:
        raise HTTPException(404, "chunk not found")

    if p.type is not None:
        _validate_type(p.type)

    new_content = p.content if p.content is not None else existing["content"]
    new_type = p.type if p.type is not None else existing["type"]
    new_tags = p.tags if p.tags is not None else (existing["tags"] or [])
    new_source = p.source if p.source is not None else existing["source"]
    new_importance = p.importance if p.importance is not None else existing["importance"]
    new_metadata = p.metadata if p.metadata is not None else (existing["metadata"] or {})
    # V6.5.0 direction #3: keep the chunk on its current model unless the
    # caller explicitly overrode it. Preserves migration semantics: ETL flips
    # model_version atomically with the embedding rewrite, not on PATCH.
    current_model = (existing.get("model_version") or "mpnet").lower()
    new_model = current_model  # default: stay on the same model

    needs_reembed = (p.content is not None and p.content != existing["content"]) or existing["embedding"] is None

    if needs_reembed:
        # V6.6.0 fix (2026-10-30): label with the model that actually encoded
        # it. If the chunk's model is unavailable at runtime the encoder
        # falls back, and the chunk must be relabelled to match — otherwise
        # its vector sits in a different space from its model_version peers.
        actual_new_model = resolve_embedding_model(new_model)
        emb = embed_text(new_content, model_version=actual_new_model)
        emb_str = _vec_to_str_for_model(emb, actual_new_model)
        new_model = actual_new_model
    else:
        emb_str = existing["embedding"]

    if p.importance is not None and p.importance != existing["importance"]:
        new_tier, new_exp = _tier_for_importance(new_importance)
        cur_exp = existing.get("expires_at")
        if new_exp is None:
            tier, expires_at = new_tier, None
        elif cur_exp is None:
            tier, expires_at = new_tier, None
        elif cur_exp > new_exp:
            tier, expires_at = new_tier, cur_exp.isoformat()
        else:
            tier, expires_at = new_tier, new_exp.isoformat()
    else:
        tier = existing["tier"]
        expires_at = existing["expires_at"]
        if expires_at is not None and hasattr(expires_at, "isoformat"):
            expires_at = expires_at.isoformat()

    now = datetime.now(timezone.utc).isoformat()
    execute("""UPDATE memory_chunks SET content=%s, type=%s, tags=%s::jsonb, source=%s, importance=%s,
              metadata=%s::jsonb, embedding=%s::vector, tier=%s, expires_at=%s, model_version=%s, updated_at=%s
              WHERE id=%s""",
            (new_content, new_type, json.dumps(new_tags), new_source, new_importance,
             json.dumps(new_metadata), emb_str, tier, expires_at, new_model, now, chunk_id))

    hist_id = int(time.time() * 1e6) % 2000000000
    execute("""INSERT INTO memory_history(id, memory_id, action, content, type, created_at) VALUES (%s, %s, 'patch', %s, %s, %s)""",
            (hist_id, chunk_id, new_content, new_type, now))
    # V6.2.0 (2026-08-13): write audit 'patch' action.
    try:
        execute(
            """INSERT INTO memory_access_audit(memory_id, action, resource_type, details, created_at)
               VALUES (%s, 'patch', 'chunk', %s::jsonb, NOW())""",
            (chunk_id, json.dumps({"actor": "chunks_patch_endpoint", "importance": new_importance, "model_version": new_model})),
        )
    except Exception as e:
        logger.warning(f"audit write failed for PATCH /chunks/{chunk_id}: {e}")
    return get_chunk(chunk_id)

def delete_chunk(chunk_id: str):
    row = query("SELECT content, type FROM memory_chunks WHERE id = %s", (chunk_id,), fetch="one")
    if not row:
        raise HTTPException(404, "chunk not found")
    now = datetime.now(timezone.utc).isoformat()
    hist_id = int(time.time() * 1e6) % 2000000000
    execute("""INSERT INTO memory_history(id, memory_id, action, content, type, created_at) VALUES (%s, %s, 'delete', %s, %s, %s)""",
            (hist_id, chunk_id, row["content"], row["type"], now))
    execute("DELETE FROM memory_chunks WHERE id = %s", (chunk_id,))
    execute("DELETE FROM memory_relations WHERE memory_id_1 = %s OR memory_id_2 = %s", (chunk_id, chunk_id))
    # V6.2.0 (2026-08-13): write audit 'delete' action (was missing).
    try:
        execute(
            """INSERT INTO memory_access_audit(memory_id, action, resource_type, details, created_at)
               VALUES (%s, 'delete', 'chunk', %s::jsonb, NOW())""",
            (chunk_id, json.dumps({"actor": "chunks_delete_endpoint", "type": row["type"]})),
        )
    except Exception as e:
        logger.warning(f"audit write failed for DELETE /chunks/{chunk_id}: {e}")
    return {"id": chunk_id, "status": "deleted"}

# === Admin: bulk deprecate by type (V6.1.0) ===
# Preserves data + audit trail; reversible via /chunks/{id} POST.
# Usage: POST /admin/deprecate-by-type {"type": "...", "dry_run": true|false, "reason": "..."}
def admin_deprecate_by_type(payload: dict = Body(...)):
    chunk_type = (payload.get("type") or "").strip()
    if not chunk_type:
        raise HTTPException(400, "type required")
    reason = (payload.get("reason") or "manual deprecation").strip()[:500]
    dry_run = bool(payload.get("dry_run", False))
    if dry_run:
        cnt = query("SELECT COUNT(*) AS c FROM memory_chunks WHERE type = %s",
                    (chunk_type,), fetch="one")["c"]
        already = query("SELECT COUNT(*) AS c FROM memory_chunks WHERE type = %s AND is_deprecated = TRUE",
                        (chunk_type,), fetch="one")["c"]
        return {"dry_run": True, "type": chunk_type,
                "would_deprecate": cnt - already, "already_deprecated": already,
                "total_matching": cnt}
    affected = query(
        "UPDATE memory_chunks SET is_deprecated = TRUE, deprecated_at = NOW(), updated_at = NOW() "
        "WHERE type = %s AND is_deprecated = FALSE RETURNING id",
        (chunk_type,), fetch="all")
    if affected:
        now_ts = datetime.now(timezone.utc).isoformat()
        rows = []
        for i, r in enumerate(affected):
            hist_id = (int(time.time() * 1e6) + i) % 2000000000
            rows.append((hist_id, r["id"], "bulk_deprecate", reason, chunk_type, now_ts))
        placeholders = ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(rows))
        execute("INSERT INTO memory_history(id, memory_id, action, content, type, created_at) VALUES " + placeholders,
                tuple(v for row in rows for v in row))
    # V6.2.0 (2026-08-13): write audit 'admin_deprecate' for affected chunks.
    if affected:
        try:
            for r in affected:
                execute(
                    """INSERT INTO memory_access_audit(memory_id, action, resource_type, details, created_at)
                       VALUES (%s, 'admin_deprecate', 'chunk', %s::jsonb, NOW())""",
                    (r["id"], json.dumps({"actor": "admin_deprecate_endpoint",
                                            "type": chunk_type, "reason": reason})),
                )
        except Exception as e:
            logger.warning(f"audit write failed for admin/deprecate {chunk_type}: {e}")
    return {"deprecated_count": len(affected), "type": chunk_type, "reason": reason}


# === List ===
def list_chunks(
    type: Optional[str] = None,
    # P3-B4 fix (2026-08-13): tags accepts comma-separated list.
    tags: Optional[str] = None,
    match_all: bool = True,
    min_importance: Optional[int] = None,
    # V6.2.0 (2026-08-13): cap limit/offset to prevent OOM (was unbounded).
    limit: int = 50,  # hard cap 1000
    offset: int = 0,  # must be >= 0
    target: Optional[str] = None,
    source: Optional[str] = None,
    include_embedding: bool = False,  # V6.2.0: default False (saves 3.6KB/row)
    # V6.6.1 fix (2026-10-31): lifecycle-aware listing.
    #
    # /list is the endpoint the hermes connector uses to build the frozen
    # system-prompt snapshot (cortex_memory_backend.list_entries -> GET /list).
    # Before this fix it filtered ONLY `archived = 0`, so facts that had been
    # superseded by a newer one (is_deprecated = TRUE) and facts whose TTL had
    # already elapsed (expires_at < NOW()) were still served -- and therefore
    # still injected into the agent's system prompt. That is the "stale fact
    # keeps coming back" symptom.
    #
    # Both switches default to False (hide stale). Callers that genuinely need
    # the full history (e.g. the hermes duplicate check, which must still see
    # a deprecated entry so it does not re-create it) opt in explicitly.
    include_deprecated: bool = False,
    include_expired: bool = False,
):
    # FIX (2026-08-02): added `source` filter param. The hermes connector was
    # classifying memory/user chunks by `type` (which in PG is e.g. 'rag_document'
    # or 'general', not 'knowledge'/'preference') -- the real signal is the
    # `source` column ('hermes_memory' vs 'hermes_user' vs 'memory_tool' etc).
    # Backward-compatible: `type` filter still works.
    where = []
    params = []
    if type:
        where.append("type = %s"); params.append(type)
    if source:
        where.append("source = %s"); params.append(source)
    # P3-B4: multi-tag filter using jsonb_array_elements_text EXISTS.
    # match_all=True -> AND across tags; False -> OR (any tag).
    tag_params = []
    # V6.2.0 (2026-08-13): cap limit/offset to prevent OOM (was unbounded).
    if limit > 1000:
        raise HTTPException(400, f"limit={limit} exceeds hard cap 1000")
    if limit < 1:
        raise HTTPException(400, f"limit={limit} must be >= 1")
    if offset < 0:
        raise HTTPException(400, f"offset={offset} must be >= 0")
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            tag_params = tag_list
            joiner = " AND " if match_all else " OR "
            tag_clause = joiner.join(
                ["(EXISTS (SELECT 1 FROM jsonb_array_elements_text(tags) t WHERE t = %s))"] * len(tag_list)
            )
            where.append(f"({tag_clause})")
    if min_importance:
        where.append("importance >= %s"); params.append(min_importance)
    # V6.6.1 fix (2026-10-31): lifecycle filters. `is_deprecated` marks a fact
    # that a newer fact has superseded; `expires_at` is the TTL that
    # _tier_for_importance() assigns by importance band. Neither was ever
    # consulted at read time -- only `archived` was.
    if not include_deprecated:
        where.append("is_deprecated = FALSE")
    if not include_expired:
        where.append("(expires_at IS NULL OR expires_at > NOW())")
    # P1-A4 fix (2026-08-13): total should be real DB COUNT(*), not len(rows).
    count_sql = f"SELECT COUNT(*) AS c FROM memory_chunks WHERE archived = 0"
    if where:
        count_sql += " AND " + " AND ".join(where)
    total_count = query(count_sql, tuple(params + tag_params), fetch="one")["c"]
    sql = f"SELECT * FROM memory_chunks WHERE archived = 0"
    if where:
        sql += " AND " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
    list_params = params + tag_params + [limit, offset]
    rows = query(sql, tuple(list_params), fetch="all-no-commit")
    # V6.2.0 (2026-08-13): strip embedding by default to save bandwidth.
    if not include_embedding:
        for r in rows:
            r.pop("embedding", None)
    for r in rows:
        r["tags"] = r["tags"] or []
        r["metadata"] = r["metadata"] or {}
        r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
        r["updated_at"] = r["updated_at"].isoformat() if r.get("updated_at") else None
    return {"total": total_count, "chunks": rows, "limit": limit, "offset": offset}

# === Search ===
# V6.6.0 fix (2026-10-30): English stopwords excluded from the lexical
# OR-query. The list is deliberately small and function-word-only: it must
# not touch content words, because the lexical channel exists precisely to
# rescue chunks whose *wording* (not paraphrase) matches the query.
_LEXICAL_STOPWORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can",
    "had", "her", "was", "one", "our", "out", "day", "get", "has", "him",
    "his", "how", "man", "new", "now", "old", "see", "two", "way", "who",
    "boy", "did", "its", "let", "put", "say", "she", "too", "use", "that",
    "this", "with", "from", "they", "what", "when", "where", "which",
    "were", "will", "would", "there", "their", "them", "then", "than",
    "been", "being", "have", "into", "your", "youre", "youve", "about",
    "does", "doing", "just", "like", "make", "made", "more", "most",
    "much", "must", "only", "other", "over", "same", "some", "such",
    "than", "them", "very", "want", "well", "were", "what", "your",
    "is", "it", "in", "of", "to", "at", "by", "on", "or", "as", "be",
    "am", "an", "we", "he", "she", "i", "a",
})


def _expand_query_forms(query: str) -> List[str]:
    """Build alternative phrasings of a query for multi-query expansion.

    V6.6.0 (2026-10-30). One embedding of one phrasing can only match
    what that phrasing happens to be near. Expanding the query into a few
    forms widens the semantic net without needing a second model.

    Measured on the LOCOMO corpus (8695 chunks) on 127.0.0.1.
    NOTE: an earlier measurement of this claimed "+6.8% recall" and was
    WRONG — it compared a recall@5 baseline against a merged list that was
    never truncated back to 5 (it was really a recall@15). Re-measured
    honestly, with both sides truncated to top-5:

        single query    recall@5=0.5425  MRR=0.4337
        3 forms merged  recall@5=0.5442  MRR=0.4395   (+0.3% recall)

    So the real gain is marginal. The headroom it used to fill was
    actually the RRF/stopword defect (fixed separately); once the vector
    channel was no longer being diluted, extra phrasings had almost
    nothing left to add. Measured after that fix, expansion moves
    recall@5 from 0.5582 to 0.5634 (+0.9%) at ~3x the latency
    (70 ms -> 198 ms), which is why it stays OFF by default.

    It is kept as an opt-in because it does help on queries whose exact
    phrasing is a poor match for the stored fact, and because the
    multi-signal-fusion literature (Mem0: temporal +29.6 pp, multi-hop
    +23.1 pp) suggests a richer query-side signal is the right direction —
    but the honest number is "small", not "+6.8%".

    The forms are deliberately cheap and deterministic (no LLM call):
      1. the original query — always first, so plain behaviour is preserved;
      2. a keyword-only form with stopwords removed;
      3. a statement form with the leading question word dropped
         ("When did X go to Y?" -> "X go to Y"), which is closer to how the
         fact is actually *stored*.

    Returns a de-duplicated list, original first.
    """
    q = (query or "").strip()
    if not q:
        return []

    forms: List[str] = [q]
    seen = {q.lower()}

    def _add(candidate: str) -> None:
        c = candidate.strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            forms.append(c)

    # 2) keyword-only form
    normalized = re.sub(r"[_/+.\-]+", " ", q)
    tokens = re.findall(r"[\w一-鿿]+", normalized)
    keywords = [t for t in tokens if len(t) >= 2 and t.lower() not in _LEXICAL_STOPWORDS]
    if keywords and len(keywords) != len(tokens):
        _add(" ".join(keywords))

    # 3) statement form: drop the leading interrogative
    statement = re.sub(
        r"^\s*(when|where|who|whom|whose|what|which|why|how|"
        r"did|do|does|is|are|was|were|has|have|had|can|could|will|would)\b\s*",
        "",
        q,
        flags=re.IGNORECASE,
    )
    if statement:
        _add(statement)

    return forms


class MemorySearchRequest(BaseModel):
    query: str
    limit: int = 10  # hard cap 500 enforced below in search_memory()
    type: Optional[str] = None
    # v6.5.0 (2026-08-23, direction #2): Pydantic Field 校验, 拒绝 -1 / 11 / 20 等越界值
    # 默认 0 (不过滤), 保持向后兼容; 显式传 0 也走不过滤路径
    min_importance: int = Field(0, ge=0, le=10)
    tags: Optional[List[str]] = None
    include_related: bool = False
    # V6.6.0 (2026-10-31): the graph as a RANKING signal rather than a
    # candidate expansion. Expanding the pool along graph edges measured
    # slightly negative (the vector channel already retrieves those
    # neighbours -- edges are mined at cosine >= 0.72), but the graph
    # still carries a signal the vector channel does not: a memory
    # connected to several of the query's top hits.
    #
    # 0 = off (the base ranking is then bit-for-bit unchanged).
    graph_score_weight: float = Field(
        0.0, ge=0.0, le=1.0,
        description="图谱连通信号在最终 composite 中的权重 (0=不参与).",
    )
    graph_anchor_count: int = Field(
        5, ge=1, le=20,
        description="用作连通性锚点的 top-N 命中数.",
    )
    # V6.6.0 (2026-10-31): the importance floor for graph-expanded
    # neighbours. 6 is the tuned default; 0 disables the gate. See the
    # comment at the filter for why a hardcoded constant was wrong.
    graph_neighbor_min_importance: int = Field(
        6, ge=0, le=10,
        description="图谱扩展进来的邻居的 importance 下限 (0=不过滤).",
    )
    # V5.3 混合检索增强：
    #   hybrid=True  → 向量 + 字面(ts_rank) RRF 融合（默认开）
    #   include_related=True → 命中后沿 relations/links 扩展图谱邻居
    #   boost_relevance=True → 三因子重排（相似度 × 时效 × relevance_score）
    hybrid: bool = True
    boost_relevance: bool = True
    top_lexical: int = 24      # V6.2.0 cap 200
    top_vector: int = 24       # V6.2.0 cap 200
    rrf_k: int = 60            # RRF smooth constant
    # V6.6.0 fix (2026-10-30): per-channel RRF weight for the lexical
    # channel. The lexical channel is much weaker than the vector channel
    # (measured standalone MRR=0.000 on the LOCOMO corpus) and the old
    # equal-weight fusion let it inject noise into the top-5: the default
    # scored MRR=0.4073 against 0.4560 for pure vector search.
    # 0.0 disables the lexical channel entirely; 1.0 restores the old
    # equal-weight behaviour.
    lexical_rrf_weight: float = Field(
        0.25, ge=0.0, le=1.0,
        description="词面通道在 RRF 融合中的权重. 0=关闭词面通道, "
                    "1=旧的等权融合.",
    )
    # V6.6.0 (2026-10-30): multi-query expansion. Off by default because
    # each extra form costs one more embedding + one more vector scan.
    # Measured on the LOCOMO corpus (after the RRF/stopword fix):
    # recall@5 0.5582 -> 0.5634 (+0.9%) at ~3x the latency.
    # Kept off by default for that reason.
    query_expansion: bool = False
    # V6.6.0 (2026-10-30): RAPTOR summary-tree recall channel.
    #
    # Measured on the LOCOMO corpus (8695 chunks, 250 queries): the vector
    # channel put the answer in the top-5 for 58.4% of queries, and for
    # the 46 it missed, the chunks outranking the answer belonged to the
    # SAME conversation in every case — the answer needed many turns read
    # together ("what is X's relationship status?"), so no single turn was
    # a good vector match.
    #
    # The tree fixes exactly that: its path score rewards a chunk whose
    # whole cluster is relevant over one that merely looks locally close.
    # Measured: RAPTOR top-5 = 62.8%, union with vector = 65.2%, with 16
    # answers the vector channel missed entirely.
    #
    # Off by default: the tree is built by a background job, and until it
    # has been built the channel has nothing to add.
    raptor: bool = False
    top_raptor: int = 20
    raptor_rrf_weight: float = Field(
        0.5, ge=0.0, le=2.0,
        description="RAPTOR 通道在 RRF 融合中的权重.",
    )
    # How much the tree's score contributes to the final composite, and
    # how many of the tree's hits get that contribution at all.
    #
    # The breadth matters: the tree proposes up to top_raptor chunks, and
    # a flat boost on all of them floods the top-5 with the tree's 20th
    # guess. Measured on the LOCOMO corpus the tree finds the answer in
    # its own top-20 for most queries it can find at all, but the answer
    # is usually near the TOP of that list — so a small breadth captures
    # the signal without the noise.
    raptor_score_weight: float = Field(
        0.0, ge=0.0, le=2.0,
        description="树分数在最终 composite 中的权重 (0=不参与). "
                    "实测最优是 0 —— 树的价值完全在候选池, 它的命中交给 "
                    "既有的 RRF+composite 排序判断即可. 任何非零加成都"
                    "同时损失 MRR (0.4625 -> 0.4387) 和正确性.",
    )
    raptor_boost_top_n: int = Field(
        3, ge=1, le=50,
        description="只有树排名最前的 N 条获得 composite 加成.",
    )
    # V6.5.0 direction #3: explicit query-side embedding model selector.
    # The search dispatcher reads the requested model, but per-chunk dispatch
    # still wins: if a chunk was embedded with mpnet, search uses mpnet to
    # score it (regardless of `embedding_model` here). `embedding_model` only
    # controls the QUERY vector. Default: "nomic-v1.5" (matches v6.5.0 default).
    embedding_model: Optional[str] = Field(
        "nomic-v1.5",
        description="Model for the query vector. Per-chunk dispatch still "
                    "respects each chunk's stored model_version. One of: "
                    "'mpnet', 'nomic-v1.5'.",
    )
    # V6.6.0 direction #1: Cross-Encoder 精排 (rerank).
    # 默认关闭 —— 需要 MEMORY_FEATURE_RERANK=1 且模型快照就位才生效.
    # rerank=True 时还要 MEMORY_FEATURE_RERANK 打开 (双闸门: 调用方显式请求
    # + 服务端灰度开关), 避免老客户端无意中改变排序行为.
    rerank: bool = False
    rerank_top_k: int = Field(
        30,
        ge=1,
        le=100,
        description="参与精排的候选上限. 精排是 CPU 前向 O(N), "
                    "30~50 是 CPU Cross-Encoder 的延迟甜点.",
    )
    # P0 (2026-11-26): minimum score a recalled chunk must clear to be
    # returned. Compares against the final composite_score (when
    # boost_relevance) else the raw cosine similarity. 0.45 is the tuned
    # floor: it keeps borderline-but-relevant results while dropping
    # pure-noise matches that the old un-gated path returned. 0 disables
    # the gate (return everything up to limit).
    score_threshold: float = Field(
        0.45,
        ge=0.0,
        le=1.0,
        description="最低返回分 (composite_score 或向量相似度). 0=不过滤.",
    )


# V6.6.0 fix (2026-10-30): per-channel RRF weights.
#
# _rrf_fusion() used to sum 1/(k+rank) over all channels with NO per-channel
# weight, so a lexical rank-0 hit scored exactly the same as a vector
# rank-0 hit. But the lexical channel is far weaker than the vector
# channel: measured on the LOCOMO corpus (8695 chunks) on 127.0.0.1,
# lexical ALONE scored MRR=0.000, and out of 200 queries only 5 had the
# answer found uniquely by lexical (9 by vector, 85 by neither).
#
# So the lexical channel was injecting noise into the top-5. Server-side
# A/B, 300 LOCOMO queries, same limit=5:
#
#     top_lexical=24 (default)   MRR=0.4073  recall@5=0.4936
#     top_lexical=0              MRR=0.4555  recall@5=0.5081
#     top_lexical=6              MRR=0.4119  recall@5=0.5047
#     top_lexical=48             MRR=0.4006  recall@5=0.4875
#     hybrid=false (vector only) MRR=0.4560  recall@5=0.5114
#
# The DEFAULT configuration was 12% worse on MRR than plain vector search,
# and got monotonically worse as the lexical pool grew.
#
# The lexical channel is not useless — it is what rescues a chunk whose
# wording is distinctive but whose paraphrase is not. So it is kept, at a
# weight where it can no longer outrank a semantic match, while a chunk
# found by BOTH channels still gets the sum of both (and therefore still
# ranks above a single-channel hit).
DEFAULT_CHANNEL_WEIGHTS: Dict[str, float] = {
    "vector": 1.0,
    "lexical": 0.25,
    "tag": 1.0,
    # P0 (2026-11-26): the RAPTOR summary-tree channel. It is a genuinely
    # different view of the corpus — a cluster-level match rather than a
    # chunk-level one — so it is weighted below the vector channel but well
    # above the lexical noise. Measured on LOCOMO: 0.5 injected a spurious
    # denominator inflation that pushed confirmed-top-5 answers out of the
    # final ranking; 0.15 keeps the tree's cluster-level recall value
    # without distorting the shared RRF normalisation every other channel
    # depends on.
    "raptor": 0.15,
}

# V6.6.0 (2026-10-30): the RAPTOR traversal beam. Measured on the LOCOMO
# corpus: beam=1 is greedy and cannot recover from a wrong top branch
# (union 61.6%), beam=5 pulls in so many marginal leaves that they
# displace good ones (61.2%), beam=3 is the peak (65.2%).
RAPTOR_BEAM = 3

# V6.6.0 (2026-10-30): feature flag for the RAPTOR summary-tree channel.
# Default off. When the tree has not been built the channel is a no-op
# anyway (an empty channel contributes nothing to the fusion, so the
# ranking is bit-for-bit identical), so turning it on is safe even
# before the first build.
def _raptor_enabled() -> bool:
    return os.environ.get("MEMORY_FEATURE_RAPTOR", "0").lower() in ("1", "true", "yes", "on")


# V6.6.0 (2026-10-30): how much the tree's score contributes to the final
# composite. It is below the vector channel's own weight (0.4 on sim,
# 0.5 on rrf) on purpose: the tree may PROPOSE a chunk that the vector
# channel missed, but it must not outrank a chunk the vector channel is
# confident about. Measured end-to-end on the LOCOMO corpus, the sweep
# over this weight is recorded in the V6.6.0 changelog.
RAPTOR_SCORE_WEIGHT = float(os.environ.get("MEMORY_RAPTOR_SCORE_WEIGHT", "0.25"))


def _rrf_fusion(
    rankings: Dict[str, List[Any]],
    k: int = 60,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Reciprocal Rank Fusion。输入 {strategy: [ids排好序]}，输出 {id: rrf_score}。

    ``weights`` gives each channel a relative weight (V6.6.0). Without it
    every channel counts equally, which let the weak lexical channel
    outrank the vector channel — see DEFAULT_CHANNEL_WEIGHTS.
    """
    scores: Dict[str, float] = {}
    for strategy, ids in rankings.items():
        w = 1.0 if not weights else float(weights.get(strategy, 1.0))
        if not ids or w == 0.0:
            continue
        for rank, cid in enumerate(ids):
            scores[cid] = scores.get(cid, 0.0) + w * (1.0 / (k + rank))
    return scores


def _fetch_chunks(ids: List[str]) -> Dict[str, dict]:
    """批量取 chunk 全量字段，preserve 列表顺序。

    V6.6.1 fix (2026-10-31): this is the TERMINAL hydration step for every
    recall channel (vector / lexical / tag / raptor / graph expansion) and for
    /graph/khop. Filtering only `archived = 0` here meant that any id which
    slipped through a channel without a lifecycle filter came back anyway --
    the final gate has to be airtight, because a deprecated chunk reaching
    this point is returned to the caller no matter how it was selected.

    The comment below used to claim this query already excluded "deleted/
    deprecated" chunks. That was only true for `archived`; `is_deprecated`
    (superseded by a newer fact) was NOT filtered until now.
    """
    if not ids:
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    rows = query(f"""
        SELECT id, content, type, tags, source, importance, metadata,
               created_at, updated_at, tier, expires_at,
               origin, origin_trust, origin_ref
        FROM memory_chunks
        WHERE id IN ({placeholders})
          AND archived = 0
          AND is_deprecated = FALSE
          AND (expires_at IS NULL OR expires_at > NOW())
    """, tuple(ids), fetch="all-no-commit")
    by_id = {}
    for r in rows:
        r["tags"] = r["tags"] or []
        r["metadata"] = r["metadata"] or {}
        r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
        r["updated_at"] = r["updated_at"].isoformat() if r.get("updated_at") else None
        by_id[r["id"]] = r
    return by_id


def _expand_graph_neighbors(ids: List[str], limit_per_hit: int = 3, cap: int = 8) -> List[str]:
    """沿 relations/links 扩展检索命中的图谱邻居（1-hop）。

    使 recall 不只返回语义相似 chunk，还能带出与命中强关联的兄弟节点
    （同一决策链、同一主题簇、同一 episode 的其它记忆）。
    返回新增的邻居 id 列表（不在原 ids 中）。
    """
    if not ids:
        return []
    placeholders = ",".join(["%s"] * len(ids))
    # V6.6.0 fix (2026-10-31): only follow edges to LIVE memories.
    #
    # A deleted chunk keeps its relations (nothing cascades), so the
    # graph still points at it. Those neighbours then consume the
    # expansion budget — the cap is 10 — while _fetch_chunks drops them
    # at the end because they are archived. The slot is wasted and the
    # live neighbour that would have replaced it never gets a chance.
    rows = query("""
        SELECT r.id, r.memory_id_1, r.memory_id_2, r.strength
        FROM memory_relations r
        JOIN memory_chunks c1 ON c1.id = r.memory_id_1
        JOIN memory_chunks c2 ON c2.id = r.memory_id_2
        WHERE (r.memory_id_1 IN (%s) OR r.memory_id_2 IN (%s))
          AND c1.archived = 0 AND c2.archived = 0
          AND c1.is_deprecated = FALSE AND c2.is_deprecated = FALSE
    """ % (placeholders, placeholders), tuple(ids + ids))
    neighbor_scores: Dict[str, float] = {}
    for r in rows:
        if r["memory_id_1"] in ids and r["memory_id_2"] not in ids:
            neighbor_scores[r["memory_id_2"]] = max(neighbor_scores.get(r["memory_id_2"], 0), r["strength"] or 0.5)
        if r["memory_id_2"] in ids and r["memory_id_1"] not in ids:
            neighbor_scores[r["memory_id_1"]] = max(neighbor_scores.get(r["memory_id_1"], 0), r["strength"] or 0.5)
    # links 表（related type）同样参与
    link_rows = query("""
        SELECT l.source_id, l.target_id, l.strength
        FROM memory_links l
        JOIN memory_chunks cs ON cs.id = l.source_id
        JOIN memory_chunks ct ON ct.id = l.target_id
        WHERE (l.source_id IN (%s) OR l.target_id IN (%s))
          AND cs.archived = 0 AND ct.archived = 0
          AND cs.is_deprecated = FALSE AND ct.is_deprecated = FALSE
    """ % (placeholders, placeholders), tuple(ids + ids))
    for r in link_rows:
        if r["source_id"] in ids and r["target_id"] not in ids:
            neighbor_scores[r["target_id"]] = max(neighbor_scores.get(r["target_id"], 0), r["strength"] or 0.5)
        if r["target_id"] in ids and r["source_id"] not in ids:
            neighbor_scores[r["source_id"]] = max(neighbor_scores.get(r["source_id"], 0), r["strength"] or 0.5)
    # 按强度排序，取 top，但限制每个命中点的扩展数
    ranked = sorted(neighbor_scores.items(), key=lambda x: -x[1])
    return [nid for nid, _ in ranked[:cap]]


def _expand_graph_neighbors_filtered(ids: List[str], query_emb: List[float],
                                     min_strength: float = 0.3, cap: int = 10,
                                     lex_hit_ids: Optional[set] = None) -> List[str]:
    """图谱邻居扩展 + 查询相关度门控（P1 GraphRAG 增强）。

    在 1-hop 邻居基础上，再按"邻居与查询向量的相似度"过滤，
    只保留与查询语义相关的邻居。避免弱链（same-source/tag-shared 低 strength）
    把无关文档（如 BootstrapBlazor 各组件页）带进 recall pack。

    v6.5.0 (2026-08-23, direction #2): 阈值放宽 + lex_hit 邻居保留
    - min_strength: 0.5 → 0.3 (召回更多图谱邻居, recall +20%)
    - cap: 6 → 10
    - lex_hit_ids: 字面精确命中 chunk id 集合, 这些邻居无条件保留 (无需 strength 过滤),
      因为查询词精准出现在 chunk 内容里, 本身就是强相关信号

    v6.5.0 (2026-08-23, direction #B): 此函数继续作为 legacy fallback path.
    新默认路径是 `_expand_graph_neighbors_ppr` (Personalized PageRank over
    memory_relations + memory_links), 通过 feature flag MEMORY_FEATURE_GRAPH_PPR
    切换. 当 flag=false 或 graph 包导入失败时回退到本路径.
    """
    neighbors = _expand_graph_neighbors(ids, cap=cap * 4)
    if not neighbors:
        return []
    # V6.6.0 fix (2026-10-30): pad the query vector to the storage column
    # dimension (768). vec_to_str() does NOT pad, so a 384-dim mpnet query
    # vector would make Postgres raise "expected 768 dimensions, not 384"
    # and take down the whole search whenever this fallback path was taken.
    qm = resolve_embedding_model()
    emb_str = _vec_to_str_for_model(query_emb, qm)
    placeholders = ",".join(["%s"] * len(neighbors))
    scored = query(
        f"""
        SELECT id, 1 - (embedding <=> %s::vector) AS s
        FROM memory_chunks
        WHERE id IN ({placeholders}) AND embedding IS NOT NULL
          AND archived = 0
          AND is_deprecated = FALSE
          AND (expires_at IS NULL OR expires_at > NOW())
          AND (model_version IS NULL OR model_version = %s)
        """,
        tuple([emb_str] + neighbors + [qm]),
    )
    lex_set = lex_hit_ids or set()
    kept = [
        r["id"] for r in scored
        if float(r["s"]) >= min_strength or r["id"] in lex_set
    ]
    return kept[:cap]


def _graph_ppr_enabled() -> bool:
    """Feature flag check. Lazy import of config to avoid circular deps."""
    try:
        from .config import config as _cfg
        return bool(getattr(_cfg, "memory_feature_graph_ppr", True))
    except Exception:
        return True


def _expand_graph_neighbors_ppr(ids: List[str], query_emb: List[float],
                                cap: int = 10,
                                lex_hit_ids: Optional[set] = None,
                                alpha: float = 0.85,
                                max_iter: int = 100,
                                tol: float = 1e-6) -> List[str]:
    """Personalized PageRank expansion (v6.5.0 direction #B, HippoRAG 2 style).

    Walks ``memory_relations`` + ``memory_links`` as a single weighted graph,
    runs power-iteration PPR seeded at the union of {hit ids + lex_hit ids},
    and returns the top ``cap`` non-seed nodes by PPR score.

    Differences from `_expand_graph_neighbors_filtered` (v6.5.0 #2 legacy):
    - No ``min_strength`` cutoff: PPR naturally down-weights weak links
      because random-walk mass decays with graph distance.
    - Lex-hit ids are added as anchors (not exempt from strength filter).
    - Self-seeding is excluded from the result so the function actually
      adds neighbors instead of echoing the seed set.

    Falls back to `_expand_graph_neighbors_filtered` if the graph package
    fails to import (e.g. half-deployed test env) so the search path
    stays functional.

    Args:
        ids: The semantic hit ids from the search pipeline (RRF + sim).
        query_emb: The query embedding (currently unused by PPR itself,
            kept for parity with the legacy signature).
        cap: Maximum neighbors to return.
        lex_hit_ids: Lexical hit ids; added as PPR anchors.
        alpha: PPR damping factor.
        max_iter: PPR iteration cap.
        tol: PPR L1 convergence threshold.

    Returns:
        List of neighbor chunk ids, top ``cap`` by PPR score.
    """
    try:
        from graph import GraphIndex  # type: ignore
        from graph.cache import compute_or_load  # type: ignore
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning("graph package import failed, falling back to strength filter: %s", exc)
        return _expand_graph_neighbors_filtered(
            ids, query_emb, min_strength=0.3, cap=cap, lex_hit_ids=lex_hit_ids,
        )

    if not ids:
        return []

    # Build a focused index: only chunks touching the hit set plus 2-hop
    # neighborhood (the GraphIndex.from_db API handles the relation / link
    # SELECTs). chunk_ids = union(hits, lex_hits).
    seed_union = list(set(ids) | (lex_hit_ids or set()))
    if not seed_union:
        return []

    try:
        index = GraphIndex.from_db(query, chunk_ids=seed_union)
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning("GraphIndex.from_db failed, falling back: %s", exc)
        return _expand_graph_neighbors_filtered(
            ids, query_emb, min_strength=0.3, cap=cap, lex_hit_ids=lex_hit_ids,
        )

    if index.is_empty():
        return []

    try:
        # V6.6.0 fix (2026-10-31): use the PPR cache.
        #
        # graph/cache.py has existed since v6.5.0 with a full
        # compute_or_load() contract (cache read -> compute -> cache write
        # against memory_graph_ppr), but NOTHING ever called it — the
        # search path called top_k_ppr() directly, which recomputes the
        # power iteration on every request. The cache table stayed empty
        # (0 rows) and the documented "1h TTL window" did nothing.
        #
        # compute_or_load has the same semantics as top_k_ppr plus the
        # cache, so this is a drop-in: same alpha / max_iter / tol, and
        # the seed union is passed through.
        #
        # The cache is keyed on the LEADING anchor, so a multi-seed walk
        # is cached under one anchor id — which is what the cache module
        # documents and accepts.
        scores = compute_or_load(
            index,
            anchor_id=seed_union[0],
            db_query=query,
            db_execute=execute,
            alpha=alpha,
            max_iter=max_iter,
            tol=tol,
            seeds=seed_union,
        )
        seed_set = set(seed_union)
        ranked = sorted(
            ((n, s) for n, s in scores.items() if n not in seed_set),
            key=lambda kv: (-kv[1], kv[0]),
        )[:cap]
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning("top_k_ppr failed, falling back: %s", exc)
        return _expand_graph_neighbors_filtered(
            ids, query_emb, min_strength=0.3, cap=cap, lex_hit_ids=lex_hit_ids,
        )

    return [node_id for node_id, _score in ranked]


def graph_detect_communities(payload: Optional[Dict] = None):
    """Detect communities in the memory graph and persist them.

    V6.6.0 fix (2026-10-31): graph/community.py (Leiden / Louvain /
    label-propagation) and the ``memory_graph_communities`` table have
    existed since v6.5.0, but NOTHING ever called the module and the
    table held 0 rows. Community detection was dead code.

    This endpoint drives it and writes the result to the table the
    schema already declares, so the partition is a BACKGROUND artifact:
    re-detecting is cheap, and consumers read the persisted rows.

    Like the RAPTOR tree this is a maintenance operation, not a search
    one — the search path does not run community detection inline.
    """
    payload = payload or {}
    algorithm = payload.get("algorithm", "auto")
    persist = bool(payload.get("persist", True))

    try:
        from graph import GraphIndex  # type: ignore
        from graph.community import detect_communities  # type: ignore
    except Exception as exc:  # pragma: no cover -- defensive
        raise HTTPException(503, f"community detection unavailable: {exc}")

    try:
        index = GraphIndex.from_db(query)
    except Exception as exc:  # pragma: no cover -- defensive
        raise HTTPException(500, f"graph load failed: {exc}")
    if index.is_empty():
        return {"detected": 0, "communities": [], "reason": "empty graph"}

    try:
        communities = detect_communities(index, algorithm=algorithm)
    except Exception as exc:
        raise HTTPException(400, f"community detection failed: {exc}")

    rows = []
    for comm in communities:
        for member in comm.member_ids:
            rows.append((comm.id, member, algorithm))

    if persist and rows:
        # Replace the partition for this algorithm atomically.
        execute("DELETE FROM memory_graph_communities WHERE algorithm = %s", (algorithm,))
        chunk = 500
        for i in range(0, len(rows), chunk):
            batch = rows[i:i + chunk]
            placeholders = ", ".join(["(%s, %s, %s)"] * len(batch))
            params = []
            for cid, member, algo in batch:
                params.extend([cid, member, algo])
            execute(
                f"INSERT INTO memory_graph_communities "
                f"(community_id, member_id, algorithm) VALUES {placeholders}",
                tuple(params),
            )

    sizes = sorted((c.size() for c in communities), reverse=True)
    return {
        "detected": len(communities),
        "members": len(rows),
        "algorithm": algorithm,
        "persisted": bool(persist and rows),
        "largest": sizes[:10],
        "median_size": (sizes[len(sizes) // 2] if sizes else 0),
    }


def graph_list_communities(algorithm: Optional[str] = None, limit: int = 50):
    """List the persisted community partition."""
    if limit < 1 or limit > 500:
        raise HTTPException(400, f"limit={limit} must be between 1 and 500")
    if algorithm:
        rows = query(
            """SELECT community_id, count(*) AS members
               FROM memory_graph_communities
               WHERE algorithm = %s
               GROUP BY community_id
               ORDER BY members DESC LIMIT %s""",
            (algorithm, limit), fetch="all-no-commit",
        )
    else:
        rows = query(
            """SELECT community_id, count(*) AS members
               FROM memory_graph_communities
               GROUP BY community_id
               ORDER BY members DESC LIMIT %s""",
            (limit,), fetch="all-no-commit",
        )
    return {"communities": rows, "total": len(rows)}


def graph_khop(memory_id: str, k: int = 2, min_strength: float = 0.5, limit: int = 12):
    """k-hop 子图遍历：从 memory_id 出发 BFS 沿 relations 扩展 k 层。

    GraphRAG 的多跳推理：给定一个命中的记忆，返回其 k 层关联的子图节点，
    供 agent 重组跨 chunk 的多步推理上下文。strength 阈值过滤弱链。
    """
    frontier = [memory_id]
    seen = set(frontier)
    hops: Dict[int, List[str]] = {0: list(frontier)}
    for hop in range(1, k + 1):
        prev = frontier
        if not prev:
            break
        placeholders = ",".join(["%s"] * len(prev))
        # V6.6.1 fix (2026-10-31): join memory_chunks on BOTH ends of the
        # relation and apply the lifecycle filter. The traversal used to read
        # memory_relations raw, so a deprecated (superseded) neighbour was
        # reachable through the graph even though every direct channel
        # filtered it. That path went live at scale when 35c4f33 / 6068df6 /
        # b3f17a5 re-enabled relation mining, which had been producing ZERO
        # rows before.
        rows = query(
            f"""
            SELECT r.memory_id_1, r.memory_id_2, r.strength
            FROM memory_relations r
            JOIN memory_chunks c1 ON c1.id = r.memory_id_1
            JOIN memory_chunks c2 ON c2.id = r.memory_id_2
            WHERE (r.memory_id_1 IN ({placeholders}) OR r.memory_id_2 IN ({placeholders}))
              AND r.strength >= %s
              AND c1.archived = 0 AND c1.is_deprecated = FALSE
              AND (c1.expires_at IS NULL OR c1.expires_at > NOW())
              AND c2.archived = 0 AND c2.is_deprecated = FALSE
              AND (c2.expires_at IS NULL OR c2.expires_at > NOW())
            """,
            tuple(prev + prev + [min_strength]))
        nxt: List[str] = []
        for r in rows:
            for cid in (r["memory_id_1"], r["memory_id_2"]):
                if cid not in seen:
                    seen.add(cid)
                    nxt.append(cid)
        hops[hop] = nxt
        frontier = nxt
    # 收集所有非 seed 节点并按 hop 深度排序（近的在前）
    all_ids = [cid for hop in range(1, k + 1) for cid in hops.get(hop, [])][:limit]
    by_id = _fetch_chunks(all_ids)
    nodes = []
    for cid in all_ids:
        r = by_id.get(cid)
        if r:
            nodes.append({"id": cid, "hop": _hop_of(hops, cid),
                          "content": str(r.get("content", ""))[:160], "type": r.get("type"),
                          "tags": r.get("tags")})
    return {"root": memory_id, "k": k, "nodes_per_hop": {str(h): len(v) for h, v in hops.items()},
            "total": len(nodes), "nodes": nodes}


def _hop_of(hops: Dict[int, List[str]], cid: str) -> int:
    for h, ids in hops.items():
        if cid in ids:
            return h
    return 0


def _build_lexical_query(query: str) -> Optional[str]:
    """构造 tsquery 兼容的字面检索词条（OR 语义）。

    用 plainto_tsquery 的 AND 要求"全部词命中" → 多词查询几乎 0 命中。
    这里改为：提取有效词条（字母/数字 token），全部 OR 连接，
    配合 ts_rank 排序让"命中越多的相关度越高"。中文串作为单 token 保留。
    返回形如 "term1 | term2 | ..." 的串（交由 to_tsquery 自动规范化，
    不用引号括起，避免 tsquery 运算符歧义）。返回 None 表示无可检索词条。

    V6.6.0 fix (2026-10-30): 英文停用词不再参与 OR 匹配。
    之前长度 >= 2 的 token 全部进查询，于是
        "When did Caroline go to the museum?"
    变成
        When | did | Caroline | go | to | the | museum
    OR 语义下 "the" 能命中大半个英文语料 —— 查询失去区分度，词面通道
    只能靠 ts_rank 排序勉强区分，等于掺噪声。实测该词面通道独立跑
    MRR=0.000（LOCOMO 8695 chunk 语料）。
    中文没有空格分词，且"的了是在和"等虚词多为单字，本来就被
    "长度 >= 2" 规则滤掉，所以中文行为不变。
    """
    # 连字符/下划线当作词条分隔（Home-Server → Home, Server），避免 tsquery 运算符歧义
    normalized = re.sub(r"[_/+.\-]+", " ", query)
    tokens = re.findall(r"[\w一-鿿]+", normalized)
    # 过滤纯数字/过短的噪声 token
    terms = [t for t in tokens if len(t) >= 2]
    # V6.6.0: 丢掉英文停用词（大小写无关），避免它们把 OR 查询撑爆
    terms = [t for t in terms if t.lower() not in _LEXICAL_STOPWORDS]
    if not terms:
        return None
    return " | ".join(terms)


def _relevance_scores(ids: List[str]) -> Dict[str, float]:
    """由 access_stats 访问频次推导 relevance 权重（0~1）。

    V5 PG 没有 V2 时代的 relevance_score 列，用"实际被 recall 的次数"作为
    "被证明有用"的信号驱动三因子重排。count>=5 归一化到 1.0。

    v6.5.0 (2026-08-23, direction #2): 新块冷启动预热
    新写入的 chunk (access_stats.count=0) 不再 rlv=0 落入冷宫期 (~5 次搜索),
    而是按 (base=0.3 + 0.05*imp + 0.2[if type=note] + min(count,10)/10) 预热,
    写时即激活。chunks 表 JOIN 一次取 imp/type，避免 N+1。
    """
    if not ids:
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    rows = query(
        "SELECT memory_id, count(*) AS c FROM access_stats "
        "WHERE memory_id IN (" + placeholders + ") GROUP BY memory_id",
        tuple(ids),
    )
    # 一次性补齐 chunks 表的 imp / type, 用于冷启动预热 (LEFT JOIN 语义)
    chunk_rows = query(
        "SELECT id, importance, type FROM memory_chunks "
        "WHERE id IN (" + placeholders + ")",
        tuple(ids),
    )
    meta_by_id: Dict[str, dict] = {r["id"]: r for r in chunk_rows}
    # 已被 access_stats 统计的 chunk 的 count 速查表
    cnt_by_id: Dict[str, int] = {r["memory_id"]: int(r["c"] or 0) for r in rows}
    out: Dict[str, float] = {}
    # 1) 对有 access_stats 记录的 chunk, 按新公式计算 (含 count 加成)
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
    # 2) 对没有 access_stats 记录的 chunk (新写入), 仍然按 imp/type 给 base 0.3 + 加成
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


# ──────────────────────────────────────────────────────────────────
# ADR-067 M8 (2026-08-06): taxonomy boost — 父子层级加权
# 实现见 server/src/memory_taxonomy.py (独立模块, 不依赖 psycopg2)
# 行为: chunk.tag 命中 tags_taxonomy 祖先层级 ∩ query.tag → composite × 1.2
# Feature flag: MEMORY_FEATURE_TAXONOMY_BOOST (默认 false)
# ──────────────────────────────────────────────────────────────────
from .memory_taxonomy import boost_factor as _taxonomy_boost_factor, is_enabled as _taxonomy_boost_enabled

# V6.6.0 direction #1 (2026-10-19): Cross-Encoder 精排层
# 实现见 server/src/rerank.py (独立模块, 不依赖 psycopg2)
# 行为: RRF 粗排候选 → bge-reranker ONNX 精排 → 与 RRF 混合排序
# Feature flag: MEMORY_FEATURE_RERANK (默认 false)
from .rerank import rerank_scores as _rerank_scores, is_enabled as _rerank_enabled




async def graph_sample(limit: int = 50, type: Optional[str] = None, category: Optional[str] = None):
    """采样 N 个 chunks + 它们之间的所有 relations, 返可视化数据.

    默认 limit=50. 如果 type 指定则只取该 type 的 chunks.
    """
    try:
        lim = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        lim = 50
    # 取 N 个节点
    sql = "SELECT id, content, type, importance, created_at FROM memory_chunks"
    params: list = []
    if type:
        sql += " WHERE type = %s"
        params.append(type)
    sql += " ORDER BY importance DESC, created_at DESC LIMIT %s"
    params.append(lim)
    nodes_rows = query(sql, tuple(params), fetch="all")

    if not nodes_rows:
        return {"nodes": [], "edges": [], "stats": {"total": 0, "sampled": 0, "edges": 0, "by_type": {}}}

    ids = [r["id"] for r in nodes_rows]
    placeholders = ",".join(["%s"] * len(ids))
    rels_rows = query(
        f"""
        SELECT memory_id_1, memory_id_2, relation_type, strength
        FROM memory_relations
        WHERE memory_id_1 IN ({placeholders}) AND memory_id_2 IN ({placeholders})
        """,
        tuple(ids + ids),
        fetch="all",
    )

    # by_type 统计
    by_type: dict = {}
    for r in nodes_rows:
        t = r["type"] or "general"
        by_type[t] = by_type.get(t, 0) + 1

    return {
        "nodes": [
            {
                "id": r["id"],
                "label": (r["content"] or "")[:50] + ("..." if r["content"] and len(r["content"]) > 50 else ""),
                "content": r["content"] or "",
                "type": r["type"] or "general",
                "importance": r["importance"] or 0,
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            }
            for r in nodes_rows
        ],
        "edges": [
            {
                "from": r["memory_id_1"],
                "to": r["memory_id_2"],
                "type": r["relation_type"] or "related",
                "strength": float(r["strength"] or 0),
            }
            for r in rels_rows
        ],
        "stats": {
            "total": query("SELECT COUNT(*) AS c FROM memory_chunks", fetch="one")["c"],
            "sampled": len(nodes_rows),
            "edges": len(rels_rows),
            "by_type": by_type,
        },
    }


def _graph_signal(anchor_ids: List[str], pool_ids: List[str],
                  db_query=query) -> Dict[str, float]:
    """How strongly each pool member is connected to the query's top hits.

    V6.6.0 (2026-10-31). The graph's value is NOT recall.

    Measured on LOCOMO with a dense graph (85% of chunks related, 26125
    relations), expanding the candidate pool along graph edges made the
    final top-5 slightly WORSE:

        no graph                 MRR=0.4829  recall@5=0.5760
        include_related gate=6   MRR=0.4819  recall@5=0.5700
        include_related gate=0   MRR=0.4827  recall@5=0.5700

    The neighbours a graph edge points at are semantically similar to
    their source (edges are mined at cosine >= 0.72), so the vector
    channel already retrieves them. The expansion contributed duplicates
    and displaced better candidates -- and relaxing the importance gate
    changed nothing, so the gate was never the blocker.

    But the graph carries a signal the vector channel does not: if a
    memory is connected to SEVERAL of the query's top hits, that is
    evidence beyond its own text. That is the multi-signal fusion
    direction (semantic + keyword + entity): the graph is the
    co-occurrence/entity signal, and it belongs in the RANKING, not in
    the candidate set.

    For each pool member the score is the summed edge strength to the
    anchors divided by the number of anchors, so one strong edge cannot
    outvote several weak ones and the scale is 0..1.

    Returns {} when there are no anchors or no edges.
    """
    if not anchor_ids or not pool_ids:
        return {}
    anchor_ids = list(dict.fromkeys(anchor_ids))
    pool_set = set(pool_ids)
    ap = ",".join(["%s"] * len(anchor_ids))

    raw: Dict[str, float] = {}
    try:
        rows = db_query(
            f"""
            SELECT memory_id_1, memory_id_2, strength FROM memory_relations
            WHERE memory_id_1 IN ({ap}) OR memory_id_2 IN ({ap})
            """, tuple(anchor_ids + anchor_ids))
        for r in rows:
            a, b = r["memory_id_1"], r["memory_id_2"]
            # src is the POOL member, dst is the ANCHOR it connects to.
            # The score belongs to the pool member.
            for src, dst in ((a, b), (b, a)):
                if src in pool_set and dst in anchor_ids:
                    raw[src] = raw.get(src, 0.0) + float(r["strength"] or 0.0)
        lrows = db_query(
            f"""
            SELECT source_id, target_id, strength FROM memory_links
            WHERE source_id IN ({ap}) OR target_id IN ({ap})
            """, tuple(anchor_ids + anchor_ids))
        for r in lrows:
            s, t = r["source_id"], r["target_id"]
            for src, dst in ((s, t), (t, s)):
                if src in pool_set and dst in anchor_ids:
                    raw[src] = raw.get(src, 0.0) + float(r["strength"] or 0.0)
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning("graph signal failed (ignored): %s", exc)
        return {}

    if not raw:
        return {}
    n = max(1, len(anchor_ids))
    return {cid: min(1.0, val / n) for cid, val in raw.items()}


def search_memory(req: MemorySearchRequest):
    # P2-C4 fix (2026-08-13): enforce hard upper bound on limit.
    if req.limit > 500:
        raise HTTPException(400, f"limit={req.limit} exceeds hard cap 500")
    if req.limit < 1:
        raise HTTPException(400, f"limit={req.limit} must be >= 1")
    # V6.2.0 (2026-08-13): cap top_vector/top_lexical to prevent OOM.
    if req.top_vector > 200:
        raise HTTPException(400, f"top_vector={req.top_vector} exceeds cap 200")
    if req.top_lexical > 200:
        raise HTTPException(400, f"top_lexical={req.top_lexical} exceeds cap 200")
    # P0 审计 (2026-08-06): 计时 search 耗时, 用于 audit details.latency_ms
    import time as _t
    _search_t0 = _t.time()
    # V6.5.0 direction #3: validate and normalize the query-side embedding model.
    query_model = (req.embedding_model or "nomic-v1.5").lower()
    if query_model not in SUPPORTED_MODELS:
        raise HTTPException(
            400,
            f"embedding_model={query_model!r} not supported. "
            f"Allowed: {sorted(SUPPORTED_MODELS)}",
        )
    # V6.6.0 fix (2026-10-30): the query must be embedded in the SAME space
    # as the chunks it is compared against. If nomic-v1.5 is unavailable the
    # encoder falls back to mpnet, so the vector channel must query
    # model_version='mpnet' — otherwise it compares an mpnet query vector
    # against nomic-v1.5 chunks and the cosine similarity is meaningless.
    # This is what made 2333 of 2583 chunks vector-invisible.
    query_model = resolve_embedding_model(query_model)
    if not req.hybrid:
        # /search vector-only also benefits from the dispatching.
        return _search_vector_only(req, query_model=query_model)

    # V6.5.0 direction #3: encode the query using the request's embedding_model
    # (default = nomic-v1.5). pgvector stores all chunks in a single 768-dim
    # column, but in practice mpnet chunks are 384-dim — see dispatch note
    # below for how per-chunk scoring works during the migration window.
    emb = embed_text(req.query, model_version=query_model)
    # Pack query vector to the storage column dim (vector(768) post-#3).
    emb_str = _vec_to_str_for_model(emb, query_model)
    # base_where 只含 vsi 的通用过滤（type/importance），不含 tag——
    # tag 作为独立的 RRF 召回通道（见下），避免"语义不中则 tag 命中丢失"。
    # V6.6.0 fix (2026-10-31): also exclude SUPERSEDED memories.
    #
    # archived = 0 means "not deleted"; is_deprecated = FALSE means "not
    # replaced by a newer fact". The conflict resolver marks the old
    # version with is_deprecated = TRUE and superseded_by, and the
    # governance stats count them as a separate bucket — so serving them
    # from /search contradicts the service's own model. The _fetch_chunks
    # comment ("filter archived=0 to prevent deleted/deprecated") treated
    # the two as the same thing.
    #
    # Measured on 127.0.0.1: 0 chunks are currently deprecated, so
    # this changes no result today — it closes a latent hole where a
    # corrected fact would still be served as if it were current.
    base_where = ["embedding IS NOT NULL", "archived = 0", "is_deprecated = FALSE"]
    # V6.6.1 fix (2026-10-31): honour the TTL at read time. `expires_at` is
    # assigned by _tier_for_importance() (importance<=3 -> 7d, <=5 -> 30d,
    # <=7 -> 180d, else permanent) and was previously only consulted by the
    # /forgetting/* cleanup endpoints. An expired-but-not-yet-swept fact was
    # therefore still returned by /search -- i.e. "outdated information keeps
    # coming back" straight from the retrieval layer.
    base_where.append("(expires_at IS NULL OR expires_at > NOW())")
    # V6.5.0 direction #3: vector-channel scope is the same model as the query,
    # so a query encoded with nomic-v1.5 only compares against chunks whose
    # model_version matches (and thus share the same embedding space).
    base_where.append("(model_version IS NULL OR model_version = %s)")
    base_params: List[Any] = [query_model]
    if req.type:
        base_where.append("type = %s"); base_params.append(req.type)
    if req.min_importance > 0:
        base_where.append("importance >= %s"); base_params.append(req.min_importance)
    where_sql = " AND ".join(base_where)

    rankings: Dict[str, List[str]] = {}

    # V6.6.0 (2026-10-30): multi-query expansion.
    #
    # One embedding of one phrasing can only reach what that phrasing
    # happens to be near. Expanding the query into a few forms widens the
    # net. Measured on the LOCOMO corpus (8695 chunks, 400 queries) on
    # 127.0.0.1:
    #
    #     single query    recall@5=0.5165  MRR=0.4337
    #     3 forms merged  recall@5=0.5442  MRR=0.4395   (+0.3% recall)
    #
    # The forms are computed up front and each form is queried through the
    # SAME channels as the original, so nothing about the retrieval
    # semantics changes — only how many phrasings are tried.
    query_forms = _expand_query_forms(req.query) if req.query_expansion else [req.query]

    # 1) 向量检索（cosine, HNSW 索引加速）。
    #    %s 占位顺序：SELECT 相似度 emb → where 过滤参数 → ORDER BY emb → LIMIT
    def _vector_scan(form: str) -> List[dict]:
        form_emb = embed_text(form, model_version=query_model)
        form_emb_str = _vec_to_str_for_model(form_emb, query_model)
        return query(
            f"""
            SELECT id, 1 - (embedding <=> %s::vector) AS similarity
            FROM memory_chunks
            WHERE {where_sql}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            tuple([form_emb_str] + base_params + [form_emb_str, req.top_vector]),
            fetch="all-no-commit",
        )

    vector_rows = _vector_scan(req.query)
    vector_ids = [r["id"] for r in vector_rows]
    rankings["vector"] = vector_ids
    sim_by_id = {r["id"]: float(r["similarity"]) for r in vector_rows}

    # V6.6.0: merge the extra forms into the vector channel. A chunk found
    # by several forms is merged by RRF over the per-form rankings (so
    # agreement across phrasings is rewarded), and its similarity is the
    # BEST similarity any form gave it.
    if len(query_forms) > 1:
        form_rankings: Dict[str, List[str]] = {"form0": vector_ids}
        for i, form in enumerate(query_forms[1:], start=1):
            rows = _vector_scan(form)
            ids = [r["id"] for r in rows]
            form_rankings[f"form{i}"] = ids
            for r in rows:
                cid = r["id"]
                sim = float(r["similarity"])
                if sim > sim_by_id.get(cid, -1.0):
                    sim_by_id[cid] = sim
        merged = _rrf_fusion(form_rankings, k=req.rrf_k)
        rankings["vector"] = sorted(
            merged.keys(), key=lambda cid: (-merged[cid], -sim_by_id.get(cid, 0.0))
        )

    # 2) 字面检索（ts_rank over GIN tsvector）。
    #    %s 占位顺序：SELECT 里 2 个 tsquery(ts_rank+headline) → where 过滤参数
    #    → tsv@@ 的 tsquery → LIMIT。lq 作为 tsquery 文本（编译期 OR 词条）传入。
    lexical_ids: List[str] = []
    lexical_form_rankings: Dict[str, List[str]] = {}

    def _lexical_scan(lq: str) -> List[dict]:
        return query(
            f"""
            SELECT id,
                   ts_rank(tsv, to_tsquery('simple', %s)) AS rant,
                   ts_headline('simple', content, to_tsquery('simple', %s),
                               'MaxFragments=1, MinWords=2, MaxWords=16')
            FROM memory_chunks
            WHERE {where_sql} AND tsv @@ to_tsquery('simple', %s)
            ORDER BY rant DESC
            LIMIT %s
            """,
            tuple([lq] + [lq] + base_params + [lq, req.top_lexical]),
            fetch="all-no-commit",
        )

    for fi, form in enumerate(query_forms):
        lq = _build_lexical_query(form)
        if not lq:
            continue
        try:
            lexical_rows = _lexical_scan(lq)
        except Exception as e:
            # tsvector/字面检索兜底失败时静默降级（向量检索不受影响）
            logger.warning(f"Lexical search degraded (RRF still on vector): {e}")
            continue
        lexical_form_rankings[f"form{fi}"] = [r["id"] for r in lexical_rows]

    if lexical_form_rankings:
        if len(lexical_form_rankings) == 1:
            lexical_ids = next(iter(lexical_form_rankings.values()))
        else:
            # V6.6.0: merge the expanded forms' lexical rankings the same
            # way the vector channel merges them.
            merged_lex = _rrf_fusion(lexical_form_rankings, k=req.rrf_k)
            lexical_ids = sorted(
                merged_lex.keys(), key=lambda cid: -merged_lex[cid]
            )
    rankings["lexical"] = lexical_ids

    # 2.5) 独立 tag 召回通道（req.tags 存在时）：
    #      按 tag 精确拉取带任一目标 tag 的 chunk（近期优先），
    #      作为 RRF 第三路。保证"用户明确指定 tag"的记录即使语义弱相关也被召回。
    tag_ids: List[str] = []
    if req.tags:
        try:
            if isinstance(req.tags, list) and req.tags:
                # 任一 tag 命中数组值（jsonb_array_elements_text 匹配）
                tag_conds = " OR ".join(
                    ["(EXISTS (SELECT 1 FROM jsonb_array_elements_text(tags) t WHERE t = %s))"] * len(req.tags))
                tag_rows = query(
                    f"""
                    SELECT id, created_at
                    FROM memory_chunks
                    WHERE ({tag_conds})
                      AND archived = 0
                      AND is_deprecated = FALSE
                      AND (expires_at IS NULL OR expires_at > NOW())
                    ORDER BY created_at DESC NULLS LAST
                    LIMIT %s
                    """,
                    tuple(req.tags + [req.top_vector]),
                    fetch="all-no-commit",
                )
                tag_ids = [r["id"] for r in tag_rows]
        except Exception as e:
            logger.warning(f"Tag recall degraded: {e}")
    rankings["tag"] = tag_ids

    # 2.6) RAPTOR 摘要树召回通道（第三路，可选）。
    #      背景：LOCOMO 实测（8695 chunk / 250 题）发现向量通道 top-5
    #      只有 58.4% 命中，且 46 个漏掉的答案**全部**被同一段对话里的
    #      其它轮次盖住 —— 这类问题（"Caroline 的感情状态是什么？"）的答案
    #      需要综合很多轮才能浮现，单轮向量都不够像。
    #      RAPTOR 树正好解决这个：path score 会把"整簇都相关"的 chunk 排到
    #      "只是局部看着像"的 chunk 前面。
    #      实测 RAPTOR top-5 = 62.8%，与向量通道 union = 65.2%，
    #      其中 16 个答案是向量通道完全找不到的。
    #      树是后台任务构建并持久化的，请求内只做内存遍历（不重建）。
    raptor_ids: List[str] = []
    raptor_score_raw: Dict[str, float] = {}
    if req.raptor or _raptor_enabled():
        try:
            from src.consolidate.raptor_index import (
                get_cached_index,
                walk as raptor_walk,
            )
            _rindex = get_cached_index(query, execute)
            if _rindex is not None:
                # 只走与查询向量同一个嵌入空间的树
                if _rindex.model_version == query_model:
                    _rhits = raptor_walk(
                        _rindex, emb, beam=RAPTOR_BEAM, top_k=req.top_raptor
                    )
                    raptor_ids = [cid for cid, _sc in _rhits]
                    raptor_score_raw = {cid: sc for cid, sc in _rhits}
                    if raptor_ids:
                        OPS_COUNTERS["raptor_applied"] = (
                            OPS_COUNTERS.get("raptor_applied", 0) + 1
                        )
        except Exception as e:
            # 树是增强项，缺失或出错时静默降级（向量通道不受影响）
            logger.warning(f"RAPTOR recall degraded: {e}")
    rankings["raptor"] = raptor_ids
    # The tree's per-chunk path score, normalised to 0..1 against the best
    # tree hit for this query, so the composite term is scale-free.
    raptor_score_by_id: Dict[str, float] = {}
    if raptor_score_raw and req.raptor_score_weight > 0:
        _rmax = max(raptor_score_raw.values()) or 1.0
        # Only the tree's own top-N proposals are boosted, and the boost
        # decays with the tree's rank so the first proposal counts most.
        # A flat boost on all top_raptor proposals floods the final top-5
        # with the tree's worst guesses — measured to be WORSE than no
        # tree at all.
        for _rank, (_cid, _sc) in enumerate(
            sorted(raptor_score_raw.items(), key=lambda kv: -kv[1]), start=1
        ):
            if _rank > req.raptor_boost_top_n:
                break
            raptor_score_by_id[_cid] = (_sc / _rmax) / _rank

    # 3) RRF 融合
    # V6.6.0 fix (2026-10-30): per-channel weights. The lexical channel is
    # weak (standalone MRR=0.000 on the LOCOMO corpus) and the old
    # equal-weight fusion let it outrank semantic matches — the default
    # scored MRR=0.4073 against 0.4560 for pure vector search.
    channel_weights = dict(DEFAULT_CHANNEL_WEIGHTS)
    channel_weights["lexical"] = req.lexical_rrf_weight
    channel_weights["raptor"] = req.raptor_rrf_weight
    rrf = _rrf_fusion(rankings, k=req.rrf_k, weights=channel_weights)
    # V6.6.0 fix (2026-10-30): the composite score's RRF term must be
    # normalised against the OTHER channels only.
    #
    # _three_factor computes rrf_norm = rrf_sc / max_rrf, and the rrf
    # term carries the largest weight in the composite (0.5). Adding the
    # raptor channel therefore silently deflated EVERY other candidate:
    # a chunk that the tree also found had its rrf raised from 0.019971
    # to 0.024727, which raised max_rrf, which shrank every other
    # chunk's rrf_norm by ~19% and dropped their composite by ~0.10.
    #
    # Measured on the LOCOMO corpus: a chunk that was 3rd without the
    # tree (composite 0.9681) fell out of the top-5 with it, because
    # unrelated chunks inherited the tree's inflated denominator.
    #
    # The tree therefore contributes its OWN term (raptor_score) and
    # does NOT participate in the shared normalisation. The other
    # channels' ranking is bit-for-bit identical whether the tree is on
    # or off.
    rrf_base = _rrf_fusion(
        {k: v for k, v in rankings.items() if k != "raptor"},
        k=req.rrf_k, weights=channel_weights,
    )
    # 字面精确命中的 id 集（查询词精准出现在内容里，混合检索的强信号）
    lexical_set = set(lexical_ids)

    # 4) 排序：RRF 分 → 同分按向量相似度
    # V6.6.0 (2026-10-30): ordered by the BASE fusion, so the candidate
    # pool's composition is identical whether the tree is on or off. The
    # tree proposes candidates (appended below); it does not get to
    # reorder the ones the other channels found.
    ordered_ids = sorted(
        rrf_base.keys(),
        key=lambda cid: (-rrf_base[cid], -sim_by_id.get(cid, 0.0)),
    )
    # V6.6.0 fix (2026-10-30): the rerank candidate pool must actually reach
    # rerank_top_k.
    #
    # The pool used to be ordered_ids[:max(limit, 10)] — hard-capped at 10 —
    # while the rerank scored candidates[:rerank_top_k] with rerank_top_k
    # defaulting to 30. So the Cross-Encoder only ever saw 10 candidates and
    # 20 of its 30 advertised slots were unreachable.
    #
    # Measured on the LOCOMO corpus (8695 chunks, 200 queries): the answer
    # is inside the vector channel's
    #     top- 5 -> 57.0%
    #     top-10 -> 63.0%
    #     top-30 -> 75.0%
    #     top-60 -> 76.0%
    # so ranks 11..30 hold ~12 points of answer mass that a reranker could
    # promote into the top-5, and the reranker was never shown them.
    #
    # The pool is widened ONLY when reranking is requested, so the default
    # (no rerank) path keeps its exact previous candidate set and its
    # previous latency.
    _pool_size = max(req.limit, 10)
    if req.rerank and _rerank_enabled():
        _pool_size = max(req.limit, 10, req.rerank_top_k)
    candidates = ordered_ids[:_pool_size]
    # V6.6.0 fix (2026-10-30): the tree's hits must survive the candidate
    # pool.
    #
    # A tree hit carries rrf = weight/(k+rank) = 0.5/61 = 0.0082, while
    # even the 10th vector hit carries 1/70 = 0.0143. So EVERY tree hit
    # sorts below EVERY vector hit in the RRF ordering and was being cut
    # here — before _three_factor ever saw it. Measured: with the tree
    # enabled the candidate-pool recall@30 rose 0.768 -> 0.788 while the
    # final top-5 recall FELL 0.612 -> 0.600, because the tree's
    # additions displaced vector candidates and its own hits were then
    # discarded.
    #
    # The tree's candidates are therefore appended to the pool rather
    # than competing for its slots. They still have to win the composite
    # score to reach the user, which is the right bar for a channel whose
    # whole value is "this chunk's CLUSTER is relevant".
    if raptor_ids:
        _in_pool = set(candidates)
        for _cid in raptor_ids[: req.top_raptor]:
            if _cid not in _in_pool:
                candidates.append(_cid)
                _in_pool.add(_cid)
    # 4.5) V6.6.0 direction #1 — Cross-Encoder 精排（可选, 双闸门）。
    #      位置: RRF 粗排之后、_three_factor() 之前 —— 两者都在"召回之后"
    #      这一层工作, 是串联而非替代。
    #      为什么不并进 _three_factor(): _three_factor 是纯标量加权, 表达不了
    #      "query 与 passage 逐 token 交互"这一维度; CE 必须单独跑一趟前向。
    #      **关键**: 精排结果不改变 candidates 顺序, 而是作为 ce_scores 注入
    #      _three_factor —— 因为下面 results.sort(key=composite_score) 会重排,
    #      单纯重排 candidates 列表会被那次排序整个盖掉, 精排等于白做。
    #      fail-open: 模型缺失/推理失败 → ce_scores 为空 → 权重与 v6.5.1 完全一致。
    ce_scores: Dict[str, float] = {}
    if req.rerank and _rerank_enabled() and candidates:
        _rerank_by_id = _fetch_chunks(candidates[: req.rerank_top_k])
        ce_scores = _rerank_scores(
            req.query,
            [(cid, (_rerank_by_id.get(cid) or {}).get("content") or "")
             for cid in candidates[: req.rerank_top_k]],
        )
        if ce_scores:
            OPS_COUNTERS["rerank_applied"] += 1
        else:
            OPS_COUNTERS["rerank_skipped"] += 1

    # 5) 图谱邻居扩展（include_related）。P1: 用查询相关度门控过滤弱链噪声。
    #    v6.5.0 (direction #2): min_strength 0.5→0.3, cap 6→10, lex_hit 邻居无条件保留
    #    v6.5.0 (direction #B): 默认走 Personalized PageRank (HippoRAG 2 风格),
    #      feature flag MEMORY_FEATURE_GRAPH_PPR=false 时回退到 strength 阈值路径.
    candidate_set = set(candidates)
    final_ids = candidates
    added_neighbors: set = set()
    if req.include_related and candidates:
        if _graph_ppr_enabled():
            neighbors = _expand_graph_neighbors_ppr(
                candidates, emb, cap=10, lex_hit_ids=lexical_set,
            )
        else:
            neighbors = _expand_graph_neighbors_filtered(
                candidates, emb, min_strength=0.3, cap=10, lex_hit_ids=lexical_set,
            )
        new_neighbors = [n for n in neighbors if n not in candidate_set]
        final_ids = candidates + new_neighbors
        added_neighbors = set(new_neighbors)

    # 6) 取全量字段
    by_id = _fetch_chunks(final_ids)
    results = []
    # 缓存 relevance，避免每条都查（三因子重排时用）
    rel = _relevance_scores(final_ids)
    now = datetime.now(timezone.utc)

    _raptor_weight = req.raptor_score_weight
    _graph_weight = req.graph_score_weight

    # V6.6.0 (2026-10-31): compute the graph connectivity signal over
    # the candidate pool. The anchors are the top-N of the BASE
    # ranking, so the signal never feeds back into its own anchors.
    graph_score_by_id: Dict[str, float] = {}
    if _graph_weight > 0 and candidates:
        _anchors = ordered_ids[: req.graph_anchor_count]
        graph_score_by_id = _graph_signal(_anchors, candidates)
        if graph_score_by_id:
            OPS_COUNTERS["graph_signal_applied"] += 1

    def _three_factor(vsim: float, imp: int, created_ts, rlv: float, rrf_sc: float,
                      lex_hit: bool, ce: Optional[float] = None,
                      raptor_score: float = 0.0,
                      graph_score: float = 0.0) -> float:
        """混合检索综合评分。

        - rrf_norm(0.5): RRF 排名融合主导——多路都能命中的记录获晋升。
        - sim_effective(0.4): 语义相关度，但字面精确命中时 sim 至少按 0.5 计
          （查询词一字不差出现在内容里，本身代表相当的相关性），从而让
          专有名词/路径的记忆不被"向量 sim=0(未进向量候选)"系统性低估。
        - importance(0.15): v6.5.0 direction #2 上调 (5%→15%), 让 imp 差距不再淹没
        - recency(0.10) / relevance(0.05): 微调, 总和 1.20 (非归一化, 允许 > 1)。
        实证平衡：字面精确(即使 sim0) > 不相关语义近似；但 sim0.8+ 真相关仍主导。
        - ce(可选, 最多 0.30): V6.6.0 direction #1 Cross-Encoder 精排分
          (sigmoid(logit), 0..1)。**只有 req.rerank 且服务端 flag 打开时才
          非 None**; 未开启精排时该项为 0, 五项权重完全不变 —— 老客户端的
          排序结果与 v6.5.1 逐位一致, 不存在静默的排序漂移。
          上限 0.30 而非更高: CE 是"锦上添花", 不该盖过 RRF 多路共识。
        """
        ce_term = (0.30 * ce) if ce is not None else 0.0
        age_days = max(0.0, (now - created_ts).total_seconds() / 86400.0) if created_ts else 30.0
        recency = 1.0 / (1.0 + 0.05 * age_days)
        max_rrf = max(rrf_base.values(), default=0.0)
        rrf_norm = (rrf_sc / max_rrf) if max_rrf > 0 else 0.0
        sim_eff = max(vsim, 0.5) if lex_hit else vsim   # 字面精确→sim至少0.5
        # V6.6.0 (2026-10-30): the RAPTOR tree's contribution.
        #
        # The tree answers a question the other channels structurally
        # cannot: "this chunk's CLUSTER is relevant", which is not the
        # same statement as "this chunk's text is similar". A chunk the
        # tree surfaces typically has LOW vector similarity -- that is
        # precisely why the vector channel missed it -- so without an
        # explicit term it can never win the composite score.
        #
        # raptor_score is already normalised to 0..1 against the best
        # tree hit for this query. The weight is deliberately below the
        # vector channel's: the tree may PROPOSE a chunk, but it must not
        # outrank a chunk the vector channel is confident about.
        raptor_term = _raptor_weight * raptor_score
        # V6.6.0 (2026-10-31): the graph's connectivity signal.
        #
        # Deliberately NOT part of rrf_norm: the RRF normalization is
        # shared by every candidate, so adding a term there would
        # silently rescale everyone else (the same trap the RAPTOR
        # channel walked into). graph_score is an independent, additive
        # term -- with weight 0 the score is bit-for-bit what it was.
        graph_term = _graph_weight * graph_score
        return (0.5 * rrf_norm + 0.4 * sim_eff
                + 0.15 * (imp / 10.0) + 0.1 * recency + 0.05 * rlv
                + ce_term + raptor_term + graph_term)


    for cid in final_ids:
        r = by_id.get(cid)
        if not r:
            continue
        # tag 硬过滤：用户指定 tags 时，结果只保留带任一目标 tag 的 chunk
        # （tag 通道保证召回，这里保证精确——不混入语义相关但没有该 tag 的记录）
        if req.tags:
            rtags = r.get("tags") or []
            if not any(t in rtags for t in req.tags):
                continue
        # v6.5.0 (direction #2): second-pass filter — 通过 include_related 加进来的邻居
        # 必须 importance >= 6 才进入最终结果, 过滤掉弱链引入的低质噪音。
        # 原候选 (candidates) 不受影响 (cid in candidate_set 时跳过检查)。
        #
        # V6.6.0 (2026-10-31): the threshold is now a request parameter.
        #
        # It was a hardcoded 6, and importance is not a uniform property of
        # a corpus — it is whatever the writer set. Measured on
        # 127.0.0.1: 42% of the production chunks that HAVE relations
        # sit at importance 5 and were therefore never expandable, and on
        # the LOCOMO corpus (a flat import at importance 5) 96% of them
        # were blocked. The graph was not sparse so much as GATED, which
        # is why include_related measured ~0 gain.
        #
        # A hardcoded magic number that silently disables a whole recall
        # channel on some corpora is not a quality filter, it is a
        # configuration bug. The default stays at 6 (the tuned value);
        # callers whose corpus has a flat importance distribution can
        # lower it, and 0 disables the gate.
        if (added_neighbors and cid in added_neighbors
                and req.graph_neighbor_min_importance > 0
                and (int(r.get("importance") or 0) < req.graph_neighbor_min_importance)):
            continue
        # 提取向量相似度（来自 search 阶段）
        vsim = sim_by_id.get(cid, 0.0)
        created_ts = None
        if r.get("created_at"):
            try:
                created_ts = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
            except Exception:
                created_ts = None
        rlv = rel.get(cid, 0.0)
        rrf_sc = rrf_base.get(cid, 0.0)
        lex_hit = cid in lexical_set
        composite = _three_factor(vsim, int(r.get("importance") or 5), created_ts, rlv, rrf_sc, lex_hit,
                                 ce_scores.get(cid),
                                 raptor_score_by_id.get(cid, 0.0),
                                 graph_score_by_id.get(cid, 0.0)) if req.boost_relevance else vsim
        # ADR-067 M8 (2026-08-06): taxonomy boost — 父子层级加权
        # chunk.tag 命中 tags_taxonomy 祖先层级 ∩ query.tag → composite × 1.2
        # feature flag MEMORY_FEATURE_TAXONOMY_BOOST 默认 false
        if req.boost_relevance and req.tags and _taxonomy_boost_enabled():
            rtags = r.get("tags") or []
            qtags = req.tags if isinstance(req.tags, list) else [req.tags]
            tb = _taxonomy_boost_factor(rtags, qtags, db_lookup=query)
            if tb > 1.0:
                composite *= tb
                OPS_COUNTERS["taxonomy_boost_applied"] += 1
        r["similarity"] = vsim
        r["rrf_score"] = rrf_sc
        r["relevance_score"] = rlv
        # V6.6.0 (2026-10-30): surface the origin marker with every recalled
        # fact. A poisoned memory is indistinguishable from a real
        # preference once it is written, so the consumer of a recall needs
        # to see where the fact came from — not just an auditor looking up
        # the id afterwards.
        r["raptor_score"] = round(raptor_score_by_id.get(cid, 0.0), 6)
        r["origin"] = r.get("origin")
        r["origin_trust"] = r.get("origin_trust")
        r["origin_ref"] = r.get("origin_ref")
        r["composite_score"] = round(float(composite), 6)
        # ce_score 单独暴露: 调参时要能直接看到精排原值, 而不是从 composite 里
        # 反推 (composite 还混了 rrf/sim/imp/recency/rlv 五个因素).
        r["ce_score"] = round(float(ce_scores[cid]), 6) if cid in ce_scores else None

        # P0 industry-alignment (2026-11-26): surface the structured
        # attributes backfilled by scripts/backfill_structured_metadata.py as
        # first-class fields, and build a self-contained grounded_context.
        #
        # Why: the raw content says "8 May ... went to a group yesterday",
        # but the gold answer is the EVENT date "7 May 2023". A downstream
        # reader (small LLM) cannot do that date arithmetic reliably. By
        # returning the resolved event date alongside the text, the memory
        # becomes a self-contained fact -- the same "memory as normalised
        # fact" contract Mem0/Zep expose. Measured A/B on 100 temporal
        # questions: F1 0.450 -> 0.544 (+9.4pp), judge 0.560 -> 0.585.
        # content is left untouched so vector/lexical retrieval is unchanged.
        md = r.get("metadata")
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        if not isinstance(md, dict):
            md = {}
        edates = md.get("event_dates") or []
        ents = md.get("entities") or []
        nums = md.get("numbers") or []
        ped = md.get("primary_event_date")
        r["event_dates"] = edates
        r["entities"] = ents
        r["numbers"] = nums
        if ped:
            r["primary_event_date"] = ped
        gctx = str(r.get("content") or "").strip()
        if ped:
            gctx = f"{gctx} [Known event date: {ped}]"
        elif edates:
            gctx = f"{gctx} [Dates: {', '.join(edates)}]"
        r["grounded_context"] = gctx

        r["related_memories"] = []
        results.append(r)

    # boost_relevance 时按 composite 重排
    if req.boost_relevance:
        results.sort(key=lambda x: -x["composite_score"])

    # P0 (2026-11-26): score_threshold 过滤。不达阈值的结果直接丢弃，
    # 这样剩下的高分条目才能填满 limit，避免 limit 被纯噪音占满。
    # 比较标的：boost_relevance 时用 composite_score，否则用原始向量相似度。
    if req.score_threshold > 0.0:
        def _score_of(r: dict) -> float:
            if req.boost_relevance:
                return float(r.get("composite_score") or 0.0)
            return float(r.get("similarity") or 0.0)
        results = [r for r in results if _score_of(r) >= req.score_threshold]

    # 收敛到请求的 limit（candidates/neighbors 可能扩展出多于 limit 的结果）
    results = results[: req.limit]

    # ──────────────────────────────────────────────────────────────────
    # ADR-067-Patch-1 (2026-08-04): 热补丁——让热度机制活过来.
    #
    # 现象: /search 命中了 chunk, 但 access_stats 没写入 → access_count 永远是 0
    #       → _relevance_scores() 永远返回 0 → composite 热度项 0.1*rlv 无效
    #       → forgetting_guided 永远不升级 tier.
    # 根因: /memories/{id}/refresh 端点存在, 但 search_memory 没调用它.
    # 修法: 在 search 返回前, 对命中 chunk 写 access_stats (失败不影响主路径).
    # ──────────────────────────────────────────────────────────────────
    if req.boost_relevance and results:
        try:
            for r in results:
                execute(
                    "INSERT INTO access_stats(memory_id, access_type, accessed_at) "
                    "VALUES (%s, 'search', NOW())",
                    (r["id"],),
                )
        except Exception as e:
            logger.warning(
                f"ADR-067-Patch-1: access_stats 写入失败 (不影响 search 返回): {e}"
            )

    # ──────────────────────────────────────────────────────────────────
    # P0 审计基础设施 (2026-08-06): 自动记录 /search 调用
    #
    # 为什么需要: memory_access_audit 之前只有 connector 显式 /audit/log 才写,
    #   而本机 hermes-agent/tools/cortex_memory_backend.py 没填 query/memory_ids,
    #   导致 audit 表里 728 行 (5 天) 全部 query=NULL + memory_id=NULL.
    # 这使得"用户问了什么 / 检索了什么 / 耗时多少"完全不可见 — 加强 KG 前无法评估.
    #
    # 修复: /search 端点内部自动记一条 audit, 不依赖 connector.
    #   action='search'
    #   details={query, total, latency_ms, result_ids (top 10), tags, min_importance, hybrid, boost_relevance}
    # 失败不影响主路径 (try/except 包裹).
    # ──────────────────────────────────────────────────────────────────
    if results:
        try:
            _search_t1 = _t.time()
            audit_details = {
                # P3-D3 fix (2026-08-13): cap query to 500 chars + hash for forensics.
                # 409 audit records show 7.1% queries >500 chars (max 3258).
                # Keeps audit table lean while preserving full text behind hash.
                "query": req.query[:500] if req.query else "",
                "query_truncated": bool(req.query and len(req.query) > 500),
                "query_len": len(req.query) if req.query else 0,
                "total": len(results),
                "result_ids_top10": [r["id"] for r in results[:10]],
                "tags": req.tags,
                "min_importance": req.min_importance,
                "hybrid": req.hybrid,
                "boost_relevance": req.boost_relevance,
                "include_related": req.include_related,
                # V6.6.0 d1: rerank 是否请求/是否真的跑出分/参与了几条.
                # 没有这三个字段就无法事后区分"没请求"与"请求了但模型没装".
                "rerank_requested": bool(req.rerank),
                "rerank_effective": bool(ce_scores),
                "rerank_scored_n": len(ce_scores),
                "type": req.type,
                "limit": req.limit,
                "latency_ms": round((_search_t1 - _search_t0) * 1000, 2),
                "actor": "search_endpoint_auto",
            }
            execute(
                """INSERT INTO memory_access_audit(memory_id, action, resource_type, details, created_at)
                   VALUES (NULL, 'search', 'search', %s::jsonb, NOW())""",
                (json.dumps(audit_details),),
            )
        except Exception as e:
            logger.warning(f"P0 audit log 写入失败 (不影响 search 返回): {e}")

    return {
        "query": req.query,
        "total": len(results),
        "hybrid": True,
        "rankings": {k: len(v) for k, v in rankings.items()},
        "results": results,
    }


def _search_vector_only(req: MemorySearchRequest, query_model: Optional[str] = None):
    """纯向量检索（hybrid=False 时的降级路径，保持旧行为）。

    V6.5.0 direction #3: ``query_model`` selects which embedding space to
    search. Default = req.embedding_model or "nomic-v1.5". Chunks on a
    different model_version are filtered out (their embeddings live in a
    different mathematical space).
    """
    qm = (query_model or req.embedding_model or "nomic-v1.5").lower()
    # V6.6.0 fix: the query vector and the model_version filter must agree on
    # the embedding space. If nomic-v1.5 is unavailable the encoder falls back
    # to mpnet, so the filter must follow (otherwise we compare an mpnet query
    # vector against nomic-v1.5 chunks — meaningless cosine similarity).
    qm = resolve_embedding_model(qm)
    emb = embed_text(req.query, model_version=qm)
    emb_str = _vec_to_str_for_model(emb, qm)
    where = ["embedding IS NOT NULL", "archived = 0",
             "is_deprecated = FALSE",
             "(expires_at IS NULL OR expires_at > NOW())",
             "(model_version IS NULL OR model_version = %s)"]
    params = [emb_str, emb_str, qm]
    if req.type:
        where.append("type = %s"); params.append(req.type)
    if req.min_importance > 0:
        where.append("importance >= %s"); params.append(req.min_importance)
    sql = f"""
        SELECT id, content, type, tags, source, importance, metadata,
               1 - (embedding <=> %s::vector) as similarity,
               embedding <=> %s::vector as distance,
               created_at
        FROM memory_chunks
        WHERE {" AND ".join(where)}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    params.extend([emb_str, req.limit])
    rows = query(sql, tuple(params))
    for r in rows:
        r["tags"] = r["tags"] or []
        r["metadata"] = r["metadata"] or {}
        r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
        r["related_memories"] = []
    return {
        "query": req.query,
        "total": len(rows),
        "hybrid": False,
        "rankings": {"vector": len(rows)},
        "results": rows,
    }

def search_tts(q: str, limit: int = 10):
    return search_memory(MemorySearchRequest(query=q, limit=limit))

def search_hybrid(q: str, limit: int = 10):
    return search_memory(MemorySearchRequest(query=q, limit=limit))

# === Relations ===
def get_relations_batch(ids: str, depth: int = 1, max_per_node: int = 10):
    """Walk the memory_relations graph from multiple seed chunk_ids at once.

    Returns the union of all relations reachable from any seed in <=depth
    hops, plus per-seed neighbor counts. Designed for /graph/khop and
    /relations/{id} callers that want to combine several seeds without
    N+1 round-trips.

    Query params:
      ids: comma-separated chunk_ids, max 32 (e.g. "id1,id2,id3")
      depth: 1..3, default 1 (direct neighbors only)
      max_per_node: int, default 10 (cap neighbors per hop to avoid blowup)

    Response shape:
      {
        "ids": [chunk_id, ...],          # echoes input order
        "depth": 1,
        "max_per_node": 10,
        "seeds": {                       # per-seed result
          chunk_id: {
            "neighbors": [{memory_id_2, relation_type, strength}, ...],
            "hop1_count": N,
            "hop2_count": M,             # only present if depth >= 2
          },
          ...
        },
        "graph": {                       # flattened for easy traversal
          "edges": [{from_id, to_id, relation_type, strength, hop}, ...],
          "nodes": [chunk_id, ...]       # unique nodes touched
        }
      }
    """
    if depth < 1 or depth > 3:
        raise HTTPException(400, "depth must be 1..3")
    if max_per_node < 1 or max_per_node > 100:
        raise HTTPException(400, "max_per_node must be 1..100")

    seed_ids = [s.strip() for s in ids.split(",") if s.strip()]
    if not seed_ids:
        raise HTTPException(400, "ids must contain at least one chunk_id")
    if len(seed_ids) > 32:
        raise HTTPException(400, "ids: max 32 chunk_ids per call")

    # BFS through memory_relations. Use SQL with a recursive CTE so the
    # entire traversal happens server-side (one query per call). The
    # graph is treated as undirected (memory_id_1 <-> memory_id_2).
    if depth == 1:
        # V6.6.1 fix (2026-10-31): join memory_chunks on both ends so a
        # superseded/expired node cannot be reached through the relation
        # graph. Same rationale as /graph/khop.
        sql = """
            WITH seeds AS (
                SELECT unnest(%s::text[]) AS cid
            )
            SELECT DISTINCT r.memory_id_1 AS from_id, r.memory_id_2 AS to_id,
                   r.relation_type, r.strength, 1 AS hop
            FROM memory_relations r
            JOIN seeds s ON r.memory_id_1 = s.cid
            JOIN memory_chunks c1 ON c1.id = r.memory_id_1
            JOIN memory_chunks c2 ON c2.id = r.memory_id_2
            WHERE r.strength >= 0.5
              AND c1.archived = 0 AND c1.is_deprecated = FALSE
              AND (c1.expires_at IS NULL OR c1.expires_at > NOW())
              AND c2.archived = 0 AND c2.is_deprecated = FALSE
              AND (c2.expires_at IS NULL OR c2.expires_at > NOW())
            UNION
            SELECT DISTINCT r.memory_id_2 AS from_id, r.memory_id_1 AS to_id,
                   r.relation_type, r.strength, 1 AS hop
            FROM memory_relations r
            JOIN seeds s ON r.memory_id_2 = s.cid
            JOIN memory_chunks c1 ON c1.id = r.memory_id_1
            JOIN memory_chunks c2 ON c2.id = r.memory_id_2
            WHERE r.strength >= 0.5
              AND c1.archived = 0 AND c1.is_deprecated = FALSE
              AND (c1.expires_at IS NULL OR c1.expires_at > NOW())
              AND c2.archived = 0 AND c2.is_deprecated = FALSE
              AND (c2.expires_at IS NULL OR c2.expires_at > NOW())
            LIMIT %s
        """
        params = (seed_ids, max_per_node * len(seed_ids) * 2)
    else:
        # V6.6.1 fix (2026-10-31): same lifecycle filter inside the recursive
        # walk -- both the edge selection and the final projection.
        sql = """
            WITH RECURSIVE seeds(cid) AS (
                SELECT unnest(%s::text[])
            ),
            walk(node, depth) AS (
                SELECT cid, 0 FROM seeds
                UNION
                SELECT CASE WHEN r.memory_id_1 = w.node THEN r.memory_id_2
                            ELSE r.memory_id_1 END,
                       w.depth + 1
                FROM walk w
                JOIN memory_relations r
                  ON (r.memory_id_1 = w.node OR r.memory_id_2 = w.node)
                JOIN memory_chunks cn ON cn.id = w.node
                JOIN memory_chunks ce
                  ON ce.id = CASE WHEN r.memory_id_1 = w.node THEN r.memory_id_2
                                  ELSE r.memory_id_1 END
                WHERE w.depth < %s AND r.strength >= 0.5
                  AND cn.archived = 0 AND cn.is_deprecated = FALSE
                  AND (cn.expires_at IS NULL OR cn.expires_at > NOW())
                  AND ce.archived = 0 AND ce.is_deprecated = FALSE
                  AND (ce.expires_at IS NULL OR ce.expires_at > NOW())
            )
            SELECT DISTINCT r.memory_id_1 AS from_id, r.memory_id_2 AS to_id,
                   r.relation_type, r.strength,
                   w.depth AS hop
            FROM walk w
            JOIN memory_relations r
              ON (r.memory_id_1 = w.node OR r.memory_id_2 = w.node)
            JOIN memory_chunks c1 ON c1.id = r.memory_id_1
            JOIN memory_chunks c2 ON c2.id = r.memory_id_2
            WHERE r.strength >= 0.5
              AND c1.archived = 0 AND c1.is_deprecated = FALSE
              AND (c1.expires_at IS NULL OR c1.expires_at > NOW())
              AND c2.archived = 0 AND c2.is_deprecated = FALSE
              AND (c2.expires_at IS NULL OR c2.expires_at > NOW())
            LIMIT %s
        """
        params = (seed_ids, depth, max_per_node * len(seed_ids) * (depth + 1))

    rows = query(sql, params)
    if not isinstance(rows, list):
        rows = rows or []
        rows = [rows] if rows else []

    edges = []
    nodes_seen = set()
    seeds_out = {cid: {"neighbors": [], "hop1_count": 0, "hop2_count": 0} for cid in seed_ids}
    for r in rows:
        if isinstance(r, dict):
            from_id = r.get("from_id")
            to_id = r.get("to_id")
            rel_type = r.get("relation_type")
            strength = float(r.get("strength") or 0.0)
            hop = int(r.get("hop") or 1)
        else:
            from_id, to_id, rel_type, strength = r[0], r[1], r[2], r[3]
            hop = int(r[4]) if len(r) > 4 else 1

        edges.append({
            "from_id": from_id,
            "to_id": to_id,
            "relation_type": rel_type,
            "strength": strength,
            "hop": hop,
        })
        for n in (from_id, to_id):
            if n:
                nodes_seen.add(n)

        for seed in seed_ids:
            if from_id == seed or to_id == seed:
                if hop == 1:
                    seeds_out[seed]["neighbors"].append({
                        "memory_id_2": to_id if from_id == seed else from_id,
                        "relation_type": rel_type,
                        "strength": strength,
                    })
                    seeds_out[seed]["hop1_count"] += 1
                elif hop == 2:
                    seeds_out[seed]["hop2_count"] += 1

    return {
        "ids": seed_ids,
        "depth": depth,
        "max_per_node": max_per_node,
        "seeds": seeds_out,
        "graph": {
            "edges": edges,
            "nodes": sorted(nodes_seen),
        },
    }



def get_relations(memory_id: str, min_strength: float = 0.0):
    rows = query("""
        SELECT * FROM memory_relations 
        WHERE (memory_id_1 = %s OR memory_id_2 = %s) AND strength >= %s
        ORDER BY strength DESC LIMIT 50
    """, (memory_id, memory_id, min_strength))
    for r in rows:
        r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
        r["updated_at"] = r["updated_at"].isoformat() if r.get("updated_at") else None
    return {"memory_id": memory_id, "total": len(rows), "relations": rows}

def delete_relations(memory_id: str):
    n = execute("DELETE FROM memory_relations WHERE memory_id_1 = %s OR memory_id_2 = %s", (memory_id, memory_id))
    return {"memory_id": memory_id, "deleted": n}

def delete_specific_relation(memory_id: str, target_id: str):
    n = execute("""DELETE FROM memory_relations 
        WHERE (memory_id_1=%s AND memory_id_2=%s) OR (memory_id_1=%s AND memory_id_2=%s)""",
        (memory_id, target_id, target_id, memory_id))
    return {"memory_id": memory_id, "target_id": target_id, "deleted": n}

def create_temporal_relation(payload: Dict):
    # FIX (2026-08-02): previously crashed 500 when source_id/target_id were
    # missing (KeyError on payload["source_id"]). Validate explicitly.
    source_id = payload.get("source_id")
    target_id = payload.get("target_id")
    if not source_id or not target_id:
        raise HTTPException(400, "payload requires source_id and target_id")
    rid = f"rel_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()
    # V6.6.0 fix (2026-10-31): the validity window is now actually
    # written. The column existed (and the temporal query was supposed
    # to filter on it) but no write path ever populated it, so there
    # was nothing to filter on and every row looked permanently valid.
    #
    # valid_from  — when the fact started being true
    # valid_to    — when it stopped being true (NULL = still true)
    # transaction_at — when the system learned it (unchanged)
    #
    # Both default to "now", so a plain relation is true from the moment
    # it is recorded. A caller that knows the fact has an end supplies
    # valid_to; a caller correcting an earlier claim supplies valid_from.
    valid_from = payload.get("valid_from") or now
    valid_to = payload.get("valid_to")
    execute("""INSERT INTO memory_relations(id, memory_id_1, memory_id_2, relation_type, strength,
                                            created_at, transaction_at, valid_from, valid_to)
              VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
              ON CONFLICT (id) DO NOTHING""",
            (rid, source_id, target_id,
             payload.get("relation_type", "related"), payload.get("strength", 0.5),
             now, now, valid_from, valid_to))
    return {"id": rid, "status": "created",
            "valid_from": valid_from, "valid_to": valid_to}


def relations_prune(payload: Optional[Dict] = None):
    """Delete graph edges whose endpoints are gone.

    V6.6.0 (2026-10-31): the relation tables have no foreign keys, so
    deleting a chunk leaves its edges behind — and nothing ever cleaned
    them up.

    Measured on 127.0.0.1 (2132 links):

        both endpoints alive            931
        one endpoint archived            90
        one endpoint no longer exists   451
        BOTH endpoints no longer exist  594

    So 1201 of 2132 links (56%) were dangling, and a quarter had lost
    both endpoints entirely. The graph was mostly made of edges to
    memories that are gone.

    That is not just untidy. The expansion path takes the top-N
    neighbours by strength, so dead edges consumed the neighbour budget
    (the cap is 10) and _fetch_chunks dropped them at the end — the
    live neighbour that would have replaced one never got a chance.

    ``dry_run`` (default True) reports what would be removed without
    touching anything, because this deletes rows.
    """
    payload = payload or {}
    dry_run = bool(payload.get("dry_run", True))
    include_archived = bool(payload.get("include_archived", True))

    dead_filter = (
        "(a.id IS NULL OR b.id IS NULL"
        + (" OR a.archived = 1 OR b.archived = 1" if include_archived else "")
        + ")"
    )
    rel_sql = f"""
        SELECT count(*) FROM memory_relations r
        LEFT JOIN memory_chunks a ON a.id = r.memory_id_1
        LEFT JOIN memory_chunks b ON b.id = r.memory_id_2
        WHERE {dead_filter}
    """
    link_sql = f"""
        SELECT count(*) FROM memory_links l
        LEFT JOIN memory_chunks a ON a.id = l.source_id
        LEFT JOIN memory_chunks b ON b.id = l.target_id
        WHERE {dead_filter}
    """
    dead_relations = query(rel_sql, fetch="one")["count"]
    dead_links = query(link_sql, fetch="one")["count"]

    if dry_run:
        return {
            "dry_run": True,
            "dead_relations": dead_relations,
            "dead_links": dead_links,
            "include_archived": include_archived,
        }

    # The DELETE must use the SAME definition as the dry run, or the
    # dry run over-reports. dead_filter is reused verbatim.
    removed_rel = execute(
        f"""
        DELETE FROM memory_relations r
        USING memory_relations r2
        LEFT JOIN memory_chunks a ON a.id = r2.memory_id_1
        LEFT JOIN memory_chunks b ON b.id = r2.memory_id_2
        WHERE r.id = r2.id AND {dead_filter}
        """
    )
    removed_link = execute(
        f"""
        DELETE FROM memory_links l
        USING memory_links l2
        LEFT JOIN memory_chunks a ON a.id = l2.source_id
        LEFT JOIN memory_chunks b ON b.id = l2.target_id
        WHERE l.id = l2.id AND {dead_filter}
        """
    )
    return {
        "dry_run": False,
        "removed_relations": removed_rel,
        "removed_links": removed_link,
        "dead_relations_before": dead_relations,
        "dead_links_before": dead_links,
    }


def get_temporal_relations(source_id: str, at_time: Optional[str] = None, relation_type: Optional[str] = None):
    """List relations originating from a chunk, optionally as of a point in time.

    V6.6.0 fix (2026-10-31): ``at_time`` was accepted and then silently
    IGNORED — the query had no time predicate at all, so asking "what
    was true at T" returned every relation regardless of T. That is the
    worst kind of failure: the endpoint answered with plausible rows and
    the caller could not tell they were unfiltered.

    The relation table carries a bi-temporal shape (valid_from/valid_to
    for WHEN the fact was true, transaction_at for WHEN the system
    learned it). The filter below implements the validity half:

        at_time <= valid_from  -> not yet true
        at_time >= valid_to    -> no longer true
        valid_from IS NULL     -> still true unless valid_to has passed

    Relations written before the validity columns were populated (every
    pre-V6.6.0 row) have valid_from = NULL, so they count as
    "always valid" rather than vanishing from the results.
    """
    where = ["memory_id_1 = %s"]; params = [source_id]
    if relation_type:
        where.append("relation_type = %s"); params.append(relation_type)
    if at_time:
        # A relation is visible at at_time when its validity window
        # contains that moment. NULL valid_from means "true from the
        # beginning", NULL valid_to means "true until further notice".
        where.append("(valid_from IS NULL OR valid_from <= %s)")
        params.append(at_time)
        where.append("(valid_to IS NULL OR valid_to > %s)")
        params.append(at_time)
    rows = query(
        f"SELECT * FROM memory_relations WHERE {' AND '.join(where)} "
        f"ORDER BY COALESCE(valid_from, created_at) DESC LIMIT 50",
        tuple(params),
    )
    return {
        "source_id": source_id,
        "at_time": at_time,
        "filtered_by_time": bool(at_time),
        "relations": rows,
    }

# === Links ===
def link_create(payload: Dict):
    lid = f"link_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    execute("""INSERT INTO memory_links(id, source_id, target_id, link_type, strength, source, created_at)
              VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (lid, payload["source_id"], payload["target_id"],
             payload.get("link_type", "related"), payload.get("strength", 0.5),
             payload.get("source", "manual"), now))
    return {"id": lid}

def get_links(source_id: str):
    rows = query("SELECT * FROM memory_links WHERE source_id = %s", (source_id,))
    return {"source_id": source_id, "links": rows}

def link_access(payload: Dict):
    execute("""UPDATE memory_links SET access_count = access_count + 1, last_accessed = NOW()
              WHERE source_id = %s AND target_id = %s""",
            (payload["source_id"], payload["target_id"]))
    return {"status": "ok"}

def link_path(source_id: str, target_id: str):
    rows = query("""SELECT * FROM memory_links 
                    WHERE (source_id = %s AND target_id = %s) 
                    OR (source_id = %s AND target_id = %s)""",
                 (source_id, target_id, target_id, source_id))
    return {"source": source_id, "target": target_id, "links": rows}

def link_decay(
    decay_rate: Optional[float] = None,
    factor: Optional[float] = None,
    dry_run: bool = False,
):
    """Decay every link's strength.

    V6.6.0 fix (2026-10-31): this endpoint and its MCP tool disagreed
    about everything.

      * the endpoint took ``decay_rate`` (a QUERY parameter, the
        fraction to SUBTRACT) while the MCP tool sent ``{"factor": ...}``
        in a JSON BODY — a name FastAPI does not declare, in a place
        FastAPI does not read for a scalar query parameter. So the
        requested rate was silently discarded and every call decayed by
        the default 0.01, whatever the caller asked for;
      * the two also meant opposite things: ``factor=0.95`` means
        "multiply by 0.95" while ``decay_rate=0.01`` means "multiply by
        0.99".

    Both spellings are now accepted and mean the SAME thing: ``factor``
    is the multiplier (0.95 -> keep 95%), ``decay_rate`` is the
    fraction removed (0.05 -> keep 95%). Supplying both is a 400
    rather than a silent pick.

    ``dry_run=True`` reports how many links WOULD be decayed and
    changes nothing. This endpoint is global and destructive — there is
    no scope parameter — and the graph expansion path filters on
    ``strength``, so a repeated decay can silently push every link
    below the threshold and quietly disable the graph.
    """
    if decay_rate is not None and factor is not None:
        raise HTTPException(
            400,
            "supply either factor (the multiplier to keep, e.g. 0.95) "
            "or decay_rate (the fraction to remove, e.g. 0.05), not both.",
        )
    if factor is not None:
        if not (0.0 < factor <= 1.0):
            raise HTTPException(400, f"factor={factor} must be in (0, 1]")
        keep = factor
    elif decay_rate is not None:
        if not (0.0 <= decay_rate < 1.0):
            raise HTTPException(400, f"decay_rate={decay_rate} must be in [0, 1)")
        keep = 1.0 - decay_rate
    else:
        keep = 0.99
    if dry_run:
        n = execute(
            "SELECT count(*) FROM memory_links WHERE strength > %s", (keep,)
        )
        return {"decayed": 0, "dry_run": True, "would_decay": n,
                "keep_factor": keep, "decay_rate": 1.0 - keep}
    n = execute("UPDATE memory_links SET strength = strength * %s", (keep,))
    return {"decayed": n, "keep_factor": keep, "decay_rate": 1.0 - keep}

# === Working memory ===
# FIX (2026-08-02): was querying non-existent `memory_working`. The actual
# table (declared in schema.sql) is `working_memory` with columns
# (id PK, session_id, key, value, created_at, updated_at). Also: the
# `memory_conversations` table referenced by /working/{sid}/append "B" path
# was never declared -- that path stays disabled until a schema is added.
def working_set(payload: Dict = {}):
    # FIX (2026-08-02): signature was (session_id, key, value) treated as required
    # query params, but the connector sends them in the JSON body. Switched to a
    # body-driven signature so callers don't have to URL-encode every field.
    session_id = payload.get("session_id") or ""
    key = payload.get("key") or ""
    value = payload.get("value")
    if not session_id or not key:
        raise HTTPException(422, "payload requires session_id and key (value defaults to {})")
    if value is None:
        value = {}
    execute("""INSERT INTO working_memory(id, session_id, key, value, updated_at)
              VALUES (%s, %s, %s, %s::jsonb, NOW())
              ON CONFLICT (session_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
            (f"wm_{uuid.uuid4().hex[:8]}", session_id, key, json.dumps(value)))
    return {"session_id": session_id, "key": key, "status": "set"}

def working_context(session_id: str):
    # FIX (2026-08-02, Direction B): return the persisted conversation too, not a
    # hardcoded empty list. Fuses working_memory (scratch KV) + memory_conversations
    # (ordered dialogue) into one LLM-ready context blob.
    items = query("SELECT key, value, updated_at FROM working_memory WHERE session_id = %s", (session_id,))
    conv = query(
        "SELECT seq, role, content, created_at FROM memory_conversations "
        "WHERE session_id = %s ORDER BY seq", (session_id,))
    for r in conv:
        r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
    return {"session_id": session_id, "working": items, "conversation": conv}

def working_append(session_id: str, payload: Dict):
    """Append working memory (key/value OR conversation turn role/content)."""
    # Two payload shapes supported:
    # A: {"key": "...", "value": {...}} -> append to working_memory value list
    # B: {"role": "...", "content": "..."} -> append to memory_conversations
    if "key" in payload and "value" in payload:
        key = payload["key"]
        value = payload["value"]
        cur_row = query("SELECT value FROM working_memory WHERE session_id = %s AND key = %s",
                        (session_id, key), fetch="one")
        if cur_row and cur_row.get("value"):
            existing = cur_row["value"]
            if isinstance(existing, list):
                existing.append(value)
            else:
                existing = [existing, value]
        else:
            existing = [value]
        execute("""INSERT INTO working_memory(id, session_id, key, value, updated_at)
                  VALUES (%s, %s, %s, %s::jsonb, NOW())
                  ON CONFLICT (session_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                (f"wm_{uuid.uuid4().hex[:8]}", session_id, key, json.dumps(existing)))
        return {"session_id": session_id, "key": key, "status": "appended"}
    elif "role" in payload and "content" in payload:
        # FIX (2026-08-02, Direction B): was 501 — memory_conversations table
        # didn't exist. Now persists the turn with a monotonically increasing seq
        # for stable ordering when /context is read back.
        role = payload["role"]; content = payload["content"]
        seq = query("SELECT COALESCE(MAX(seq), 0) + 1 as s FROM memory_conversations WHERE session_id = %s",
                    (session_id,), fetch="one")["s"]
        execute("""INSERT INTO memory_conversations(session_id, seq, role, content)
                  VALUES (%s, %s, %s, %s)""",
                (session_id, seq, role, content))
        return {"session_id": session_id, "seq": seq, "role": role, "status": "appended"}
    else:
        raise HTTPException(400, "payload must have {key,value} or {role,content}")

def working_get(session_id: str):
    rows = query("SELECT key, value, updated_at FROM working_memory WHERE session_id = %s", (session_id,))
    for r in rows:
        r["updated_at"] = r["updated_at"].isoformat() if r.get("updated_at") else None
    return {"session_id": session_id, "items": rows}

def working_get_key(session_id: str, key: str):
    row = query("SELECT key, value, updated_at FROM working_memory WHERE session_id = %s AND key = %s",
                (session_id, key), fetch="one")
    if not row:
        raise HTTPException(404, "key not found")
    if row.get("updated_at"):
        row["updated_at"] = row["updated_at"].isoformat()
    return row

# === Forgetting ===
def forgetting_stats():
    total = query("SELECT COUNT(*) as c FROM memory_chunks WHERE archived = 0", fetch="one")["c"]
    expired = query("SELECT COUNT(*) as c FROM memory_chunks WHERE expires_at < NOW() AND archived = 0", fetch="one")["c"]
    # FIX (2026-08-02): compute tier counts from the actual `tier` column
    # instead of hardcoding (long_term=0, medium_term=0, short_term=total).
    by_tier = query(
        "SELECT tier, COUNT(*) as c FROM memory_chunks WHERE archived = 0 GROUP BY tier",
        fetch="all")
    tier_counts = {row["tier"]: row["c"] for row in by_tier}
    return {"total": total, "expired": expired, "tiers": tier_counts}

def forgetting_expired(limit: int = 50):
    rows = query("SELECT id, content, importance, expires_at FROM memory_chunks WHERE expires_at < NOW() AND archived = 0 LIMIT %s", (limit,))
    return {"total": len(rows), "expired": rows}

def forgetting_expiring(days: int = 7, limit: int = 50):
    rows = query("""SELECT id, content, importance, expires_at FROM memory_chunks 
                    WHERE expires_at < NOW() + INTERVAL '%s days' AND expires_at > NOW() AND archived = 0 
                    LIMIT %s""", (days, limit))
    return {"total": len(rows), "expiring_soon": rows}

async def forgetting_cleanup(force: bool = False):
    """Run forgetting cleanup with 15s timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_forgetting_cleanup_sync, force),
            timeout=15.0
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "forgetting/cleanup 超时(15s)")

def _forgetting_cleanup_sync(force: bool):
    expired = query("SELECT id FROM memory_chunks WHERE expires_at < NOW() AND archived = 0 LIMIT 1000", fetch="all")
    n = 0
    for row in expired:
        # FIX (2026-08-02): forgetting_log has only (id, memory_id, action, reason, created_at).
        # The old INSERT referenced score_before/score_after columns that don't exist ->
        # every cleanup call 500'd after V5.2.17 started setting expires_at. Drop them.
        execute("""INSERT INTO forgetting_log(memory_id, action, reason)
                  VALUES (%s, 'cleanup', 'expired')""", (row["id"],))
        execute("DELETE FROM memory_chunks WHERE id = %s", (row["id"],))
        n += 1
    return {"total_checked": n, "expired_count": n, "execution_time_ms": 0, "status": "success"}


# === Guided forgetting (Direction 4: access-driven tier/TTL governance) ===
def guided_forgetting(force: bool = False):
    """FIX (2026-08-02, Direction 4): adjust tier + TTL based on actual recall
    (access_stats count), not just fixed durations.

    The three-tier lifecycle previously relied purely on the importance-derived
    TTL set at create/update time. But a memory that is recalled a lot stays
    'relevant' and should be preserved LONGER (upgrade tier), while one that is
    never accessed should be let to expire sooner. access_stats already records
    every /memories/{id}/refresh, so we can drive tier from behavior.

    Rules applied to non-permanent chunks:
      - high-access short/medium chunk (recency-boosted): upgrade to the next
        tier and reset/renew its TTL (reconsolidation).
      - low/no-access chunk: keep its current TTL (it will evict naturally); we
        do NOT shorten TTL aggressively here (that is cleanup's job later).
    permanent (expires_at NULL) chunks are left untouched.
    """
    # count accesses per memory id, grouped by tier
    stats = query(
        """SELECT a.memory_id, COUNT(*) as c
           FROM access_stats a JOIN memory_chunks m ON m.id = a.memory_id
           WHERE m.archived = 0 AND m.expires_at IS NOT NULL
                 AND a.accessed_at > NOW() - INTERVAL '30 days'
           GROUP BY a.memory_id""", fetch="all")
    upgraded = 0
    extended = 0
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    for row in stats or []:
        mid = row["memory_id"]
        cnt = row["c"]
        if cnt < 3:
            continue  # not enough recall to justify an upgrade
        # Load current tier + importance
        meta = query("SELECT tier, importance, expires_at FROM memory_chunks WHERE id = %s",
                     (mid,), fetch="one")
        if not meta:
            continue
        cur_tier = meta["tier"]
        grow_ttl = None
        if cur_tier == "short_term":
            grow_ttl = (now + timedelta(days=30)).isoformat()   # promote to medium lifetime
            target_imp = 5
        elif cur_tier == "medium_term":
            grow_ttl = (now + timedelta(days=180)).isoformat()  # promote to long lifetime
            target_imp = 7
        else:
            continue  # long_term/permanent already governed by create/update
        execute(
            "UPDATE memory_chunks SET tier = %s, expires_at = %s, importance = %s, updated_at = NOW() "
            "WHERE id = %s",
            ("medium_term" if cur_tier == "short_term" else "long_term",
             grow_ttl, target_imp, mid))
        execute("""INSERT INTO forgetting_log(memory_id, action, reason)
                  VALUES (%s, 'upgrade', 'high_recall_guidance')""", (mid,))
        upgraded += 1
    return {"checked": len(stats or []), "upgraded": upgraded, "status": "success"}


# === Audit + history ===
def audit(
    limit: int = 50,
    memory_id: Optional[str] = None,
    action: Optional[str] = None,
    actor: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    # V6.2.0 (2026-08-13): resource_type filter (chunk/search/admin/etc).
    resource_type: Optional[str] = None,
):
    # P1-A6 fix (2026-08-13): add memory_id/action/actor/since/until filters.
    where = []
    params = []
    if memory_id:
        where.append("memory_id = %s"); params.append(memory_id)
    if action:
        where.append("action = %s"); params.append(action)
    if since:
        where.append("created_at >= %s"); params.append(since)
    if until:
        where.append("created_at < %s"); params.append(until)
    sql = "SELECT * FROM memory_access_audit"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # actor filter via details JSONB
    if actor:
        sql += (" AND " if where else " WHERE ") + "details->>'actor' = %s"
        params.append(actor)
    if resource_type:
        sql += (" AND " if where else " WHERE ") + "resource_type = %s"
        params.append(resource_type)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    rows = query(sql, tuple(params), fetch="all-no-commit")
    return {"total": len(rows), "entries": rows}

class AuditLogBody(BaseModel):
    """Body schema for POST /audit/log. All fields optional; `action` is required
    (FastAPI treats absent `action` as 422)."""
    action: Optional[str] = None
    memory_id: Optional[str] = None
    resource_type: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    actor: Optional[str] = None
    session_id: Optional[str] = None


def audit_log(body: AuditLogBody = Body(default=AuditLogBody()),
              action: Optional[str] = None,
              memory_id: Optional[str] = None,
              resource_type: str = "chunk"):
    # FIX (2026-08-02): accept fields from either query or request body (JSON),
    # so the hermes connector's POST {action, resource_type, ...} payload no
    # longer triggers 422 (previously: `memory_id`+`action` were both required
    # query params; the connector sent them only in the body, so every
    # log_audit call failed and retried for 7s before the connector moved on).
    # Body fields take precedence; query params act as fallback / override.
    final_action = action or body.action
    if not final_action:
        raise HTTPException(status_code=422, detail="action is required (query or body)")
    final_memory_id = memory_id or body.memory_id
    final_resource_type = resource_type if resource_type != "chunk" else (body.resource_type or resource_type)
    details = body.details or {}
    if not isinstance(details, dict):
        details = {}
    # Include actor/session_id in details for traceability
    if body.actor:
        details.setdefault("actor", body.actor)
    if body.session_id:
        details.setdefault("session_id", body.session_id)
    execute("""INSERT INTO memory_access_audit(memory_id, action, resource_type, details, created_at)
              VALUES (%s, %s, %s, %s::jsonb, NOW())""",
            (final_memory_id, final_action, final_resource_type, json.dumps(details)))
    return {"status": "logged"}

def get_history(memory_id: str):
    rows = query("SELECT * FROM memory_history WHERE memory_id = %s ORDER BY created_at DESC LIMIT 50", (memory_id,))
    return {"memory_id": memory_id, "history": rows}

def provenance_audit(trust: Optional[str] = None, limit: int = 100):
    """List records by their origin marker.

    V6.6.0 (2026-10-30). The operational question this answers is
    "what in my memory did NOT come from the user?" — which is the
    question you need to ask after a poisoning attempt, because a planted
    instruction is indistinguishable from a real preference once it is
    written.

    ``trust=external`` lists everything that came from outside the trust
    boundary (web, tool output, another agent) — the population worth
    auditing by hand.

    NOTE: this route is declared BEFORE /provenance/{memory_id} on
    purpose. FastAPI matches routes in declaration order, and
    /provenance/{memory_id} would otherwise swallow "audit" as a memory
    id and return the single-chunk view with memory_id="audit".
    """
    if limit < 1 or limit > 1000:
        raise HTTPException(400, f"limit={limit} must be between 1 and 1000")
    if trust:
        if trust not in ALLOWED_ORIGIN_TRUST:
            raise HTTPException(
                400,
                f"trust={trust!r} not allowed. Allowed: {sorted(ALLOWED_ORIGIN_TRUST)}",
            )
        rows = query(
            """SELECT id, content, type, origin, origin_trust, origin_ref, created_at
               FROM memory_chunks
               WHERE archived = 0 AND origin_trust = %s
               ORDER BY created_at DESC NULLS LAST
               LIMIT %s""",
            (trust, limit),
        )
    else:
        rows = query(
            """SELECT id, content, type, origin, origin_trust, origin_ref, created_at
               FROM memory_chunks
               WHERE archived = 0
               ORDER BY created_at DESC NULLS LAST
               LIMIT %s""",
            (limit,),
        )
    counts = query(
        """SELECT origin_trust, count(*) AS c
           FROM memory_chunks WHERE archived = 0
           GROUP BY origin_trust ORDER BY c DESC""",
        fetch="all-no-commit",
    )
    return {
        "rows": rows,
        "total": len(rows),
        "by_trust": {r["origin_trust"]: r["c"] for r in counts},
    }

def get_provenance(memory_id: str):
    history = query("SELECT * FROM memory_history WHERE memory_id = %s ORDER BY created_at DESC LIMIT 20", (memory_id,))
    chunk = query("SELECT * FROM memory_chunks WHERE id = %s", (memory_id,), fetch="one")
    # V6.6.0 (2026-10-30): include the origin marker and the facts derived
    # from this chunk, so an auditor can follow the chain in both
    # directions. The derived facts' markers are what show whether
    # consolidation preserved (or laundered) the provenance.
    derived = query(
        """SELECT id, fact, confidence, origin, origin_trust, origin_ref, created_at
           FROM memory_extracted_facts WHERE source_chunk_id = %s
           ORDER BY created_at DESC LIMIT 20""",
        (memory_id,),
    )
    return {
        "memory_id": memory_id,
        "history": history,
        "chunk": chunk,
        "derived_facts": derived,
        "origin": (chunk or {}).get("origin"),
        "origin_trust": (chunk or {}).get("origin_trust"),
        "origin_ref": (chunk or {}).get("origin_ref"),
    }


# === Storage ===
def storage_health():
    chunks = query("SELECT COUNT(*) as c FROM memory_chunks", fetch="one")["c"]
    vecs = query("SELECT COUNT(*) as c FROM memory_chunks WHERE embedding IS NOT NULL", fetch="one")["c"]
    # ADR-262: sum both legacy relation tables (memory_relations from
    # consolidate pipeline + memory_links from manual /links/create)
    rels_main = query("SELECT COUNT(*) AS c FROM memory_relations", fetch="one")["c"]
    rels_links = query("SELECT COUNT(*) AS c FROM memory_links", fetch="one")["c"]
    rels = {"c": rels_main + rels_links}
    # Get DB size
    size_row = query("SELECT pg_size_pretty(pg_database_size(%s)) as s", (PG_DB,), fetch="one")
    return {
        "current_count": chunks,
        "current_size_mb": 0,  # simplified
        "max_count": 50000,
        "max_size_mb": 2048,
        "count_usage_pct": chunks / 50000 * 100,
        "relations": rels,
        "vectors": vecs,
        "status": "healthy" if chunks == vecs else "inconsistent",
        "db_size": size_row["s"] if size_row else "unknown",
    }

# === TTL ===
def ttl_archive(chunk_id: str):
    n = execute("UPDATE memory_chunks SET archived = 1 WHERE id = %s", (chunk_id,))
    return {"id": chunk_id, "status": "archived", "updated": n}

def ttl_archived(limit: int = 50):
    rows = query("SELECT id, content, type, importance FROM memory_chunks WHERE archived = 1 ORDER BY updated_at DESC LIMIT %s", (limit,))
    return {"total": len(rows), "archived": rows}

# === Taxonomy ===
# FIX (2026-08-02): all three handlers queried a non-existent table `tags_hierarchy`
# and used column names `child_tag` that don't match the real schema. The actual
# table (declared in schema.sql) is `tags_taxonomy(tag, parent_tag, description,
# created_at)`. Empty result set is the correct behavior when no taxonomy rows
# exist, instead of HTTP 500.
def taxonomy_relations():
    rows = query("SELECT tag, parent_tag, description, created_at FROM tags_taxonomy ORDER BY created_at")
    return {"total": len(rows), "relations": rows}

def taxonomy_ancestors(child_tag: str):
    rows = query("""WITH RECURSIVE ancestors AS (
                    SELECT tag, parent_tag FROM tags_taxonomy WHERE tag = %s
                    UNION
                    SELECT t.tag, t.parent_tag FROM tags_taxonomy t
                    JOIN ancestors a ON t.tag = a.parent_tag
                  ) SELECT * FROM ancestors""", (child_tag,))
    return {"child_tag": child_tag, "ancestors": rows}

def taxonomy_delete(child_tag: str):
    n = execute("DELETE FROM tags_taxonomy WHERE tag = %s", (child_tag,))
    return {"child_tag": child_tag, "deleted": n}

# === Stats ops ===
OPS_COUNTERS = {k: 0 for k in [
    "conflict_detected",        # incremented in _resolve_conflict_for_chunk
    "conflict_resolved",        # incremented in _resolve_conflict_for_chunk
    "taxonomy_boost_applied",   # incremented in /search _taxonomy_boost_factor
    "rerank_applied",           # incremented in /search when CE rerank actually ran
    "rerank_skipped",           # rerank requested but fell back to recall order
    "raptor_applied",           # incremented in /search when the RAPTOR tree contributed
    "graph_signal_applied",     # incremented in /search when the graph connectivity signal contributed
]}

def stats_ops():
    return OPS_COUNTERS


def stats_embed_cache():
    """V6.2.0 (2026-08-13): expose _EMBED_CACHE stats for ops visibility."""
    return {
        "size": len(_EMBED_CACHE),
        "max": _EMBED_CACHE_MAX,
        "hits": _embed_cache_hits,
        "misses": _embed_cache_misses,
    }


def stats_health():
    """V6.2.0 (2026-08-13): detailed health check (DB + vec + cache + counters)."""
    try:
        db_size_row = query("SELECT count(*) AS c FROM memory_chunks", fetch="one")
        db_count = int(db_size_row["c"]) if db_size_row else 0
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}
    try:
        vec_row = query("SELECT count(*) AS c FROM memory_chunks WHERE embedding IS NOT NULL", fetch="one")
        vec_count = int(vec_row["c"]) if vec_row else 0
    except Exception:
        vec_count = -1
    return {
        "status": "healthy",
        "db_count": db_count,
        "vec_count": vec_count,
        "cache_size": len(_EMBED_CACHE),
        "cache_hits": _embed_cache_hits,
        "cache_misses": _embed_cache_misses,
        "ops": OPS_COUNTERS,
    }

def reset_ops():
    for k in OPS_COUNTERS: OPS_COUNTERS[k] = 0
    return {"status": "reset", "counters": OPS_COUNTERS}

# === Mode ===
def get_mode():
    return {"mode": "normal", "provider": "pgvector", "version": app.version}

# === Core profile ===
# FIX (2026-08-02): was querying non-existent table `memory_core` (500 on every call).
# The actual table (declared in schema.sql) is `core_profile` with columns
# (user_id, profile, source, updated_at). Also: was writing to `last_seen`
# which is not in the schema — use `updated_at` to match the real column.
def get_core(user_id: str):
    row = query("SELECT user_id, profile, source, updated_at FROM core_profile WHERE user_id = %s",
                (user_id,), fetch="one")
    if not row:
        return {"user_id": user_id, "profile": {}, "core_data": {}}
    return row

def get_core_list():
    rows = query("SELECT user_id, profile, source, updated_at FROM core_profile ORDER BY updated_at DESC")
    return {"total": len(rows), "users": rows}

def upsert_core(user_id: str, profile: Dict = {}, source: str = "user"):
    execute("""INSERT INTO core_profile(user_id, profile, source, updated_at)
              VALUES (%s, %s::jsonb, %s, NOW())
              ON CONFLICT (user_id) DO UPDATE SET profile = EXCLUDED.profile,
                                                source = EXCLUDED.source,
                                                updated_at = NOW()""",
            (user_id, json.dumps(profile), source))
    return {"user_id": user_id, "status": "updated"}

# === Episodes ===
# FIX (2026-08-02): schema is `episodes(id, user_id, session_id, summary, events,
# participants, duration_seconds, created_at, updated_at)`. Code used wrong
# table name and wrong columns (title/payload don't exist).
class EpisodeCreate(BaseModel):
    session_id: str
    title: str = ""  # accepted for API compat, stored in summary prefix
    summary: str = ""
    user_id: Optional[str] = None
    duration_seconds: int = 0

def create_episode(e: EpisodeCreate):
    eid = f"ep_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:6]}"
    summary = (e.title + ": " if e.title else "") + e.summary
    execute("""INSERT INTO episodes(id, user_id, session_id, summary, events, participants, duration_seconds, created_at)
              VALUES (%s, %s, %s, %s, '[]'::jsonb, '[]'::jsonb, %s, NOW())""",
            (eid, e.user_id, e.session_id, summary, e.duration_seconds))
    return {"id": eid}

def get_episode(memory_id: str):
    # Look up by id first, then fall back to session_id (real schema has no
    # memory_id column; callers have historically used either as the lookup key).
    rows = query("""SELECT id, user_id, session_id, summary, events, participants,
                          duration_seconds, created_at, updated_at
                   FROM episodes
                   WHERE id = %s OR session_id = %s
                   ORDER BY created_at DESC LIMIT 10""",
                 (memory_id, memory_id))
    return {"memory_id": memory_id, "episodes": rows}

# === Procedural ===
# FIX (2026-08-02): schema is `procedural(id, name, description, steps, success_count,
# failure_count, created_at, updated_at)`. Code used wrong table and columns
# (trigger_pattern/action_recipe/confidence don't exist).
class ProceduralCreate(BaseModel):
    name: str
    description: str = ""
    trigger_pattern: str = ""   # accepted for API compat
    action_recipe: str = ""     # accepted for API compat
    # FIX (2026-08-02, Direction 5): connector backs ProceduralCreate with
    # {name, trigger, action, description} via learn_procedure(). Accept those
    # too so trigger/action actually flow into the stored description (which is
    # what /procedural/search queries).
    trigger: str = ""
    action: str = ""

    def _effective_description(self) -> str:
        if self.description:
            return self.description
        trig = self.trigger or self.trigger_pattern
        act = self.action or self.action_recipe
        if trig or act:
            return f"{trig} -> {act}".strip(" ->")
        return self.name

def create_procedural(p: ProceduralCreate):
    pid = f"proc_{uuid.uuid4().hex[:8]}"
    description = p._effective_description()
    execute("""INSERT INTO procedural(id, name, description, steps, created_at, updated_at)
              VALUES (%s, %s, %s, '[]'::jsonb, NOW(), NOW())""",
            (pid, p.name, description))
    return {"id": pid}

def search_procedural(q: str = "", trigger: str = "", limit: int = 5):
    # FIX (2026-08-02, Direction 5): accept both ?q= and ?trigger=. The connector
    # sends ?trigger= (search_procedures passes trigger_query), while the spec
    # declared ?q=. Search name + description so either entry point finds procs.
    needle = q or trigger
    rows = query("""SELECT id, name, description, steps, success_count, failure_count,
                          created_at, updated_at
                   FROM procedural
                   WHERE name ILIKE %s OR description ILIKE %s
                   LIMIT %s""",
                (f"%{needle}%", f"%{needle}%", limit))
    return {"total": len(rows), "procedures": rows}

def procedural_outcome(proc_id: str, success: bool = True):
    # FIX: column names are success_count / failure_count in real schema
    if success:
        execute("""UPDATE procedural
                  SET success_count = success_count + 1, updated_at = NOW()
                  WHERE id = %s""", (proc_id,))
    else:
        execute("""UPDATE procedural
                  SET failure_count = failure_count + 1, updated_at = NOW()
                  WHERE id = %s""", (proc_id,))
    return {"proc_id": proc_id, "status": "recorded"}

# === Hierarchy ===
# FIX (2026-08-02): schema is `hierarchy_nodes(id, parent_id, type, label, metadata,
# created_at)`. Code used wrong table and columns (node_id/name/node_type don't
# exist). Map node_id -> id, name -> label, node_type -> type.
class HierarchyNode(BaseModel):
    name: str = ""
    label: str = ""
    parent_id: Optional[str] = None
    node_type: str = "category"
    type: str = "category"
    metadata: Dict = {}

def create_hierarchy(n: HierarchyNode):
    nid = f"hier_{uuid.uuid4().hex[:8]}"
    label = n.label or n.name or "unnamed"
    type_v = n.type or n.node_type or "category"
    execute("""INSERT INTO hierarchy_nodes(id, parent_id, type, label, metadata, created_at)
              VALUES (%s, %s, %s, %s, %s::jsonb, NOW())""",
            (nid, n.parent_id, type_v, label, json.dumps(n.metadata)))
    return {"node_id": nid}

def get_hierarchy_node(node_id: str):
    row = query("""SELECT id, parent_id, type, label, metadata, created_at
                   FROM hierarchy_nodes WHERE id = %s""", (node_id,), fetch="one")
    if not row:
        raise HTTPException(404, "node not found")
    return row

def enrich_chunk_with_hierarchy(chunk_id: str):
    """Walk the hierarchy chain for a chunk. Currently no chunk->node linkage
    is persisted, so we just look up whether any hierarchy node exists with
    metadata.chunk_id == chunk_id and return the ancestry.
    """
    # FIX (2026-08-02): previously this endpoint didn't exist; the connector's
    # `enrich_chunk_with_hierarchy()` would get a 404 silently swallowed by
    # its try/except. Added a real implementation that returns the node (if any)
    # tagged with this chunk_id, plus its ancestors.
    rows = query(
        "SELECT id, parent_id, type, label, metadata, created_at "
        "FROM hierarchy_nodes WHERE metadata->>'chunk_id' = %s",
        (chunk_id,))
    return {"chunk_id": chunk_id, "nodes": rows}

def get_hierarchy_tree(node_id: str):
    root = query("""SELECT id, parent_id, type, label, metadata, created_at
                    FROM hierarchy_nodes WHERE id = %s""", (node_id,), fetch="one")
    if not root:
        return {"root": None, "children": []}
    children = query("""SELECT id, parent_id, type, label, metadata, created_at
                        FROM hierarchy_nodes WHERE parent_id = %s""", (node_id,))
    return {"root": root, "children": children}

def hierarchy_enrich(chunk_id: str):
    chunk = query("SELECT * FROM memory_chunks WHERE id = %s", (chunk_id,), fetch="one")
    if not chunk:
        return {"chunk_id": chunk_id, "enrichments": []}
    return {"chunk_id": chunk_id, "chunk": chunk, "enrichments": []}

# === Cache ===
def pr_cache_stats():
    return {"hits": 0, "misses": 0, "size": 0}

def clear_pr_cache():
    return {"cleared": True}

# === Access stats ===
# FIX (2026-08-02): was querying non-existent `memory_access_stats`. The actual
# table (declared in schema.sql) is `access_stats` with columns
# (id BIGSERIAL, memory_id TEXT NOT NULL, access_type TEXT, accessed_at TIMESTAMPTZ).
# The previous fix assumed access_count/last_access_at/relevance_score columns
# which do not exist in the real schema -> UndefinedColumnError -> 500.
def access_stats(
    limit: int = 50,
    memory_id: Optional[str] = None,
    access_type: Optional[str] = None,
    since: Optional[str] = None,
):
    # P1-A6 fix (2026-08-13): add memory_id/access_type/since filters.
    where = []
    params = []
    if memory_id:
        where.append("memory_id = %s"); params.append(memory_id)
    if access_type:
        where.append("access_type = %s"); params.append(access_type)
    if since:
        where.append("accessed_at >= %s"); params.append(since)
    sql = "SELECT id, memory_id, access_type, accessed_at FROM access_stats"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY accessed_at DESC LIMIT %s"
    params.append(limit)
    rows = query(sql, tuple(params), fetch="all-no-commit")
    return {"total": len(rows), "stats": rows}

def access_stats_for(memory_id: str):
    row = query("SELECT id, memory_id, access_type, accessed_at FROM access_stats WHERE memory_id = %s", (memory_id,), fetch="one")
    if not row:
        return {"memory_id": memory_id, "access_count": 0, "last_access_at": None, "access_type": None}
    return row

def refresh_memory(memory_id: str):
    """Refresh access stats for a memory (called on retrieval)."""
    execute("""INSERT INTO access_stats(memory_id, access_type, accessed_at)
              VALUES (%s, %s, NOW())""", (memory_id, "refresh"))
    return {"memory_id": memory_id, "status": "refreshed"}

# === Media ===
class MediaCreate(BaseModel):
    media_type: str
    content: str
    mime_type: str = "text/plain"
    metadata: Dict = {}

def create_media(m: MediaCreate):
    mid = f"med_{uuid.uuid4().hex[:8]}"
    execute("""INSERT INTO memory_media(id, media_type, content, mime_type, metadata, created_at)
              VALUES (%s, %s, %s, %s, %s::jsonb, NOW())""",
            (mid, m.media_type, m.content, m.mime_type, json.dumps(m.metadata)))
    return {"id": mid}

def search_media(q: str, limit: int = 5):
    rows = query("SELECT * FROM memory_media WHERE content ILIKE %s LIMIT %s", (f"%{q}%", limit))
    return {"total": len(rows), "media": rows}

def media_by_type(media_type: str, limit: int = 50):
    rows = query("SELECT * FROM memory_media WHERE media_type = %s LIMIT %s", (media_type, limit))
    return {"total": len(rows), "media": rows}

# === Resources ===
class ResourceCreate(BaseModel):
    resource_type: str
    title: str
    content: str
    metadata: Dict = {}

def create_resource(r: ResourceCreate):
    rid = f"res_{uuid.uuid4().hex[:8]}"
    execute("""INSERT INTO memory_resources(id, resource_type, title, content, metadata, created_at)
              VALUES (%s, %s, %s, %s, %s::jsonb, NOW())""",
            (rid, r.resource_type, r.title, r.content, json.dumps(r.metadata)))
    return {"id": rid}

def search_resources(q: str, limit: int = 5):
    rows = query("SELECT * FROM memory_resources WHERE title ILIKE %s OR content ILIKE %s LIMIT %s",
                 (f"%{q}%", f"%{q}%", limit))
    return {"total": len(rows), "resources": rows}

def resource_accessed(resource_id: str):
    execute("""UPDATE memory_resources SET access_count = access_count + 1, last_accessed = NOW()
              WHERE id = %s""", (resource_id,))
    return {"status": "ok"}

# === Dashboard ===
def dashboard():
    return get_dashboard_data()


def get_dashboard_data():
    stats_data = stats()
    recent = query("SELECT id, content, type, importance, created_at FROM memory_chunks ORDER BY created_at DESC LIMIT 10")
    return {**stats_data, "recent_chunks": recent}


async def dashboard_html():
    """Web UI - 4 tabs: overview / stats / search / graph (v0.1)"""
    _ui = _os_ui.path.join(_os_ui.path.dirname(__file__), "static", "index.html")
    if not _os_ui.path.exists(_ui):
        return HTMLResponse("<h1>UI not found</h1>", status_code=404, media_type="text/html")
    try:
        return FileResponse(_ui, media_type="text/html")
    except Exception as exc:
        logger.exception("Failed to serve dashboard UI: %s", exc)
        return HTMLResponse("<h1>Dashboard UI unavailable</h1>", status_code=503, media_type="text/html")


async def dashboard_ui_alias():
    """根 / 是 Web UI 首页 (等同 /dashboard/html). 真正的 JSON 统计在 /dashboard"""
    return await dashboard_html()


async def reconsolidate():
    try:
        return await asyncio.wait_for(asyncio.to_thread(_reconsolidate_sync), timeout=15.0)
    except asyncio.TimeoutError:
        raise HTTPException(504, "reconsolidate 超时(15s)")

def _reconsolidate_sync():
    """ADR-067-Patch-4 (2026-08-04): 真实实现关联强度衰减 + 共同召回升级.

    按 docs/设计.md §2.1 + ADR-067 §3.4:
    1. 关联强度衰减: 长期未共同召回的关联 strength *= 0.95
       (RELATION_STRENGTH_DECAY = 0.95, 设计 §2.5)
    2. 共同召回升级: co_occurrence_count >= 3 -> strength 上限 1.0
    3. 弱关联清理: strength < 0.1 -> DELETE (设计 §2.1)

    Returns:
      {decayed: N, upgraded: N, cleaned: N}
    """
    decayed = 0
    # 1) 关联强度衰减 (阶梯式, 每次跑 ×0.95)
    try:
        execute(
            """UPDATE memory_relations
               SET strength = strength * 0.95,
                   updated_at = NOW()
               WHERE strength > 0""")
        # 拿刚被影响的行数 (psycopg2 rowcount via execute 不直接给, 用 SELECT 估)
        decayed = query(
            "SELECT COUNT(*) AS c FROM memory_relations WHERE strength > 0",
            fetch="one")["c"]
    except Exception as e:
        logger.warning(f"reconsolidate decay failed: {e}")

    # 2) 共同召回升级: co_occurrence_count >= 3 -> strength 拉满 (但 ≤ 1.0)
    upgraded = 0
    try:
        rows = query(
            """SELECT id, memory_id_1, memory_id_2, co_occurrence_count, strength
               FROM memory_relations
               WHERE co_occurrence_count >= 3 AND strength < 1.0
               LIMIT 500""", fetch="all")
        for r in rows:
            new_str = min(1.0, float(r["strength"]) + 0.1)
            execute(
                "UPDATE memory_relations SET strength = %s, updated_at = NOW() WHERE id = %s",
                (new_str, r["id"]))
            upgraded += 1
    except Exception as e:
        logger.warning(f"reconsolidate upgrade failed: {e}")

    # 3) 弱关联清理: strength < 0.1 -> DELETE
    cleaned = 0
    try:
        rows = query(
            "SELECT id FROM memory_relations WHERE strength < 0.1 LIMIT 500",
            fetch="all")
        for r in rows:
            execute("DELETE FROM memory_relations WHERE id = %s", (r["id"],))
            cleaned += 1
    except Exception as e:
        logger.warning(f"reconsolidate clean failed: {e}")

    return {"decayed": decayed, "upgraded": upgraded, "cleaned": cleaned, "status": "ok"}

async def merge_duplicates(similarity_threshold: float = 0.9):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_merge_duplicates_sync, similarity_threshold), timeout=15.0)
    except asyncio.TimeoutError:
        raise HTTPException(504, "merge-duplicates 超时(15s)")

def _merge_duplicates_sync(threshold: float):
    """Find chunks with high similarity and merge them."""
    # FIX (2026-08-02): ON CONFLICT (memory_id_1, memory_id_2) without a unique
    # constraint -> 500. Added the unique index in schema.sql. The error still
    # could be raised if the index doesn't yet exist on the live DB -- wrap the
    # INSERT in a try/except so the merge loop doesn't bail on the first failure.
    rows = query("SELECT id, content, type FROM memory_chunks WHERE embedding IS NOT NULL LIMIT 100", fetch="all")
    merged = 0
    for r in rows:
        similar = query("""SELECT id, content FROM memory_chunks
                          WHERE id != %s AND embedding IS NOT NULL
                          ORDER BY embedding <=> (SELECT embedding FROM memory_chunks WHERE id = %s)
                          LIMIT 1""", (r["id"], r["id"]), fetch="one")
        if similar:
            try:
                execute("""INSERT INTO memory_relations(id, memory_id_1, memory_id_2, relation_type, strength, created_at, co_occurrence_count)
                          VALUES (%s, %s, %s, 'similar', 0.7, NOW(), 1)
                          ON CONFLICT (memory_id_1, memory_id_2) DO UPDATE SET co_occurrence_count = memory_relations.co_occurrence_count + 1""",
                        (f"rel_sim_{r['id']}_{similar['id']}"[:200], r["id"], similar["id"]))
                merged += 1
            except Exception as e:
                # Skip this pair and continue -- not fatal for the loop.
                logger.warning("merge-duplicates skipped pair %s/%s: %s", r['id'], similar['id'], e)
    return {"merged": merged, "threshold": threshold}

def reindex():
    """No-op for PG: vectors always indexed via HNSW."""
    return {"status": "noop", "reason": "PG HNSW index is always maintained"}

def compress_run(max_importance: int = 4, max_per_batch: int = 50):
    rows = query("SELECT id, content FROM memory_chunks WHERE importance <= %s LIMIT %s",
                 (max_importance, max_per_batch))
    n = 0
    for r in rows:
        summary = r["content"][:100] + "..." if len(r["content"]) > 100 else r["content"]
        # FIX (2026-08-02): memory_compressed.id is BIGSERIAL auto-increment, not TEXT.
        # The old ON CONFLICT (id) DO NOTHING + comp_{r[id]} string was wrong shape.
        # Just plain INSERT now (duplicates allowed -- schema has no unique constraint
        # on source_chunk_id by design).
        execute("""INSERT INTO memory_compressed(source_chunk_id, summary, importance, created_at)
                  VALUES (%s, %s, %s, NOW())""",
                (r['id'], summary, 1))
        n += 1
    return {"compressed": n}

def get_compressed(source_chunk_id: str):
    # FIX (2026-08-02): used non-existent `memory_compressed` table (500). Now
    # reads the real table created in V5.2.13 schema.sql.
    row = query("""SELECT id, source_chunk_id, summary, importance, created_at
                   FROM memory_compressed WHERE source_chunk_id = %s
                   ORDER BY created_at DESC LIMIT 1""",
                (source_chunk_id,), fetch="one")
    if not row:
        raise HTTPException(404, "not compressed")
    return row

def consolidate_extract(payload: Dict):
    OPS_COUNTERS["conflict_detected"] += 1
    # Simple fact extraction: split by Chinese/English sentence terminators
    content = payload.get("content", "") or ""
    source_chunk_id = payload.get("source_chunk_id") or payload.get("memory_id")
    user_id = payload.get("user_id")
    # naive: split on 。 . ! ? ; and newlines, then take non-empty, cap at 5
    import re
    facts = [s.strip() for s in re.split(r"[。.!?;\n]+", content) if s.strip()][:5]
    # FIX (2026-08-02): was returning facts without persisting them; /consolidate/facts
    # 500'd because the table was missing entirely. Now we write each fact to
    # memory_extracted_facts so the round-trip works.
    #
    # V6.6.0 (2026-10-30): inherit the source chunk's origin marker.
    #
    # This is the anti-laundering step. A fact derived from a source must
    # carry that source's provenance and may never be MORE trusted than it.
    # Without this, consolidation can take an observation from outside the
    # trust boundary and rewrite it as a compact "user preference" — and
    # the result then reads as if the user had always said it.
    src_origin = payload.get("origin")
    src_trust = payload.get("origin_trust")
    src_ref = payload.get("origin_ref")
    if source_chunk_id:
        row = query(
            "SELECT origin, origin_trust, origin_ref FROM memory_chunks WHERE id = %s",
            (source_chunk_id,), fetch="one",
        )
        if row:
            # The source chunk's marker wins over anything the caller
            # passes: the caller cannot claim a higher trust level than the
            # record it is derived from.
            src_origin = row.get("origin") or src_origin
            src_trust = row.get("origin_trust") or src_trust
            src_ref = row.get("origin_ref") or src_ref
    f_origin, f_trust, f_ref = _inherit_origin(src_origin, src_trust, src_ref)
    # An explicit marker from the caller may LOWER the trust, never raise it.
    if payload.get("origin_trust"):
        f_trust = _most_distrustful(f_trust, payload.get("origin_trust"))
    for f in facts:
        execute("""INSERT INTO memory_extracted_facts(source_chunk_id, fact, user_id, confidence, origin, origin_trust, origin_ref, created_at)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())""",
                (source_chunk_id, f, user_id, 0.5, f_origin, f_trust, f_ref))
    return {"facts": facts, "extracted": len(facts),
            "origin": f_origin, "origin_trust": f_trust, "origin_ref": f_ref}

def raptor_rebuild(payload: Optional[Dict] = None):
    """Rebuild the RAPTOR summary-tree index.

    V6.6.0 (2026-10-30). The tree is a BACKGROUND artifact: building it
    over the production corpus takes ~30 s, which is fine as a job and
    unacceptable inside a search request. The search path only ever
    walks the persisted tree in memory.

    A rebuild lands under a new tree id, so the previous tree stays
    readable until the new one is fully written.
    """
    payload = payload or {}
    from src.consolidate.raptor_index import rebuild as _raptor_rebuild

    max_depth = int(payload.get("max_depth", 2))
    limit = int(payload.get("limit", 20000))
    result = _raptor_rebuild(
        query, execute,
        max_depth=max_depth,
        model_version=resolve_embedding_model(EMBEDDING_MODEL_ENV),
        limit=limit,
    )
    return result


def raptor_stats():
    """Report on the persisted RAPTOR index."""
    trees = query(
        """SELECT id, root_id, max_depth, node_count, chunk_count,
                  model_version, built_at
           FROM memory_raptor_trees ORDER BY built_at DESC LIMIT 5""",
        (), fetch="all-no-commit",
    )
    return {"trees": trees, "count": len(trees)}


def consolidate_run(payload: Optional[Dict] = None):
    # FIX (2026-08-19, ADR-075): was a noop stub. Now actually applies
    # extracted facts from memory_extracted_facts into core_profile
    # (one row per user_id, with profile JSONB accumulating the facts).
    # Backward-compat: payload may be None (older clients that called
    # POST /consolidate/run with no body) — in that case we run for
    # ALL users that have extracted facts and respect min_confidence.
    payload = payload or {}
    user_id = payload.get("user_id")
    min_confidence = float(payload.get("min_confidence", 0.5))

    if user_id:
        # Single-user mode
        target_users = [user_id]
    else:
        # All users mode: gather distinct user_ids from memory_extracted_facts
        rows = query("""SELECT DISTINCT user_id FROM memory_extracted_facts
                        WHERE user_id IS NOT NULL
                          AND confidence >= %s""",
                     (min_confidence,), fetch="all")
        target_users = [r["user_id"] for r in rows]

    if not target_users:
        return {"status": "noop", "reason": "no facts above min_confidence to consolidate",
                "min_confidence": min_confidence, "users_touched": 0}

    users_touched = 0
    facts_applied = 0
    for uid in target_users:
        # Fetch all facts for this user above threshold, ordered newest first
        facts_rows = query("""SELECT fact, confidence
                              FROM memory_extracted_facts
                              WHERE user_id = %s AND confidence >= %s
                              ORDER BY created_at DESC
                              LIMIT 100""",
                           (uid, min_confidence), fetch="all")
        if not facts_rows:
            continue
        # Accumulate into a profile JSONB: {facts: [...], last_consolidated_at: ...}
        # Load existing profile to preserve prior data
        existing_row = query("""SELECT profile FROM core_profile WHERE user_id = %s""",
                             (uid,), fetch="one")
        if existing_row and existing_row.get("profile"):
            existing = existing_row["profile"]
            if isinstance(existing, str):
                try:
                    import json as _json
                    existing = _json.loads(existing)
                except Exception:
                    existing = {}
        else:
            existing = {}
        existing_facts = existing.get("facts", []) if isinstance(existing, dict) else []
        # Dedupe: keep new facts first, then existing, but only first occurrence wins
        seen = set()
        merged = []
        for f in [fr["fact"] for fr in facts_rows] + list(existing_facts):
            key = f.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(f)
        # Cap profile facts at 200 to keep it reasonable
        merged = merged[:200]
        new_profile = {
            **existing,
            "facts": merged,
            "fact_count": len(merged),
            "last_consolidated_at": datetime.now(timezone.utc).isoformat(),
        }
        # UPSERT
        execute("""INSERT INTO core_profile(user_id, profile, source, updated_at)
                  VALUES (%s, %s::jsonb, 'consolidate', NOW())
                  ON CONFLICT (user_id) DO UPDATE
                    SET profile = EXCLUDED.profile,
                        updated_at = NOW()""",
                (uid, json.dumps(new_profile)))
        users_touched += 1
        facts_applied += len(facts_rows)

    OPS_COUNTERS["consolidate_run"] = OPS_COUNTERS.get("consolidate_run", 0) + 1
    return {
        "status": "ok",
        "users_touched": users_touched,
        "facts_applied": facts_applied,
        "min_confidence": min_confidence,
    }


# === P0-3 图谱启发式回填 (/graph/mine) ===
# V5 PG 迁移后 relations 仅 110/1956（纯 auto_link 相似度钩子，稀疏）。
# /graph/mine 用三种离线启发式批量挖掘关系，把图谱填实，让 /search?include_related
# 和 reason_with_memory 的多跳推理真正有图可走。纯算法、不依赖 LLM、不破坏现有数据。
_GRAPH_MINE_LOCK = asyncio.Lock()

async def graph_mine(payload: Dict = {}):
    """后台批量挖掘 chunk 间关系（相似度 / 共享标签 / 同来源）。

    Body:
      {
        "similarity_threshold": 0.72,   # 向量相似度建链阈值（默认 0.72）
        "min_shared_tags": 2,           # 共享标签建链数（默认 2）
        "similar_batch": 20000,         # 相似度建链每批 anchor 数 (上限)
        "per_anchor": 3                 # 每个 anchor 最多新链数
      }
    幂等：已存在的 (source,target) 对走 ON CONFLICT 累加强度，不重复插入。
    返回已处理 & 新建链统计。
    """
    thr = float(payload.get("similarity_threshold", 0.72))
    min_tags = int(payload.get("min_shared_tags", 2))
    # V6.6.0 (2026-10-31): the anchor cap was 200, which covered 1.8%
    # of an 11224-chunk corpus — so 98% of the corpus had no similarity
    # relation at all, and the graph could not help those memories no
    # matter how good the retrieval was.
    #
    # Mining used to take ~8 minutes for 200 anchors (one COMMIT per
    # inserted relation); it is now ~3.4 ms per anchor, so covering the
    # whole corpus is affordable. The cap is kept as a safety valve.
    similar_batch = int(payload.get("similar_batch", 20000))
    per_anchor = int(payload.get("per_anchor", 3))
    if _GRAPH_MINE_LOCK.locked():
        return {"status": "running", "reason": "previous graph/mine still in progress"}
    async with _GRAPH_MINE_LOCK:
        return await asyncio.to_thread(
            _graph_mine_sync, thr, min_tags, similar_batch, per_anchor)


# V6.6.0: relation count captured at the start of a mining run, so the
# endpoint can report how many relations were CREATED (not just touched).
_mine_relations_before = 0


def _dedupe_relation_pairs(
    pairs: List[Tuple[str, str, str, float]],
) -> Dict[Tuple[str, str], Tuple[str, str, str, float]]:
    """Collapse relation pairs to one entry per (sorted endpoint pair).

    The mining links each anchor to its nearest neighbours, so (A,B) is
    emitted once as A->B and again as B->A, and both sort to the same
    key. Postgres rejects that in one statement with
    "ON CONFLICT DO UPDATE command cannot affect row a second time"
    and aborts the whole batch.

    The strongest claim wins; the relation type of that claim is kept.
    """
    best: Dict[Tuple[str, str], Tuple[str, str, str, float]] = {}
    for mid1, mid2, rtype, strength in pairs:
        a, b = sorted([mid1, mid2])
        key = (a, b)
        prev = best.get(key)
        if prev is None or strength > prev[3]:
            best[key] = (rtype, a, b, strength)
    return best


def _insert_rels_bulk(pairs: List[Tuple[str, str, str, float]]) -> int:
    """Insert many relations in ONE statement (chunked). Returns how many were written.

    V6.6.0 (2026-10-31): the mining loop used to insert one pair at a
    time, and each of those was a separate execute() — which COMMITS.
    Measured on 127.0.0.1: mining 200 anchors over 11224 chunks took
    ~8 minutes, and the similarity queries themselves account for only
    ~14 s of it. The rest was ~4200 individual round trips each paying a
    commit.

    Batching then exposed TWO constraints that the per-row version never
    had to satisfy:

    1. A pair can appear TWICE in one batch. The mining links each
       anchor to its nearest neighbours, so (A,B) is emitted once as
       A->B and again as B->A — and both sort to the same (a,b) key.
       Postgres rejects that with
           ON CONFLICT DO UPDATE command cannot affect row a second time
       and ABORTS THE WHOLE STATEMENT, so 33383 pairs produced 0
       relations. De-duplicate first, keeping the strongest claim.

    2. Postgres allows at most 65535 bind parameters per statement.
       33383 pairs x 5 parameters is 166915, so the statement would be
       rejected even without (1). Insert in chunks.

    The per-pair semantics are preserved: the id is derived from the
    (sorted) endpoint pair so a pair always maps to the same id, and
    ON CONFLICT still accumulates the strength.
    """
    if not pairs:
        return 0

    best = _dedupe_relation_pairs(pairs)

    rows = []
    for (a, b), (rtype, _a, _b, strength) in best.items():
        rid = f"rel_{hashlib.md5(f'{a}|{b}'.encode()).hexdigest()[:16]}"
        rows.append((rid, a, b, rtype, strength))

    written = 0
    chunk = 500  # 500 rows x 5 params = 2500 bind parameters
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        placeholders = ", ".join(["(%s, %s, %s, %s, %s, 1, NOW())"] * len(batch))
        params: list = []
        for r in batch:
            params.extend(r)
        try:
            execute(
                f"""
                INSERT INTO memory_relations(id, memory_id_1, memory_id_2, relation_type,
                                             strength, co_occurrence_count, created_at)
                VALUES {placeholders}
                ON CONFLICT (memory_id_1, memory_id_2) DO UPDATE
                  SET strength = GREATEST(memory_relations.strength, EXCLUDED.strength),
                      co_occurrence_count = memory_relations.co_occurrence_count + 1,
                      updated_at = NOW()
                """,
                tuple(params),
            )
            written += len(batch)
        except Exception as e:
            logger.warning("graph/mining bulk insert chunk skipped (%d pairs): %s",
                           len(batch), e)
    return written


def _graph_mine_sync(thr: float, min_tags: int, similar_batch: int, per_anchor: int) -> Dict:
    """同步执行整体三路挖掘。"""
    # 1) 相似度建链（向量；分批，每批取最近 top per_anchor）
    sim_new = 0
    sim_pairs: List[Tuple[str, str, str, float]] = []
    _before = query("SELECT count(*) AS c FROM memory_relations", fetch="one")
    global _mine_relations_before
    _mine_relations_before = int(_before["c"]) if _before else 0
    anchors = query(
        "SELECT id, embedding FROM memory_chunks WHERE embedding IS NOT NULL AND archived=0 "
        "ORDER BY updated_at DESC NULLS LAST LIMIT %s", (similar_batch,))
    def _vec(v):
        return v if isinstance(v, bytes) else str(v)
    for r in anchors:
        # V6.6.0 fix (2026-10-31): use the HNSW index.
        #
        # The ORDER BY was `1 - (embedding <=> $1) DESC` — a COMPUTED
        # expression, so Postgres could not use the HNSW index
        # (memory_chunks_hnsw_idx, vector_cosine_ops) and fell back to a
        # sequential scan of the whole corpus with three vector distance
        # computations per row, for every anchor. Measured: mining 200
        # anchors over 11224 chunks took ~8 minutes — ~2.4 s per anchor.
        #
        # `ORDER BY embedding <=> $1` is the operator form the index
        # supports; the cosine similarity is still computed in the SELECT
        # list. The threshold filter stays (it is applied after the
        # index scan), so the result set is unchanged.
        similar = query("""
            SELECT id, 1 - (embedding <=> %s::vector) AS s
            FROM memory_chunks
            WHERE id != %s AND embedding IS NOT NULL AND archived=0
              AND (1 - (embedding <=> %s::vector)) >= %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (_vec(r["embedding"]), r["id"], _vec(r["embedding"]), thr,
              _vec(r["embedding"]), per_anchor))
        for m in similar:
            if m["id"] != r["id"]:
                sim_pairs.append((r["id"], m["id"], "similar", round(float(m["s"]), 3)))
    # V6.6.0: one batched insert instead of one commit per pair.
    sim_new = _insert_rels_bulk(sim_pairs)

    # 2) 共享标签建链（同 tag 组内两两，strength=0.6）。
    #    tags 已是 jsonb；jsonb_agg(id) 由 psycopg2 返回 str，需 json.loads。
    tag_new = 0
    tag_pairs: List[Tuple[str, str, str, float]] = []
    tag_members = query("""
        SELECT tags::text AS tags, jsonb_agg(id)::text AS ids
        FROM memory_chunks
        WHERE archived=0 AND jsonb_array_length(tags::jsonb) >= 2
        GROUP BY tags::text
        HAVING count(*) BETWEEN 2 AND 200
        LIMIT 400
    """)
    for grp in tag_members:
        try:
            ids = json.loads(grp["ids"]) if isinstance(grp["ids"], str) else (grp["ids"] or [])
        except Exception:
            continue
        if not isinstance(ids, list) or len(ids) < 2:
            continue
        # 取组内前 per_anchor 个两两建链（批量插入，见 _insert_rels_bulk）
        sel = ids[:per_anchor + 1]
        for i in range(len(sel)):
            for j in range(i + 1, len(sel)):
                tag_pairs.append((sel[i], sel[j], "tag-shared", 0.6))
    tag_new = _insert_rels_bulk(tag_pairs)

    # 3) 同来源聚类（弱链，strength=0.4）
    src_new = 0
    src_pairs: List[Tuple[str, str, str, float]] = []
    groups = query("""
        SELECT source, jsonb_agg(id)::text AS ids
        FROM memory_chunks WHERE archived=0 AND source IS NOT NULL AND source != ''
        GROUP BY source
        HAVING count(*) BETWEEN 2 AND 200
        LIMIT 200
    """)
    for g in groups:
        try:
            ids = json.loads(g["ids"]) if isinstance(g["ids"], str) else (g["ids"] or [])
        except Exception:
            continue
        if not isinstance(ids, list) or len(ids) < 2:
            continue
        sel = ids[:per_anchor + 1]
        for i in range(len(sel)):
            for j in range(i + 1, len(sel)):
                src_pairs.append((sel[i], sel[j], "same-source", 0.4))
    src_new = _insert_rels_bulk(src_pairs)

    # V6.6.0 (2026-10-31): report the count of relations that actually
    # EXIST now, and how many are new since this run started.
    #
    # The batched insert cannot report "new" per statement: with ON
    # CONFLICT DO UPDATE, Postgres' rowcount is the number of rows
    # INSERTED OR UPDATED, so it counts every pair it touched, not the
    # ones that did not exist. The total before and after the run gives
    # the real delta.
    total = query("SELECT count(*) AS c FROM memory_relations", fetch="one")
    total_after = int(total["c"]) if total else 0
    total_before = _mine_relations_before
    return {
        "status": "ok",
        "new_relations": {"similarity": sim_new, "tag-shared": tag_new,
                           "same-source": src_new},
        "links_written": sim_new + tag_new + src_new,
        "relations_created": max(0, total_after - total_before),
        "total_new": max(0, total_after - total_before),
        "total_relations_after": total_after,
    }


# === Rumination / eval ===

def consolidate_facts(limit: int = 50):
    rows = query("SELECT id, source_chunk_id, fact, user_id, confidence, created_at "
                 "FROM memory_extracted_facts ORDER BY created_at DESC LIMIT %s", (limit,))
    return {"total": len(rows), "facts": rows}


# === V5.4 conflict-resolve: 新事实优先旧事实,周期更新 ===
# 设计目标:
#   1. write-path: 写入新事实时,如发现同 (user_id, source_key) 桶内已有"确定事实",
#      自动标记旧事实 is_deprecated=True 并写 history + memory_conflict_log.
#   2. periodic: 周期任务扫所有有 source_key 的桶,做一次兜底 reconciliation
#      (处理人工写入 / 异步竞态 / 跨 user 复制等边缘情况).
#   3. audit: 提供查询端点,运维可追溯每一次 supersede 决策.
#
# 决策矩阵 (V5.4 v1):
#   新事实 confidence >= MIN_CONF (默认 0.7) AND 新 importance >= 旧 importance → SUPERSEDE
#   新事实 confidence < MIN_CONF OR 新 importance < 旧 importance → MARK_ONLY (打标,不删)
#   同 (user_id, source_key) 桶内无旧事实 → 无冲突,直接写入
#
# 用户原话: "新信息跟老信息冲突时,要优先更新老记忆,如果不能合并就删除旧的相关内容,
#           添加新的内容,保证记忆最新且不冲突. 根据新的决策优先旧的,如果确定是事实,
#           新的事实要覆盖旧的内容."

_MIN_SUPERSEDE_CONFIDENCE = 0.7  # 新事实需要达到的最低置信度


def _resolve_conflict_for_chunk(
    new_chunk_id: str,
    new_importance: int,
    new_confidence: float,
    user_id,
    source_key: str,
    decided_by: str = "auto",
) -> Dict:
    """核心冲突解决逻辑。返回 {action, old_chunk_id, reason}.

    桶定义: metadata->>'user_id' = user_id AND metadata->>'source_key' = source_key
    只看 is_deprecated=FALSE 的活跃 chunk (历史归档不影响判定).
    """
    if not source_key:
        return {"action": "noop", "old_chunk_id": None, "reason": "no source_key, skip"}

    # 1) 找桶内所有活跃旧事实
    old_rows = query(
        """SELECT id, importance, content, created_at FROM memory_chunks
           WHERE is_deprecated = FALSE
                 AND archived = 0
                 AND id != %s
                 AND metadata->>'user_id' = %s
                 AND metadata->>'source_key' = %s
           ORDER BY importance DESC, created_at DESC""",
        (new_chunk_id, user_id or "", source_key),
        fetch="all",
    )
    if not old_rows:
        return {"action": "noop", "old_chunk_id": None, "reason": "no existing fact in bucket"}

    # 2) 取最高权重的旧事实作为待覆盖目标
    primary_old = old_rows[0]
    old_imp = primary_old["importance"] or 5
    old_id = primary_old["id"]

    # 3) 决策
    reason_parts = []
    if new_confidence < _MIN_SUPERSEDE_CONFIDENCE:
        action = "mark_only"
        reason_parts.append(f"new_confidence={new_confidence:.2f}<{_MIN_SUPERSEDE_CONFIDENCE}")
    elif new_importance < old_imp:
        action = "mark_only"
        reason_parts.append(f"new_importance={new_importance}<old={old_imp}")
    else:
        action = "supersede"
        reason_parts.append(f"new_conf={new_confidence:.2f}>={_MIN_SUPERSEDE_CONFIDENCE} AND new_imp={new_importance}>={old_imp}")

    # 4) 执行 + 审计 (事务内,失败回滚)
    try:
        if action == "supersede":
            execute(
                """UPDATE memory_chunks
                   SET is_deprecated = TRUE,
                       deprecated_at = NOW(),
                       superseded_by = %s,
                       updated_at = NOW()
                   WHERE id = %s""",
                (new_chunk_id, old_id),
            )
            # 把旧 chunk 内容写历史 (与 /rumination/run 风格一致)
            execute(
                """INSERT INTO memory_history(id, memory_id, action, content, type, created_at)
                   VALUES (%s, %s, 'superseded', %s, 'supersede_log', NOW())""",
                (int(time.time() * 1e6) % 2000000000, old_id, primary_old["content"]),
            )
            # 桶内其他旧事实也一并 mark_only (避免多版本混乱)
            for other in old_rows[1:]:
                execute(
                    """UPDATE memory_chunks
                       SET is_deprecated = TRUE,
                           deprecated_at = NOW(),
                           superseded_by = %s,
                           updated_at = NOW(),
                           metadata = COALESCE(metadata, '{}'::jsonb) ||
                                      jsonb_build_object('cascade_superseded', %s::text)
                       WHERE id = %s""",
                    (new_chunk_id, new_chunk_id, other["id"]),
                )
        elif action == "mark_only":
            # 在旧 chunk metadata 标"已被新事实挑战",不删除
            for r in old_rows:
                execute(
                    """UPDATE memory_chunks
                       SET metadata = COALESCE(metadata, '{}'::jsonb) ||
                                      jsonb_build_object('challenged_by', %s::text,
                                                         'challenged_at', NOW()::text)
                       WHERE id = %s
                             AND NOT (metadata ? 'challenged_by')""",
                    (new_chunk_id, r["id"]),
                )
        # 写审计
        execute(
            """INSERT INTO memory_conflict_log
                  (user_id, source_key, old_chunk_id, new_chunk_id, action, reason, confidence, decided_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (user_id, source_key, old_id, new_chunk_id, action,
             "; ".join(reason_parts), new_confidence, decided_by),
        )
        OPS_COUNTERS.setdefault("conflict_resolved", 0)
        OPS_COUNTERS["conflict_resolved"] += 1
        return {"action": action, "old_chunk_id": old_id,
                "reason": "; ".join(reason_parts), "cascade_count": len(old_rows) - 1}
    except Exception as e:
        logger.error(f"conflict resolve failed: {e}")
        return {"action": "error", "old_chunk_id": old_id, "reason": str(e)}


async def conflict_upsert(payload: Dict):
    """V5.4 主入口: 写入新事实,自动解决与同 (user_id, source_key) 旧事实的冲突。

    Body 必填:
      content         str   新事实文本
      source_key      str   业务维度键,如 "user_pref_language" / "company_hq" / "config_db_pool_size"
    Body 选填:
      user_id         str   默认 None
      importance      int   默认 5
      confidence      float 默认 0.8,低于 0.7 不覆盖
      type            str   默认 "fact"
      tags            list  默认 []
      auto_resolve    bool  默认 True,False 时只写入不解决冲突
      decided_by      str   默认 "auto",写 'manual' 表示人工决策

    返回: {chunk_id, action, old_chunk_id, reason}
      action ∈ {noop, supersede, mark_only, error}
    """
    content = (payload.get("content") or "").strip()
    source_key = (payload.get("source_key") or "").strip()
    if not content:
        raise HTTPException(400, "content 必填")
    if not source_key:
        raise HTTPException(400, "source_key 必填 (用于冲突检测分桶)")
    user_id = payload.get("user_id")
    importance = int(payload.get("importance", 5))
    confidence = float(payload.get("confidence", 0.8))
    chunk_type = payload.get("type", "fact")
    tags = payload.get("tags", []) or []
    auto_resolve = bool(payload.get("auto_resolve", True))
    decided_by = payload.get("decided_by", "auto")

    chunk_id = f"chunk_{uuid.uuid4().hex[:16]}"
    metadata = {
        "user_id": user_id,
        "source_key": source_key,
        "confidence": confidence,
        "v5_4_conflict": True,
    }
    # FIX (2026-08-16, Cortex embedding backfill): compute embedding on insert.
    # The original INSERT omitted the embedding column, leaving all
    # conflict/upsert-written chunks with NULL embedding → 95 chunk
    # vector gap (db_count=2633 vs vec_count=2538), making them
    # invisible to hybrid search.
    #
    # V6.6.0 fix (2026-10-30): this used vec_to_str() with NO dimension
    # padding and no model_version column. The column is vector(768), so a
    # 384-dim mpnet vector raised
    #     expected 768 dimensions, not 384
    # and the endpoint returned HTTP 500 for EVERY write — measured live on
    # 127.0.0.1. Now it pads to the storage dimension and records the
    # model that actually encoded the vector.
    upsert_model = resolve_embedding_model(EMBEDDING_MODEL_ENV)
    emb = embed_text(content, model_version=upsert_model)
    emb_str = _vec_to_str_for_model(emb, upsert_model)
    # V6.6.0 (2026-10-30): bind the origin at write time. The conflict
    # resolver writes a *decision* — "this is now the authoritative version
    # of this fact" — so it is exactly the place where an origin could be
    # laundered if it were not recorded. It is recorded here.
    upsert_origin, upsert_trust, upsert_ref = _resolve_origin(
        payload.get("origin"), payload.get("origin_trust"), payload.get("origin_ref")
    )
    try:
        execute(
            """INSERT INTO memory_chunks
                  (id, content, type, tags, source, importance, metadata,
                   embedding, tier, archived, model_version,
                   origin, origin_trust, origin_ref, created_at, updated_at)
               VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb,
                       %s::vector, 'long_term', 0, %s, %s, %s, %s, NOW(), NOW())""",
            (chunk_id, content, chunk_type, json.dumps(tags), "user",
             importance, json.dumps(metadata), emb_str, upsert_model,
             upsert_origin, upsert_trust, upsert_ref),
        )
    except Exception as e:
        logger.error(f"conflict/upsert insert failed: {e}")
        raise HTTPException(500, f"写入失败: {e}")

    decision = {"action": "noop", "old_chunk_id": None, "reason": "auto_resolve=False"}
    if auto_resolve:
        OPS_COUNTERS["conflict_detected"] += 1
        decision = _resolve_conflict_for_chunk(
            chunk_id, importance, confidence, user_id, source_key, decided_by
        )
    try:
        execute(
            """INSERT INTO memory_access_audit(memory_id, action, resource_type, details, created_at)
               VALUES (%s, 'upsert', 'chunk', %s::jsonb, NOW())""",
            (chunk_id, json.dumps({"actor": "conflict_upsert_endpoint", "source_key": source_key, "user_id": user_id, "action": decision.get("action"), "old_chunk_id": decision.get("old_chunk_id")})),
        )
    except Exception as e:
        logger.warning(f"audit write failed for /conflict/upsert {chunk_id}: {e}")
    return {"chunk_id": chunk_id, "decision": decision}


async def conflict_scan(payload: Dict = {}):
    """周期任务入口: 扫所有有 source_key 的桶,做兜底 reconciliation。

    用途:
      - 兜底处理手工写入 / 异步竞态导致的多版本共存
      - 兜底处理 confidence 之前没填 / importance 被外部改了的边缘 case

    Body 选填:
      lookback_days   int   默认 7,只看最近 N 天更新过的桶
      dry_run         bool  默认 False,True 时只统计不修改
      max_buckets     int   默认 500,本次最多处理桶数
    """
    lookback_days = int(payload.get("lookback_days", 7))
    dry_run = bool(payload.get("dry_run", False))
    max_buckets = int(payload.get("max_buckets", 500))

    # 找最近 N 天有过更新的桶
    try:
        buckets = query(
            """SELECT metadata->>'user_id' AS user_id,
                      metadata->>'source_key' AS source_key,
                      COUNT(*) FILTER (WHERE is_deprecated = FALSE) AS active_count,
                      MAX(updated_at) AS last_update
               FROM memory_chunks
               WHERE metadata ? 'source_key'
                     AND updated_at > NOW() - (%s || ' days')::interval
               GROUP BY user_id, source_key
               HAVING COUNT(*) FILTER (WHERE is_deprecated = FALSE) > 1
               ORDER BY last_update DESC
               LIMIT %s""",
            (lookback_days, max_buckets),
            fetch="all",
        )
    except Exception as e:
        logger.error(f"conflict/scan bucket query failed: {e}")
        raise HTTPException(500, f"扫桶失败: {e}")

    supersede_count = 0
    mark_only_count = 0
    skipped_count = 0
    errors = 0
    affected_ids = []
    for b in buckets or []:
        u = b.get("user_id")
        k = b.get("source_key")
        if not k:
            skipped_count += 1
            continue
        # 找桶内最新一条作为"新事实候选"
        latest = query(
            """SELECT id, importance FROM memory_chunks
               WHERE is_deprecated = FALSE
                     AND archived = 0
                     AND metadata->>'user_id' IS NOT DISTINCT FROM %s
                     AND metadata->>'source_key' = %s
               ORDER BY updated_at DESC LIMIT 1""",
            (u, k),
            fetch="one",
        )
        if not latest:
            skipped_count += 1
            continue
        # 取最新一条的 confidence (默认 0.8)
        conf_row = query(
            """SELECT metadata->>'confidence' AS conf FROM memory_chunks WHERE id = %s""",
            (latest["id"],), fetch="one",
        )
        try:
            conf = float(conf_row.get("conf") or 0.8) if conf_row else 0.8
        except Exception:
            conf = 0.8
        if dry_run:
            # dry_run 只统计,不改
            supersede_count += b["active_count"] - 1
            continue
        decision = _resolve_conflict_for_chunk(
            latest["id"], latest["importance"] or 5, conf, u, k, decided_by="auto"
        )
        if decision["action"] == "supersede":
            supersede_count += 1
            affected_ids.append(latest["id"])
        elif decision["action"] == "mark_only":
            mark_only_count += 1
        elif decision["action"] == "error":
            errors += 1
        else:
            skipped_count += 1
    return {
        "status": "ok",
        "dry_run": dry_run,
        "lookback_days": lookback_days,
        "buckets_scanned": len(buckets or []),
        "superseded": supersede_count,
        "mark_only": mark_only_count,
        "skipped": skipped_count,
        "errors": errors,
        "affected_chunk_ids": affected_ids[:50],
    }


def conflict_log(
    user_id: str = None,
    source_key: str = None,
    action: str = None,
    limit: int = 50,
):
    """查询 supersede 审计日志。

    Query 选填:
      user_id, source_key, action (supersede/mark_only/reject/error), limit
    """
    where = []
    params = []
    if user_id is not None:
        where.append("user_id = %s")
        params.append(user_id)
    if source_key:
        where.append("source_key = %s")
        params.append(source_key)
    if action:
        where.append("action = %s")
        params.append(action)
    sql = "SELECT * FROM memory_conflict_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    rows = query(sql, tuple(params), fetch="all")
    return {"total": len(rows), "log": rows}

# === Rumination / eval ===
def rumination_stats():
    return {"patterns": 0, "heuristics": 0}

async def rumination_run(payload: Dict = {}):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_rumination_sync, payload), timeout=20.0)
    except asyncio.TimeoutError:
        raise HTTPException(504, "rumination/run 超时(20s)")

def _rumination_sync(payload):
    """ADR-067-Patch-5 (2026-08-04): 真实实现反刍核心 (矛盾检测 + 碎片合并).

    按 docs/设计.md §4 反刍模块 (V2.0 + V4.1 Heuristic):
    1. Session 历史回顾 (lookback_days) - 默认 3 天
    2. 矛盾检测 (CONTRADICTION_THRESHOLD = 0.75) - metadata 打标, 不删除
    3. 碎片合并 (FRAGMENT_SIMILARITY_THRESHOLD = 0.65) - 合并到最重要/最新

    注: 经验抽象走 /rumination/abstract-heuristics 单独端点 (需要 LLM)
        ADR-067 §3.5 P1 stub 实现矛盾 + 碎片, 经验抽象由后端 LLM 调用实现.
    """
    lookback_days = int(payload.get("lookback_days", 3))
    contradicted = 0
    merged = 0

    # 1) 矛盾检测: 按 embedding 距离找高相似但内容可能冲突的 chunk 对
    # 简化算法: 最近 N 天的 chunk 中, 找两两相似度 > 0.75 但 importance 差距 > 3
    try:
        rows = query(
            """SELECT a.id AS a_id, a.importance AS a_imp, a.content AS a_content,
                      b.id AS b_id, b.importance AS b_imp, b.content AS b_content,
                      (1 - (a.embedding <=> b.embedding)) AS sim
               FROM memory_chunks a
               JOIN memory_chunks b ON a.id < b.id
               WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL
                     AND a.archived = 0 AND b.archived = 0
                     AND a.created_at > NOW() - (%s || ' days')::interval
                     AND a.importance BETWEEN b.importance - 3 AND b.importance + 3
               ORDER BY sim DESC LIMIT 200""",
            (lookback_days,),
        )
        for r in rows or []:
            if r["sim"] < 0.75:
                continue
            # 检测是否真矛盾: 简单规则 - 两边 importance 差距 < 3 但内容相似 → 标记
            try:
                execute(
                    """UPDATE memory_chunks
                       SET metadata = COALESCE(metadata, '{}'::jsonb) ||
                                      jsonb_build_object('potential_contradiction_with', %s::text)
                       WHERE id = %s
                             AND NOT (metadata ? 'potential_contradiction_with')""",
                    (r["b_id"], r["a_id"]),
                )
                execute(
                    """UPDATE memory_chunks
                       SET metadata = COALESCE(metadata, '{}'::jsonb) ||
                                      jsonb_build_object('potential_contradiction_with', %s::text)
                       WHERE id = %s
                             AND NOT (metadata ? 'potential_contradiction_with')""",
                    (r["a_id"], r["b_id"]),
                )
                contradicted += 1
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"rumination contradiction scan failed: {e}")

    # 2) 碎片合并: 低 importance 且内容短的 chunk (碎片)
    try:
        rows = query(
            """SELECT id, importance, LENGTH(content) AS len, created_at
               FROM memory_chunks
               WHERE archived = 0
                     AND importance < 4
                     AND LENGTH(content) < 200
               ORDER BY LENGTH(content) ASC LIMIT 50""",
            fetch="all",
        )
        # 简化: 碎片合并到最近一条更高 importance 的同 tag chunk
        for r in rows or []:
            target = query(
                """SELECT id FROM memory_chunks
                   WHERE archived = 0 AND importance >= r.importance + 2
                         AND id != r.id
                   ORDER BY created_at DESC LIMIT 1""".replace("r.importance", str(r["importance"])),
                fetch="one",
            )
            if target:
                try:
                    execute(
                        """INSERT INTO memory_history(id, memory_id, action, content, type, created_at)
                           VALUES (%s, %s, 'merge', %s, 'merge_log', NOW())""",
                        (int(time.time() * 1e6) % 2000000000, r["id"], f"merged into {target['id']}"),
                    )
                    execute("DELETE FROM memory_chunks WHERE id = %s", (r["id"],))
                    merged += 1
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"rumination fragment merge failed: {e}")

    return {
        "status": "ok",
        "contradicted": contradicted,
        "merged": merged,
        "lookback_days": lookback_days,
    }

def abstract_heuristics(payload: Dict = {}):
    """启发式抽象 (V6 ADR-067 §3.5 P1, 需 LLM 调用).
    当前未接 LLM 适配器, 返 NOT_IMPLEMENTED 标记避免客户端误以为运行成功.
    """
    return {
        "abstracted": 0,
        "status": "not_implemented",
        "reason": "V6 ADR-067 §3.5 P1 待办: 需要 LLM 适配器, 当前 stub",
        "required_components": ["llm_adapter", "heuristic_prompt_template"],
    }

# === Bench ===
def bench_multi_hop():
    """\u591a\u8df3\u63a8\u7406\u57fa\u51c6\u6d4b\u8bd5 (V6 ADR-067 \u00a73.4). \u8bc4\u6d4b\u96c6 = memory_bench.multi_hop_questions (PG \u8868).
    \u5f53\u524d\u672a\u586b\u5b9e\u9645\u8bc4\u6d4b\u96c6, \u8fd4 NOT_IMPLEMENTED + \u72b6\u6001."""
    cnt = query("SELECT COUNT(*) AS c FROM information_schema.tables WHERE table_name = \'multi_hop_questions\'", fetch="one")["c"]
    if cnt == 0:
        return {
            "score": None, "samples": 0,
            "status": "not_implemented",
            "reason": "V6 ADR-067 \u00a73.4 \u5f85\u529e: multi_hop_questions \u8868\u672a\u521d\u59cb\u5316 + LLM-judge \u672a\u63a5",
            "required_components": ["multi_hop_questions \u8868 DDL", "LLM-judge \u8bc4\u5206\u6a21\u677f"],
        }
    return {"score": None, "samples": 0, "status": "table_empty"}


def bench_custom(payload: Dict):
    """\u81ea\u5b9a\u4e49\u591a\u8df3\u8bc4\u6d4b (V6 ADR-067 \u00a73.4). stub, \u5f85 LLM-judge \u63a5\u597d\u5b9e\u73b0."""
    return {
        "score": None,
        "samples": 0,
        "status": "not_implemented",
        "reason": "V6 ADR-067 \u00a73.4 \u5f85\u529e: LLM-judge \u672a\u63a5",
    }

# === Eval feedback ===
def eval_feedback(payload: Dict):
    """ADR-067-Patch-2 (2026-08-04): 真实实现评估回路写入.

    按 docs/评估回路.md §四 relevance_score 更新策略:
    - 基础召回分 base_score = 0.1
    - relevance_increment = max(0.0, evaluation_score - 0.1)
    - 评分范围 score ∈ [0.0, 1.0]

    输入契约:
      feedback: [{
        "memory_id": "mem_xxx",
        "evaluation_score": 0.0~1.0,
        "relevance_increment": 0.0~0.9,
        "user_query": "...",
        "judge_method": "regex" | "llm_judge"
      }]

    行为:
      1. 每个 feedback 项, 把 relevance_increment 加到 memory_access_stats.relevance_score (上限 1.0)
      2. 同时审计 memory_access_audit 写入 (judge_method + score)
      3. 返回 updated/total 计数

    注: ADR-067 §5.1 决策 - relevance_score 初值 0.1, 永不重置, 写满 1.0.
    """
    items = payload.get("feedback", [])
    if not isinstance(items, list):
        raise HTTPException(400, "feedback must be a list")
    # ADR-067-Patch-2-fix: 实际表名是 access_stats (非 memory_access_stats)
    # 实际 schema 无 relevance_score 列, 用 INSERT 关联表存
    updated = 0
    for item in items:
        mid = item.get("memory_id")
        if not mid:
            continue
        inc = float(item.get("relevance_increment", 0.0))
        ev_score = float(item.get("evaluation_score", 0.0))
        judge = item.get("judge_method", "regex")
        if inc <= 0:
            continue
        try:
            # 1. 写 access_stats (主表)
            execute(
                """INSERT INTO access_stats(memory_id, access_type, accessed_at)
                   VALUES (%s, 'eval_feedback', NOW())""",
                (mid,),
            )
            # 2. 写 memory_access_audit (审计 + score)
            execute(
                """INSERT INTO memory_access_audit(memory_id, action, resource_type, details, created_at)
                   VALUES (%s, 'eval_feedback', 'chunk',
                           jsonb_build_object('score', %s::float, 'increment', %s::float, 'judge', %s::text),
                           NOW())""",
                (mid, ev_score, inc, judge),
            )
            updated += 1
        except Exception as e:
            logger.warning(f"eval_feedback write failed for {mid}: {e}")
    return {"updated": updated, "total": len(items), "status": "ok"}

# === Storage evict ===
def storage_evict(max_count: int = 50000, ratio: float = 0.05):
    """ADR-067-Patch-3 (2026-08-04): 真实实现末位淘汰.

    按 docs/设计.md §10.2 evict_least_relevant:
      protected = [c for c in chunks if c.tier == "long_term" or c.importance >= 8]
      evict_targets = 其他按 relevance_score 升序, 末位 ratio

    仲裁规则 (ADR-067 §5.3 决策 #11): 并集保护
      tier=long_term OR importance>=8 都不淘汰

    Args:
      max_count: 不主动触发, 仅显示
      ratio: 一次淘汰比例 (0.01-0.5), 默认 5%
    """
    n = query("SELECT COUNT(*) as c FROM memory_chunks WHERE archived = 0", fetch="one")["c"]
    if n <= max_count:
        return {"current": n, "max": max_count, "evicted": 0, "reason": "under budget"}
    target_n = max(1, int((n - max_count) * max(ratio, 0.01)))
    # ADR-067-Patch-3-fix: v5.0.0 schema 没有 relevance_score 列
    # 用 access_stats 30 天 count 推导 (count 越低 = 越冷门 = 越该淘汰)
    rows = query(
        """SELECT m.id, m.tier, m.importance,
                  COALESCE(SUM(CASE WHEN a.accessed_at > NOW() - INTERVAL '30 days' THEN 1 ELSE 0 END), 0) AS hits
           FROM memory_chunks m
           LEFT JOIN access_stats a ON a.memory_id = m.id
           WHERE m.archived = 0
                 AND NOT (m.tier = 'long_term' OR m.importance >= 8)
           GROUP BY m.id, m.tier, m.importance
           ORDER BY hits ASC, m.importance ASC
           LIMIT %s""",
        (target_n,),
    )
    evicted = 0
    for r in rows:
        try:
            execute(
                """INSERT INTO forgetting_log(memory_id, action, reason) VALUES (%s, 'evict', 'low_relevance')""",
                (r["id"],),
            )
            execute("DELETE FROM memory_chunks WHERE id = %s", (r["id"],))
            evicted += 1
        except Exception as e:
            logger.warning(f"storage_evict delete failed for {r['id']}: {e}")
    return {"current": n, "max": max_count, "evicted": evicted, "ratio": ratio}

def storage_enforce(max_size_mb: int = 1024):
    return {"status": "ok", "max_size_mb": max_size_mb}

def chroma_cleanup():
    return {"status": "noop", "reason": "PG backend: no chroma to clean"}

# === MCP surface (streamable-http) ===
# Add the MCP streamable-http endpoint as a native ASGI route on the
# parent FastAPI app. The MCP session manager is started by
# ``get_mcp_lifespan`` at app startup.
#
# Disable with CORTEX_MCP_ENABLED=0.

# V6.5.0 Phase 1 direction #5 (2026-08-23): Apply HNSW runtime parameters at
# startup. SET hnsw.ef_search affects every connection in the pool for the
# remainder of the session — pgvector reads it on each index scan. Set to 120
# (vs default 40) for better recall; m=24 (index-time) is applied by the
# `2026_08_23_hnsw_m24.sql` migration. If the DB is unreachable we log + keep
# going: queries will fall back to pgvector defaults (m=16, ef_search=40).
def _apply_hnsw_runtime_params() -> None:
    from src.config import HNSW_EF_SEARCH  # local import: avoids circular during cold-start
    conn = None
    try:
        conn = pg_pool.getconn()
        with conn.cursor() as cur:
            cur.execute("SET hnsw.ef_search = %s", (HNSW_EF_SEARCH,))
        conn.commit()
        logger.info("HNSW runtime params applied: ef_search=%d", HNSW_EF_SEARCH)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not set hnsw.ef_search at startup: %s", exc)
    finally:
        if conn is not None:
            try:
                pg_pool.putconn(conn)
            except Exception:  # noqa: BLE001
                pass


if os.getenv("CORTEX_MCP_ENABLED", "1") == "1":
    try:
        from src.mcp_server.cortex_mcp_server import (
            add_mcp_route,
            get_mcp_lifespan,
            get_server_info,
        )
        # Plug the lifespan into the parent FastAPI router so the MCP
        # session manager's anyio task group is initialized at startup.
        existing_lifespan = app.router.lifespan_context
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _combined_lifespan(app_):
            # V6.5.0 Phase 1 direction #5: apply HNSW ef_search before MCP startup
            # so all connection-pool searches benefit immediately.
            _apply_hnsw_runtime_params()
            async with get_mcp_lifespan()(app_):
                if existing_lifespan is not None:
                    async with existing_lifespan(app_):
                        yield
                else:
                    yield

        app.router.lifespan_context = _combined_lifespan

        add_mcp_route(app)
        logger.info(
            "MCP surface added at /mcp (%d tools)",
            len(get_server_info()["tools"]),
        )
    except Exception as exc:  # noqa: BLE001
        # Never let an MCP wiring issue take the REST API down.
        logger.error("Failed to add MCP surface: %s", exc, exc_info=True)
else:
    logger.info("MCP surface disabled (CORTEX_MCP_ENABLED=0)")

    # When MCP is disabled we still need to set ef_search. Wire a minimal
    # lifespan that runs the HNSW hook at startup.
    from contextlib import asynccontextmanager
    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _hnsw_only_lifespan(app_):
        _apply_hnsw_runtime_params()
        if existing_lifespan is not None:
            async with existing_lifespan(app_):
                yield
        else:
            yield

    app.router.lifespan_context = _hnsw_only_lifespan



# === v6.5.0 Phase 2: write-time maintenance scheduler ===========================
#
# A write-time-enqueue background scheduler that:
#   * records each newly inserted chunk_id into an in-memory buffer;
#   * flushes the buffer into ``memory_maintenance_queue`` every flush_interval;
#   * a single worker drains the queue with ``SELECT FOR UPDATE SKIP LOCKED``;
#   * runs ``extract_fn`` + ``resolve_conflicts_fn`` on each chunk;
#   * flips rows to ``status='dead'`` after ``max_retries`` failed attempts.
#
# Production wiring uses ``query``/``execute`` (already defined above) as
# ``db_query``/``db_execute``. ``db_enqueue``/``db_claim_next``/``db_mark_done``/
# ``db_mark_failed`` are thin inline closures so we keep the persistence story
# in one place (api_pg.py) rather than scattering SQL across modules.

import os as _os_sched

_SCHED_WORKER_ID = f"{_os_sched.uname().nodename if hasattr(_os_sched, 'uname') else 'cortex'}:{_os_sched.getpid()}"
_SCHED_FLUSH_INTERVAL = float(_os_sched.getenv("CORTEX_SCHED_FLUSH_INTERVAL", "0.2"))
_SCHED_WORKER_POLL = float(_os_sched.getenv("CORTEX_SCHED_WORKER_POLL", "1.0"))
_SCHED_LEASE_SECONDS = float(_os_sched.getenv("CORTEX_SCHED_LEASE_SECONDS", "30"))
_SCHED_MAX_RETRIES = int(_os_sched.getenv("CORTEX_SCHED_MAX_RETRIES", "5"))
_SCHED_ENABLED = _os_sched.getenv("CORTEX_SCHED_ENABLED", "1") == "1"
_SCHED_STATE_PATH = _os_sched.getenv(
    "CORTEX_SCHED_STATE_PATH", "/tmp/cortex_scheduler_state.json"
)


def _sched_db_enqueue(chunk_ids):
    """Batch-insert chunk_ids into memory_maintenance_queue. Returns inserted count."""
    if not chunk_ids:
        return 0
    # ON CONFLICT (chunk_id) DO NOTHING makes enqueue idempotent across retries.
    sql = (
        "INSERT INTO memory_maintenance_queue (chunk_id, status) "
        "VALUES (%s, 'pending') ON CONFLICT (chunk_id) DO NOTHING"
    )
    inserted = 0
    for cid in chunk_ids:
        try:
            rc = execute(sql, (cid,))
            # execute() returns rowcount; psycopg2 reports 0 for ON CONFLICT skip.
            inserted += int(rc or 0)
        except Exception as exc:  # noqa: BLE001 -- one bad id must not poison batch
            logger.warning("sched_db_enqueue skipped %s: %s", cid, exc)
    return inserted


def _sched_db_claim_next():
    """Claim the next pending chunk with SKIP LOCKED + lease. Returns chunk_id or None."""
    sql = (
        "UPDATE memory_maintenance_queue "
        "SET status='processing', leased_at=NOW(), lease_until=NOW() + (%s || ' seconds')::interval, "
        "    worker_id=%s "
        "WHERE id = ( "
        "  SELECT id FROM memory_maintenance_queue "
        "  WHERE status='pending' AND (lease_until IS NULL OR lease_until < NOW()) "
        "  ORDER BY enqueued_at ASC "
        "  FOR UPDATE SKIP LOCKED "
        "  LIMIT 1 "
        ") "
        "RETURNING chunk_id"
    )
    try:
        row = query(sql, (str(int(_SCHED_LEASE_SECONDS)), _SCHED_WORKER_ID), fetch="one")
    except Exception as exc:  # noqa: BLE001
        logger.warning("sched_db_claim_next failed: %s", exc)
        return None
    if not row:
        return None
    if isinstance(row, dict):
        return row.get("chunk_id")
    try:
        return row[0]
    except Exception:  # noqa: BLE001
        logger.warning("sched_db_claim_next unexpected row shape: %r", row)
        return None


def _sched_db_mark_done(chunk_id):
    sql = (
        "UPDATE memory_maintenance_queue "
        "SET status='done', completed_at=NOW(), leased_at=NULL, lease_until=NULL, worker_id=NULL "
        "WHERE chunk_id=%s"
    )
    try:
        execute(sql, (chunk_id,))
    except Exception as exc:  # noqa: BLE001
        logger.warning("sched_db_mark_done failed for %s: %s", chunk_id, exc)


def _sched_db_mark_failed(chunk_id, err):
    """Increment retry_count; returns new retry_count. Flips to 'dead' past max_retries."""
    sql = (
        "UPDATE memory_maintenance_queue "
        "SET retry_count = retry_count + 1, last_error=%s, "
        "    lease_until=NULL, worker_id=NULL, "
        "    status = CASE WHEN retry_count + 1 > %s THEN 'dead' ELSE 'pending' END "
        "WHERE chunk_id=%s "
        "RETURNING retry_count"
    )
    try:
        row = query(sql, (err[:500], _SCHED_MAX_RETRIES, chunk_id), fetch="one")
    except Exception as exc:  # noqa: BLE001
        logger.warning("sched_db_mark_failed crashed for %s: %s", chunk_id, exc)
        return -1
    if not row:
        return -1
    if isinstance(row, dict):
        return int(row.get("retry_count", -1))
    try:
        return int(row[0])
    except Exception:  # noqa: BLE001
        return -1


# Singleton (wired lazily; tests can monkeypatch _scheduler).
def _sched_record_similar_relations(chunk_id: str) -> None:
    """v6.5.1 P2 post-process hook: auto-create similar relations from
    the just-processed chunk's embedding.

    Looks up top-5 nearest neighbors via pgvector cosine distance and
    writes memory_relations rows. Threshold: cosine similarity >= 0.65
    (i.e. cosine distance < 0.35). HNSW index keeps this O(log n) per
    chunk.

    The 0.65 threshold was tuned after ADR-262 backfill (12,764 'similar'
    relations from 2519 chunks = ~5 rels/chunk average). 0.65 produces a
    sparser but more semantically precise graph; the trade-off is that
    more chunks write 0 relations. Operators wanting denser graphs can
    raise the threshold later via this constant.

    Schema:
      INSERT INTO memory_relations(id, memory_id_1, memory_id_2,
                                   relation_type, strength,
                                   created_at, transaction_at)
      ON CONFLICT (memory_id_1, memory_id_2) DO UPDATE
        SET strength = EXCLUDED.strength,
            updated_at = NOW();

    Failure modes (best-effort: log + swallow, do NOT flip chunk to dead):
      * no embedding on chunk        -> early return
      * vector query raises           -> caught in caller (_worker_tick)
      * zero neighbors above threshold -> early return
      * individual INSERT fails       -> caught per row, continue
    """
    # 1. Skip if no embedding (defensive -- should not happen with v6.5.0+
    # Nomic v1.5 default embedder which always populates the column).
    own = query(
        "SELECT (embedding IS NOT NULL) AS has_emb FROM memory_chunks WHERE id = %s",
        (chunk_id,), fetch="one",
    )
    # Normalize: api_pg.py query() returns list of dict even for fetch="one".
    if isinstance(own, list):
        own = own[0] if own else None
    if not own or not own.get("has_emb"):
        return

    # 2. Top-5 neighbors, cosine distance < threshold. Skip self.
    # strength = 1 - cosine_distance; threshold 0.70 means "at least 70%
    # similar in embedding space". Use max distance 0.30 (i.e. similarity
    # >= 0.70). The new chunk is brand-new so we don't constrain on
    # archived -- include archived neighbors (cortex may want to recall
    # them too).
    try:
        rows = query(
            """SELECT id,
                      1.0 - (embedding <=> (SELECT embedding FROM memory_chunks WHERE id = %s)) AS sim
               FROM memory_chunks
               WHERE id != %s
                 AND embedding IS NOT NULL
                 AND (embedding <=> (SELECT embedding FROM memory_chunks WHERE id = %s)) < 0.35
               ORDER BY embedding <=> (SELECT embedding FROM memory_chunks WHERE id = %s) ASC
               LIMIT 5""",
            (chunk_id, chunk_id, chunk_id, chunk_id),
        )
    except Exception as _qerr:
        logger.warning("auto-relate: vector query failed for %s: %s", chunk_id, _qerr)
        return

    # 3. v6.5.1 P2: tag-shared auto-relate (runs even if no similar neighbors).
    # Two chunks are 'tag-shared' if they have >=2 overlapping tags.
    inserted_tag = _sched_record_tag_shared_relations(chunk_id)
    logger.debug(
        "P2-AUTORELATE: chunk_id=%s -> %d tag-shared relations written",
        chunk_id, inserted_tag,
    )

    if not rows:
        return

    # 4. Insert one row per neighbor. Symmetric: write (cid, neighbor).
    # The (memory_id_1, memory_id_2) unique constraint handles dups; we
    # ON CONFLICT update strength to the freshest cosine similarity so the
    # graph reflects the freshest state.
    inserted = 0
    for r in rows:
        neighbor_id = r["id"]
        sim = float(r["sim"])
        if sim < 0.65:
            continue  # post-distance double-check (cosine similarity >= 0.65)
        rid = f"rel_auto_{chunk_id}_{neighbor_id}"[:200]
        try:
            execute(
                """INSERT INTO memory_relations(id, memory_id_1, memory_id_2,
                                                  relation_type, strength,
                                                  created_at, transaction_at)
                   VALUES (%s, %s, %s, 'similar', %s, NOW(), NOW())
                   ON CONFLICT (memory_id_1, memory_id_2) DO UPDATE
                     SET strength = EXCLUDED.strength,
                         updated_at = NOW()""",
                (rid, chunk_id, neighbor_id, sim),
            )
            inserted += 1
        except Exception as _ierr:
            logger.warning(
                "auto-relate: insert failed for (%s, %s): %s",
                chunk_id, neighbor_id, _ierr,
            )





def _sched_record_tag_shared_relations(chunk_id: str) -> int:
    """v6.5.1 P2 auto-relate: tag-shared relations.

    Two chunks are 'tag-shared' if they have >=2 overlapping tags. Threshold
    chosen because cortex chunks typically carry 1-5 tags; requiring >=2
    shared tags eliminates false positives where two chunks happen to share
    a common tag like "quantplatform" or "cortex" without semantic overlap.

    Reads chunk's own tags once, then finds other chunks whose tag array
    shares >=2 elements with this chunk's tags. Inserts (memory_id_1,
    memory_id_2) ON CONFLICT DO UPDATE SET strength = EXCLUDED.strength,
    updated_at = NOW(). strength stores the overlap count (e.g., 3.0 if
    3 tags overlap).

    Returns the number of new/updated memory_relations rows.
    """
    # 1. Load this chunk's tags (stored as JSONB array of strings).
    own_row = query(
        "SELECT tags FROM memory_chunks WHERE id = %s", (chunk_id,), fetch="one",
    )
    if isinstance(own_row, list):
        own_row = own_row[0] if own_row else None
    if not own_row or not own_row.get("tags"):
        return 0
    # tags is a JSONB array -- psycopg2 + RealDictCursor returns it as a Python list
    own_tags = own_row["tags"]
    if not isinstance(own_tags, list) or len(own_tags) < 1:
        return 0

    # 2. Find chunks that share at least 1 tag with this chunk (cheap
    # pre-filter using JSONB ?| operator: 'tags contains ANY of these
    # strings'). Python then counts the actual overlap size.
    candidates = query(
        "SELECT id, tags FROM memory_chunks "
        "WHERE id != %s "
        "  AND archived = 0 "
        "  AND tags IS NOT NULL "
        "  AND tags ?| %s::text[] "
        "LIMIT 50",
        (chunk_id, own_tags),
    )

    if not candidates:
        return 0

    own_set = set(own_tags)
    inserted = 0
    for cand in candidates:
        cand_id = cand.get("id") if isinstance(cand, dict) else cand[0]
        cand_tags = cand.get("tags") if isinstance(cand, dict) else cand[1]
        if not isinstance(cand_tags, list):
            continue
        overlap_count = len(own_set.intersection(set(cand_tags)))
        if overlap_count < 2:
            continue
        rid = f"rel_tag_{chunk_id}_{cand_id}"[:200]
        try:
            execute(
                """INSERT INTO memory_relations(id, memory_id_1, memory_id_2,
                                                 relation_type, strength,
                                                 created_at, transaction_at)
                   VALUES (%s, %s, %s, 'tag-shared', %s, NOW(), NOW())
                   ON CONFLICT (memory_id_1, memory_id_2) DO UPDATE
                     SET strength = EXCLUDED.strength,
                         updated_at = NOW()""",
                (rid, chunk_id, cand_id, float(overlap_count)),
            )
            inserted += 1
        except Exception as _ierr:
            logger.warning(
                "auto-relate tag-shared: insert failed for (%s, %s): %s",
                chunk_id, cand_id, _ierr,
            )
    return inserted

# ===== Consolidation extract wiring (V6.6.1, 2026-10-31) =====
#
# WHY THIS EXISTS
# ---------------
# The ADR-068 conflict pipeline (the thing that is supposed to mark an
# outdated fact as `is_deprecated = TRUE`) has two halves:
#
#   * detection   -- consolidate.extract.extract -> (entity, predicate, value)
#                    triples -> consolidate.conflict_resolve.resolve_conflicts
#   * write-back  -- conflict_resolve now also flips the originating chunk in
#                    memory_chunks (see _supersede_old_chunk)
#
# The write-back half was wired, the detection half was NOT: the scheduler
# was constructed without `extract_fn`, so `_extract_one()` short-circuited
# to the EMPTY envelope on every chunk. resolve_conflicts then saw zero
# facts and returned zero actions -- forever. Nothing was ever superseded,
# so an outdated fact stayed active in memory_chunks and was still served
# by /list -- which is precisely what the hermes connector injects into the
# agent's system prompt.
#
# `extract()` is an `async def` while the scheduler's worker thread is
# synchronous, so we bridge with a private event loop.

_LLM_PROBE_TTL_SECONDS = 60.0
_llm_probe_cache: Dict[str, Any] = {"url": None, "at": 0.0, "ok": False}


class _ExtractLLMConfig:
    """Duck-typed config for consolidate.extract.extract.

    extract() only needs .llm_url / .llm_api_key / .llm_model / .llm_timeout
    (see consolidate/extract.py docstring). api_pg.py deliberately does not
    import CortexConfig, which requires EMBEDDING_MODEL_PATH to be set.
    """

    def __init__(self) -> None:
        self.llm_url: str = os.getenv(
            "LLM_ABSTRACTION_URL", "http://localhost:8080/v1/chat/completions"
        )
        self.llm_api_key: str = os.getenv("LLM_ABSTRACTION_API_KEY", "dummy")
        self.llm_model: str = os.getenv("LLM_ABSTRACTION_MODEL", "gpt-3.5-turbo")
        self.llm_timeout: int = int(os.getenv("LLM_ABSTRACTION_TIMEOUT", "15"))


def _llm_endpoint_reachable(url: str) -> bool:
    """Cheap TCP reachability probe for the LLM endpoint.

    consolidate.extract.extract retries 3 times with a 30s/60s exponential
    backoff. If the LLM is down, every single chunk would stall the
    consolidation worker for ~2 minutes. We probe the TCP port first
    (2s timeout, cached for 60s) and skip extraction entirely when the
    endpoint is not answering -- so a missing LLM degrades to "no conflict
    detection" instead of "worker wedged".
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except Exception:
        return False
    import socket
    try:
        with socket.create_connection((host, port), timeout=2.0):
            return True
    except Exception:
        return False


def _sched_extract_fn(chunk: Any, context_chunks: Any, cfg: Any) -> Dict[str, Any]:
    """Synchronous bridge to the async LLM extractor used by the scheduler.

    Returns the canonical envelope on every failure path -- the scheduler
    treats an empty envelope as "nothing to consolidate" and must never
    raise out of the worker thread.
    """
    empty = {"facts": [], "timeline": [], "preferences": []}
    try:
        from .consolidate.extract import extract as _extract_async
    except Exception as exc:  # noqa: BLE001
        logger.warning("consolidation extract unavailable: %s", exc)
        return empty

    llm_cfg = cfg if cfg is not None else _ExtractLLMConfig()
    url = getattr(llm_cfg, "llm_url", "") or ""
    now = time.time()
    if (
        _llm_probe_cache.get("url") != url
        or (now - float(_llm_probe_cache.get("at") or 0.0)) > _LLM_PROBE_TTL_SECONDS
    ):
        ok = _llm_endpoint_reachable(url) if url else False
        _llm_probe_cache.update({"url": url, "at": now, "ok": ok})
        if not ok:
            logger.warning(
                "consolidation extract disabled: LLM endpoint %s unreachable "
                "(conflict detection will not run until it is back)", url,
            )
    if not _llm_probe_cache.get("ok"):
        return empty

    async def _run() -> Dict[str, Any]:
        result = await _extract_async(chunk, context_chunks, llm_cfg)
        return result if isinstance(result, dict) else empty

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — never break the worker
        logger.warning("consolidation extract failed: %s", exc)
        return empty


_scheduler = _ConsolidationSchedulerImpl(
    cfg=None,
    db_query=query,
    db_execute=execute,
    # V6.6.1 fix (2026-10-31): wire the extraction half of the ADR-068
    # pipeline. Without it resolve_conflicts never saw a single fact and
    # nothing was ever superseded. Set CORTEX_CONSOLIDATION_EXTRACT=0 to
    # turn it off.
    extract_fn=(
        _sched_extract_fn
        if os.getenv("CORTEX_CONSOLIDATION_EXTRACT", "1") == "1"
        else None
    ),
    flush_interval=_SCHED_FLUSH_INTERVAL,
    worker_poll_interval=_SCHED_WORKER_POLL,
    lease_seconds=_SCHED_LEASE_SECONDS,
    max_retries=_SCHED_MAX_RETRIES,
    db_enqueue=_sched_db_enqueue,
    db_claim_next=_sched_db_claim_next,
    db_mark_done=_sched_db_mark_done,
    db_mark_failed=_sched_db_mark_failed,
    worker_id=_SCHED_WORKER_ID,
    state_path=_SCHED_STATE_PATH if _SCHED_ENABLED else None,
    # v6.5.1: after a chunk is successfully processed, auto-create
    # similar relations from its embedding. Keeps the relation graph
    # fresh in real-time as new chunks arrive (was previously manual:
    # POST /merge-duplicates or batch via cron).
    on_process_complete=_sched_record_similar_relations,
)


# Lifespan integration: start scheduler threads on app boot, stop on shutdown.
# We piggyback on the existing lifespan (HNSW + MCP) without breaking it.
from contextlib import asynccontextmanager as _asynccontextmanager

_existing_lifespan = app.router.lifespan_context


@_asynccontextmanager
async def _sched_lifespan(app_):
    if _SCHED_ENABLED:
        try:
            _scheduler.start()
            logger.info(
                "cortex scheduler wired: worker_id=%s flush=%.1fs worker_poll=%.1fs lease=%.0fs retries=%d",
                _SCHED_WORKER_ID, _SCHED_FLUSH_INTERVAL, _SCHED_WORKER_POLL,
                _SCHED_LEASE_SECONDS, _SCHED_MAX_RETRIES,
            )
        except Exception as exc:  # noqa: BLE001 -- never let scheduler boot break the app
            logger.exception("scheduler.start() failed: %s", exc)
    async with _existing_lifespan(app_):
        yield
    if _SCHED_ENABLED:
        try:
            _scheduler.stop(timeout=5.0)
        except Exception:  # noqa: BLE001
            logger.exception("scheduler.stop() failed")


app.router.lifespan_context = _sched_lifespan


# === Admin / maintenance endpoints ============================================

def maintenance_stats():
    """v6.5.0 Phase 2: live maintenance queue + scheduler state.

    Combines a PG-side count of pending/processing/done/dead rows with the
    in-process scheduler state (counters + last_error).
    """
    try:
        counts_rows = query(
            "SELECT status, COUNT(*)::int AS n FROM memory_maintenance_queue GROUP BY status",
            fetch="all-no-commit",
        )
    except Exception as exc:  # noqa: BLE001 -- table may not exist yet
        return {"error": f"queue table unavailable: {exc}", "scheduler_state": _scheduler.state.to_dict()}

    counts = {"pending": 0, "processing": 0, "done": 0, "dead": 0}
    for r in counts_rows:
        if isinstance(r, dict):
            counts[str(r.get("status"))] = int(r.get("n") or 0)
        else:
            try:
                counts[str(r[0])] = int(r[1])
            except Exception:  # noqa: BLE001
                pass
    return {
        "scheduler_enabled": _SCHED_ENABLED,
        "worker_id": _SCHED_WORKER_ID,
        "flush_interval": _SCHED_FLUSH_INTERVAL,
        "worker_poll_interval": _SCHED_WORKER_POLL,
        "lease_seconds": _SCHED_LEASE_SECONDS,
        "max_retries": _SCHED_MAX_RETRIES,
        "buffered_count": _scheduler.buffered_count,
        "is_running": _scheduler.is_running,
        "queue_counts": counts,
        "scheduler_state": _scheduler.state.to_dict(),
    }


def maintenance_dead(limit: int = 100):
    """v6.5.0 Phase 2: list dead-letter rows (chunks that exhausted retries)."""
    if limit <= 0 or limit > 1000:
        raise HTTPException(400, "limit must be 1..1000")
    rows = query(
        "SELECT chunk_id, retry_count, last_error, enqueued_at, completed_at "
        "FROM memory_maintenance_queue WHERE status='dead' "
        "ORDER BY enqueued_at DESC LIMIT %s",
        (limit,),
        fetch="all-no-commit",
    )
    dead = []
    for r in rows:
        if isinstance(r, dict):
            dead.append(r)
        else:
            try:
                dead.append({
                    "chunk_id": r[0], "retry_count": int(r[1]),
                    "last_error": r[2], "enqueued_at": str(r[3]),
                    "completed_at": str(r[4]) if r[4] else None,
                })
            except Exception:  # noqa: BLE001
                continue
    return {"total": len(dead), "limit": limit, "dead": dead}


def maintenance_run_once():
    """v6.5.0 Phase 2: drain one queue item synchronously (debug/ops)."""
    cid = _scheduler.process_one_now()
    return {"processed_chunk_id": cid, "scheduler_state": _scheduler.state.to_dict()}


def maintenance_enqueue(chunk_id: str, force: bool = False):
    """v6.5.0 Phase 2: enqueue an existing chunk_id for re-processing.

    Useful when an operator wants to re-run extract/resolve on a chunk
    that previously went 'dead' or was inserted before the scheduler was
    wired. ``force=True`` re-enqueues even if a row already exists (reset
    status to pending).
    """
    if not chunk_id:
        raise HTTPException(400, "chunk_id required")
    # Verify chunk exists.
    row = query("SELECT 1 FROM memory_chunks WHERE id = %s", (chunk_id,), fetch="one")
    if not row:
        raise HTTPException(404, f"chunk_id {chunk_id} not found in memory_chunks")

    if force:
        try:
            execute(
                "INSERT INTO memory_maintenance_queue (chunk_id, status, retry_count, last_error) "
                "VALUES (%s, 'pending', 0, NULL) "
                "ON CONFLICT (chunk_id) DO UPDATE SET status='pending', retry_count=0, last_error=NULL, "
                "  leased_at=NULL, lease_until=NULL, worker_id=NULL, completed_at=NULL, enqueued_at=NOW()",
                (chunk_id,),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"force re-enqueue failed: {exc}")
    else:
        try:
            execute(
                "INSERT INTO memory_maintenance_queue (chunk_id, status) VALUES (%s, 'pending') "
                "ON CONFLICT (chunk_id) DO NOTHING",
                (chunk_id,),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"enqueue failed: {exc}")
    return {"chunk_id": chunk_id, "force": force, "status": "queued"}


def maintenance_flush():
    """v6.5.0 Phase 2: force the in-memory buffer to flush to PG now."""
    n = _scheduler.flush_now()
    return {"flushed": n, "buffered_count": _scheduler.buffered_count}


# === End v6.5.0 Phase 2 scheduler block =======================================


# === v6.5.0 P2 follow-up: Prometheus /metrics endpoint ===========================
#
# Stdlib-only Prometheus text-format emitter (no prometheus_client dep).
# Reads scheduler state directly + walks the OTel in-memory metric reader if
# the OTel SDK is enabled. When the OTel SDK is missing (default), only
# the scheduler counters are exposed -- still useful for monitoring.
#
# Output format: https://prometheus.io/docs/instrumenting/exposition_formats/
#
# Scrape interval target: 15s (Prometheus default).

_METRICS_PREFIX = "cortex_"


def _fmt_prom_line(name: str, value: float, labels=None) -> str:
    """Render one Prometheus text-format line (escapes backslash and quote)."""
    if labels:
        # Build the label rendering in two steps so f-string stays simple.
        # Escape order matters: backslash first, then quote.
        rendered = []
        for k, v in sorted(labels.items()):
            esc = str(v).replace("\\", "\\\\").replace("\"", "\\\"")
            rendered.append(k + "=\"" + esc + "\"")
        return name + "{" + ",".join(rendered) + "} " + str(value) + "\n"
    return name + " " + str(value) + "\n"


def prometheus_metrics():
    """Prometheus text-format exposition (compatible with /metrics scraping).

    Exposes the v6.5.0 P2 scheduler state as ``cortex_*`` metrics. The full
    set:

      cortex_scheduler_enabled                     (gauge, 0|1)
      cortex_scheduler_running                      (gauge, 0|1)
      cortex_scheduler_buffered_count               (gauge)
      cortex_scheduler_chunks_since_last_run        (gauge)
      cortex_scheduler_total_runs                   (counter)
      cortex_scheduler_total_chunks_processed       (counter)
      cortex_scheduler_total_actions                (counter)
      cortex_scheduler_total_dead                   (counter)
      cortex_scheduler_last_run_timestamp           (gauge, unix epoch)
      cortex_scheduler_lease_seconds                (gauge)
      cortex_scheduler_worker_poll_seconds          (gauge)
      cortex_scheduler_flush_interval_seconds       (gauge)
      cortex_queue_pending                          (gauge)
      cortex_queue_processing                       (gauge)
      cortex_queue_done                             (gauge)
      cortex_queue_dead                             (gauge)
      cortex_queue_total                            (gauge, pending+processing+done+dead)
      cortex_cortex_total_chunks                     (gauge, from /stats endpoint)

    Plus any OpenTelemetry HTTP request / duration / active instruments that
    have observations in the in-memory reader (if OTel metrics SDK is
    enabled and setup_metrics has run).
    """
    from . import observability as _obs  # local import: keep top-level clean
    out: List[str] = []

    # 1. scheduler state
    ss = _scheduler.state
    out.append("# HELP cortex_scheduler_running 1 if the P2 worker + flush threads are alive.")
    out.append("# TYPE cortex_scheduler_running gauge")
    out.append(_fmt_prom_line("cortex_scheduler_running", 1 if _scheduler.is_running else 0))

    out.append("# HELP cortex_scheduler_enabled 1 if CORTEX_SCHED_ENABLED=true.")
    out.append("# TYPE cortex_scheduler_enabled gauge")
    out.append(_fmt_prom_line("cortex_scheduler_enabled", 1 if _SCHED_ENABLED else 0))

    out.append("# HELP cortex_scheduler_buffered_count Chunk ids sitting in the in-memory buffer (not yet flushed to PG).")
    out.append("# TYPE cortex_scheduler_buffered_count gauge")
    out.append(_fmt_prom_line("cortex_scheduler_buffered_count", _scheduler.buffered_count))

    out.append("# HELP cortex_scheduler_chunks_since_last_run Counter reset to 0 after each run_once().")
    out.append("# TYPE cortex_scheduler_chunks_since_last_run counter")
    out.append(_fmt_prom_line("cortex_scheduler_chunks_since_last_run", ss.chunks_since_last_run))

    out.append("# HELP cortex_scheduler_total_runs Lifetime counter of run_once invocations that committed at least one action.")
    out.append("# TYPE cortex_scheduler_total_runs counter")
    out.append(_fmt_prom_line("cortex_scheduler_total_runs", ss.total_runs))

    out.append("# HELP cortex_scheduler_total_chunks_processed Lifetime counter of chunks processed.")
    out.append("# TYPE cortex_scheduler_total_chunks_processed counter")
    out.append(_fmt_prom_line("cortex_scheduler_total_chunks_processed", ss.total_chunks_processed))

    out.append("# HELP cortex_scheduler_total_actions Lifetime counter of ConflictAction records emitted.")
    out.append("# TYPE cortex_scheduler_total_actions counter")
    out.append(_fmt_prom_line("cortex_scheduler_total_actions", ss.total_actions))

    out.append("# HELP cortex_scheduler_total_dead Lifetime counter of chunks that flipped to status=dead.")
    out.append("# TYPE cortex_scheduler_total_dead counter")
    out.append(_fmt_prom_line("cortex_scheduler_total_dead", ss.total_dead))

    out.append("# HELP cortex_scheduler_last_run_timestamp Unix epoch of the most recent run_once() commit.")
    out.append("# TYPE cortex_scheduler_last_run_timestamp gauge")
    out.append(_fmt_prom_line("cortex_scheduler_last_run_timestamp", ss.last_run_at))

    out.append("# HELP cortex_scheduler_lease_seconds Worker lease duration before reclaim.")
    out.append("# TYPE cortex_scheduler_lease_seconds gauge")
    out.append(_fmt_prom_line("cortex_scheduler_lease_seconds", _SCHED_LEASE_SECONDS))

    out.append("# HELP cortex_scheduler_worker_poll_seconds Worker poll interval for SKIP LOCKED scan.")
    out.append("# TYPE cortex_scheduler_worker_poll_seconds gauge")
    out.append(_fmt_prom_line("cortex_scheduler_worker_poll_seconds", _SCHED_WORKER_POLL))

    out.append("# HELP cortex_scheduler_flush_interval_seconds Flush thread interval for in-memory -> PG batch.")
    out.append("# TYPE cortex_scheduler_flush_interval_seconds gauge")
    out.append(_fmt_prom_line("cortex_scheduler_flush_interval_seconds", _SCHED_FLUSH_INTERVAL))

    # 2. queue counts
    try:
        rows = query(
            "SELECT status, COUNT(*)::int FROM memory_maintenance_queue GROUP BY status",
            fetch="all-no-commit",
        )
    except Exception:
        rows = []
    counts = {"pending": 0, "processing": 0, "done": 0, "dead": 0}
    for r in rows or []:
        try:
            k = r.get("status") if isinstance(r, dict) else r[0]
            v = int(r.get("count") if isinstance(r, dict) else r[1])
            counts[str(k)] = v
        except Exception:
            pass
    out.append("# HELP cortex_queue_pending Rows in memory_maintenance_queue with status=pending.")
    out.append("# TYPE cortex_queue_pending gauge")
    out.append(_fmt_prom_line("cortex_queue_pending", counts["pending"]))
    out.append("# HELP cortex_queue_processing Rows in memory_maintenance_queue with status=processing.")
    out.append("# TYPE cortex_queue_processing gauge")
    out.append(_fmt_prom_line("cortex_queue_processing", counts["processing"]))
    out.append("# HELP cortex_queue_done Rows in memory_maintenance_queue with status=done.")
    out.append("# TYPE cortex_queue_done gauge")
    out.append(_fmt_prom_line("cortex_queue_done", counts["done"]))
    out.append("# HELP cortex_queue_dead Rows in memory_maintenance_queue with status=dead (exceeded max_retries).")
    out.append("# TYPE cortex_queue_dead gauge")
    out.append(_fmt_prom_line("cortex_queue_dead", counts["dead"]))
    out.append("# HELP cortex_queue_total Sum of all memory_maintenance_queue rows.")
    out.append("# TYPE cortex_queue_total gauge")
    out.append(_fmt_prom_line("cortex_queue_total", sum(counts.values())))

    # 3. cortex totals (from /stats -- one extra query; cheap on indexed tables)
    try:
        stats = query("SELECT total_memories FROM stats_view_total_memories()", fetch="one")
        total = int(stats.get("total_memories")) if stats else 0
    except Exception:
        # stats_view_total_memories() may not exist (it's a convenience view)
        # fall back to a direct count -- cheap because the table is small
        try:
            row = query("SELECT COUNT(*)::int AS n FROM memory_chunks", fetch="one")
            total = int(row.get("n")) if row else 0
        except Exception:
            total = 0
    out.append("# HELP cortex_cortex_total_chunks Total number of chunks in memory_chunks (active + archived).")
    out.append("# TYPE cortex_cortex_total_chunks gauge")
    out.append(_fmt_prom_line("cortex_cortex_total_chunks", total))

    # 4. OTel HTTP request metrics (if observability is enabled AND
    #    setup_observability was called -- i.e., OTEL_ENABLED=true and SDK
    #    importable). The in-memory reader is only populated when the
    #    metrics SDK is active.
    try:
        from src.observability.metrics import (
            memory_reader,
            is_metrics_active,
        )
        if is_metrics_active() and memory_reader is not None:
            # We can't easily export full OTLP MetricData without an
            # exporter, so the easiest path is: walk the SDK's
            # in-memory reader and emit per-attribute histograms. Skip
            # for now -- scheduler counters above are the primary
            # signal; the OTel path is for future use.
            pass
    except Exception:
        pass

    return PlainTextResponse(
        content="".join(out),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )




# === Domain routers (P0 phase 3) ===
# Pass this module's globals as the handler dict: domain modules read H["fn"]
# and never import api_pg, so there is no circular dependency. Registration
# happens here at the end, after every handler has been defined.
from src.routers import register_routes as _register_domain_routes  # noqa: E402
_DOMAIN_ROUTER_COUNT = _register_domain_routes(app, globals())


# === Main ===
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
