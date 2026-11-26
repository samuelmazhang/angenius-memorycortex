"""Repair ETL: re-embed chunks whose stored vector is mpnet-padded but whose
``model_version`` label says ``nomic-v1.5``.

V6.6.0 fix (2026-10-30)
-----------------------
The corpus on 127.0.0.1 ended up with two incompatible vector spaces under
one label::

    true-nomic    2333 chunks   768 real dims
    mpnet-padded   250 chunks   384 real dims + 384 zeros
    both labelled model_version = 'nomic-v1.5'

Cause: the 2026-09-01 image rebuild left ``sentence_transformers``/``torch``
out of the baked venv, so ``get_nomic_model()`` always returned
"unavailable" and ``embed_text()`` silently fell back to mpnet — while the
caller still wrote ``model_version='nomic-v1.5'``.

Why the existing ``etl/etl_reembed.py`` cannot do this job
-----------------------------------------------------------
It selects chunks by ``WHERE model_version = %s``. Every affected chunk is
labelled ``nomic-v1.5``, so selecting ``source_model='mpnet'`` finds ZERO
rows — the label is exactly what is wrong. This script instead detects the
*shape* of the stored vector (count of non-zero components) and repairs the
rows whose label and shape disagree.

Detection
---------
A 768-dim nomic vector has ~768 non-zero components. An mpnet vector that
was zero-padded to 768 has ~384. The threshold is deliberately loose
(``< 500``) so a legitimate nomic vector that happens to contain a few
exact zeros is never touched.

Usage
-----
    python -m server.src.etl.etl_repair_mislabeled --dry-run
    python -m server.src.etl.etl_repair_mislabeled --batch-size 50
    python -m server.src.etl.etl_repair_mislabeled --target-model mpnet

``--target-model`` selects the space the repaired chunks land in. The
default re-embeds with the *actual* model that the runtime can load
(``resolve_embedding_model()``), so the vector and the label always agree.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import List, Optional

logger = logging.getLogger("cortex-repair")

# A nomic vector has ~768 non-zero components; an mpnet vector padded to the
# 768-wide column has ~384. Anything below this is treated as padded.
NONZERO_DIM_THRESHOLD = 500

# The mislabeled rows are labelled 'nomic-v1.5' but hold mpnet vectors.
MISLABELED_LABEL = "nomic-v1.5"


def _find_mislabeled(db_query, limit: int) -> List[dict]:
    """Select chunks whose vector shape disagrees with their label.

    The check is done in SQL via a text cast of the pgvector value, because
    pgvector does not expose element-wise access in this Postgres version.
    """
    sql = """
        SELECT id, content, model_version, created_at
        FROM memory_chunks
        WHERE archived = 0
          AND embedding IS NOT NULL
          AND model_version = %s
          AND (
            SELECT count(*)
            FROM unnest(
              string_to_array(trim(both '[]' from embedding::text), ',')::text[]
            ) v
            WHERE v::float8 <> 0
          ) < %s
        ORDER BY created_at
        LIMIT %s
    """
    rows = db_query(sql, (MISLABELED_LABEL, NONZERO_DIM_THRESHOLD, limit),
                    fetch="all-no-commit")
    return list(rows)


def _repair_one(db_execute, chunk_id: str, embedding_str: str, new_model: str):
    """Rewrite the embedding and the label atomically."""
    db_execute(
        """UPDATE memory_chunks
              SET embedding = %s::vector, model_version = %s, updated_at = NOW()
            WHERE id = %s""",
        (embedding_str, new_model, chunk_id),
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--max-batches", type=int, default=0,
                    help="0 = no limit (process every mislabeled chunk)")
    ap.add_argument("--sleep-ms", type=int, default=200,
                    help="throttle between batches to avoid CPU contention")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be repaired, change nothing")
    ap.add_argument("--target-model", default=None,
                    help="target space; default = the model this runtime "
                         "can actually load (resolve_embedding_model())")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Import api_pg regardless of layout:
    #   * container  : /app/src/api_pg.py        -> "from src import api_pg"
    #   * repo checkout: server/src/api_pg.py    -> "from server.src import api_pg"
    # api_pg itself imports siblings as ``src.<pkg>``, so the directory that
    # CONTAINS the ``src`` package must be on sys.path.
    try:
        from src import api_pg  # type: ignore
    except ImportError:
        sys.path.insert(0, os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")))
        from server.src import api_pg  # type: ignore

    target_model = args.target_model or api_pg.resolve_embedding_model()
    logger.info("target model for repaired chunks: %s", target_model)

    if args.dry_run:
        rows = _find_mislabeled(api_pg.query, 20)
        logger.info("DRY RUN: %d mislabeled chunk(s) in the first 20 probed", len(rows))
        for r in rows[:20]:
            logger.info("  %s  label=%s  %s", r["id"], r["model_version"],
                        (r["content"] or "")[:60].replace("\n", " "))
        total = api_pg.query(
            """SELECT count(*) AS c FROM memory_chunks
                WHERE archived = 0 AND embedding IS NOT NULL
                  AND model_version = %s
                  AND (SELECT count(*) FROM unnest(
                         string_to_array(trim(both '[]' from embedding::text), ',')::text[]
                       ) v WHERE v::float8 <> 0) < %s""",
            (MISLABELED_LABEL, NONZERO_DIM_THRESHOLD), fetch="one")
        logger.info("DRY RUN: total mislabeled chunks = %s", total["c"])
        return 0

    t0 = time.time()
    repaired = 0
    batches = 0
    while True:
        if args.max_batches and batches >= args.max_batches:
            logger.info("stopping at max-batches=%d", args.max_batches)
            break
        rows = _find_mislabeled(api_pg.query, args.batch_size)
        if not rows:
            logger.info("no more mislabeled chunks")
            break
        batches += 1
        for r in rows:
            emb = api_pg.embed_text(r["content"], model_version=target_model)
            emb_str = api_pg._vec_to_str_for_model(emb, target_model)
            _repair_one(api_pg.execute, r["id"], emb_str, target_model)
            repaired += 1
        logger.info("batch %d: repaired %d chunk(s) (total %d)",
                    batches, len(rows), repaired)
        if args.sleep_ms:
            time.sleep(args.sleep_ms / 1000.0)

    logger.info("done: %d chunk(s) repaired in %.1fs", repaired, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
