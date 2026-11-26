"""Background ETL: re-embed old `mpnet` chunks with Nomic v1.5.

V6.5.0 Phase 1 direction #3 (ROI 8, 3.5 人天) — Nomic v1.5 embedding upgrade.

Why background:
- 2638 chunks × ~50 ms Nomic inference ~= 2-3 minutes wall-clock.
- Foreground re-embed would block /search / /chunks during the migration.
- Background job + incremental batches lets /search continue serving traffic.

Spec reference:
- docs/改进方案-cortex-v6.5.0-完整版-20260822.md §6 (方向 #3)
- workflow-graph/tasks/v6.5.0-phase-1-implementation/spec.md (方向 #3)

Usage (CLI):
    python -m server.src.etl.etl_reembed --batch-size 50 --max-batches 5
    python -m server.src.etl.etl_reembed --dry-run
    python -m server.src.etl.etl_reembed --target-ratio 0.10   # 10% A/B rollout

Design:
- Imports `embed_text()` lazily so the embedding model is loaded once
  per call (not at module import — saves cold start).
- Per-batch transaction: SELECT batch → re-embed → UPDATE batch → commit.
  Partial failure of one batch does NOT poison the next batch.
- Throttled by `--sleep-ms` between batches (avoid CPU/GPU contention).
- Idempotent: running twice on the same chunk is a no-op (already updated).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

# Allow running as a standalone script:
#   python -m server.src.etl.etl_reembed
# Add the repo root to sys.path so we can import server.src.api_pg.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("etl_reembed")

# Default source/target model versions (kept as module constants for testability).
DEFAULT_SOURCE_MODEL = "mpnet"     # current MiniLM-L12-v2 ONNX
DEFAULT_TARGET_MODEL = "nomic-v1.5"  # Nomic v1.5 (HF Sentence-Transformers)


def _fetch_batch(
    source_model: str,
    batch_size: int,
    db_query,
) -> List[dict]:
    """Fetch the next batch of chunks still embedded with source_model.

    Skips archived chunks and anything already on the target model.
    Returns a list of {id, content} dicts.
    """
    rows = db_query(
        """SELECT id, content FROM memory_chunks
           WHERE archived = 0 AND model_version = %s
           ORDER BY created_at ASC
           LIMIT %s""",
        (source_model, batch_size),
        fetch="all-no-commit",
    )
    return [{"id": r["id"], "content": r["content"]} for r in rows or []]


def _update_chunk_embedding(
    chunk_id: str,
    embedding_str: str,
    target_model: str,
    db_execute,
) -> None:
    """Re-write the chunk's embedding + flip model_version atomically."""
    db_execute(
        """UPDATE memory_chunks
           SET embedding = %s::vector, model_version = %s, updated_at = NOW()
           WHERE id = %s""",
        (embedding_str, target_model, chunk_id),
    )


def reembed_batch(
    db_query,
    db_execute,
    embed_fn,
    vec_to_str_fn,
    vec_to_storage_str_fn=None,
    batch_size: int = 50,
    source_model: str = DEFAULT_SOURCE_MODEL,
    target_model: str = DEFAULT_TARGET_MODEL,
    max_batches: Optional[int] = None,
    sleep_ms: int = 0,
    dry_run: bool = False,
    target_ratio: Optional[float] = None,
    total_count_provider=None,
) -> dict:
    """Run the ETL re-embed loop.

    Args:
        db_query: `query(sql, params, fetch)` from api_pg (or a stub for tests).
        db_execute: `execute(sql, params)` from api_pg (or a stub).
        embed_fn: `embed_text(text, model_version=...)` from api_pg.
        vec_to_str_fn: `vec_to_str(vec)` from api_pg.
        batch_size: chunks per transaction (default 50).
        source_model: chunks currently on this model will be re-embedded.
        target_model: chunks will be flipped to this model after re-embed.
        max_batches: stop after N batches (None = run until no more stale chunks).
        sleep_ms: pause between batches in milliseconds (avoid CPU spikes).
        dry_run: if True, log intent but do NOT write.
        target_ratio: A/B rollout cap — stop after this fraction of all chunks
                      has been re-embedded (e.g. 0.10 = 10%).
        total_count_provider: callable returning total active chunk count for
                              target_ratio calculation. If None, ratio is skipped.

    Returns:
        Summary dict: {batches, reembedded, skipped, dry_run, errors,
                       target_model, source_model, total_elapsed_ms}.
    """
    t0 = time.time()
    batches = 0
    reembedded = 0
    skipped = 0
    errors: List[str] = []

    while True:
        if max_batches is not None and batches >= max_batches:
            break

        # Rollout cap (target_ratio): count of chunks already on target_model
        if target_ratio is not None and total_count_provider is not None:
            try:
                total = total_count_provider()
                target_already = (
                    db_query(
                        "SELECT COUNT(*) AS c FROM memory_chunks "
                        "WHERE archived = 0 AND model_version = %s",
                        (target_model,),
                        fetch="one",
                    ) or {}
                ).get("c", 0) or 0
                if total > 0 and (target_already / total) >= target_ratio:
                    logger.info(
                        "target_ratio %.2f%% reached (already %.2f%%), stop.",
                        target_ratio * 100,
                        (target_already / total) * 100,
                    )
                    break
            except Exception as e:
                logger.warning("target_ratio check failed (continue anyway): %s", e)

        try:
            batch = _fetch_batch(source_model, batch_size, db_query)
        except Exception as e:
            logger.error("fetch_batch failed: %s", e)
            errors.append(f"fetch: {e}")
            break

        if not batch:
            logger.info("No more chunks on model_version=%s, done.", source_model)
            break

        if dry_run:
            logger.info(
                "DRY-RUN batch %d: would re-embed %d chunks (id range %s..%s)",
                batches + 1,
                len(batch),
                batch[0]["id"],
                batch[-1]["id"],
            )
            batches += 1
            skipped += len(batch)
            continue

        # Re-embed the batch + flip model_version
        try:
            for row in batch:
                emb = embed_fn(row["content"], model_version=target_model)
                # V6.5.0 direction #3: ETL always targets Nomic (768-dim),
                # so the vector already matches the column width. We still
                # prefer the storage-aware packer when provided (testable).
                if vec_to_storage_str_fn is not None:
                    emb_str = vec_to_storage_str_fn(emb, 768)
                else:
                    emb_str = vec_to_str_fn(emb)
                _update_chunk_embedding(row["id"], emb_str, target_model, db_execute)
            reembedded += len(batch)
            batches += 1
            logger.info(
                "Batch %d re-embedded: %d chunks (total so far: %d)",
                batches,
                len(batch),
                reembedded,
            )
        except Exception as e:
            logger.error("re-embed batch failed (%d chunks): %s", len(batch), e)
            errors.append(f"batch {batches + 1}: {e}")
            # Don't break — next batch may succeed.

        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)

    elapsed_ms = round((time.time() - t0) * 1000, 2)
    summary = {
        "batches": batches,
        "reembedded": reembedded if not dry_run else 0,
        "skipped": skipped,
        "dry_run": dry_run,
        "errors": errors,
        "source_model": source_model,
        "target_model": target_model,
        "total_elapsed_ms": elapsed_ms,
    }
    logger.info("ETL summary: %s", summary)
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point — `python -m server.src.etl.etl_reembed`."""
    parser = argparse.ArgumentParser(
        description="Re-embed Cortex chunks from mpnet to nomic-v1.5 (v6.5.0 direction #3)",
    )
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--source-model", default=DEFAULT_SOURCE_MODEL)
    parser.add_argument("--target-model", default=DEFAULT_TARGET_MODEL)
    parser.add_argument("--sleep-ms", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--target-ratio",
        type=float,
        default=None,
        help="A/B rollout cap (0.0-1.0). Stop after this fraction of chunks "
             "is on target_model. Example: 0.10 for 10%%.",
    )
    args = parser.parse_args(argv)

    # Lazy imports so `--help` works without psycopg2/onnxruntime installed.
    from server.src.api_pg import query, execute, embed_text, vec_to_str, vec_to_storage_str

    def total_count_provider() -> int:
        return query(
            "SELECT COUNT(*) AS c FROM memory_chunks WHERE archived = 0",
            fetch="one",
        )["c"]

    summary = reembed_batch(
        db_query=query,
        db_execute=execute,
        embed_fn=embed_text,
        vec_to_str_fn=vec_to_str,
        vec_to_storage_str_fn=vec_to_storage_str,
        batch_size=args.batch_size,
        source_model=args.source_model,
        target_model=args.target_model,
        max_batches=args.max_batches,
        sleep_ms=args.sleep_ms,
        dry_run=args.dry_run,
        target_ratio=args.target_ratio,
        total_count_provider=total_count_provider,
    )
    # Exit non-zero if any errors occurred
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
