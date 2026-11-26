"""Ingest the LOCOMO 1986 QA benchmark into cortex.

Reads ``data/locomo/data/locomo10.json`` and POSTs every dialogue
turn + observation + session_summary as a chunk with metadata
carrying the original ``dia_id`` and ``sample_id`` so that recall
evaluators can match them back to QA evidence.

Usage:
    python _locomo_ingest.py [--limit-samples N] [--tag locomo] \\
        --base-url http://127.0.0.1:1880

Defaults to all 10 samples (~8700 chunks, ~7 minutes with the
Nomic embedder).

Tags: every chunk is tagged with ``locomo`` and the sample_id
(e.g. ``locomo-conv-26``) so the recall evaluator can filter.

Idempotency: chunks with duplicate content within the same sample
are skipped (we use ``source_key`` server-side; if the batch endpoint
rejects a duplicate it'll show up in the ``errors`` list).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


def _post_batch(url: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    body = {"chunks": items}
    req = urllib.request.Request(
        f"{url}/chunks/batch",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _make_chunk(
    text: str,
    *,
    sample_id: str,
    kind: str,
    dia_id: Optional[str] = None,
    session: Optional[int] = None,
    speaker: Optional[str] = None,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Build one chunk payload. Returns None if text is empty."""
    text = (text or "").strip()
    if not text:
        return None
    metadata = {
        "sample_id": sample_id,
        "kind": kind,  # turn | observation | summary
    }
    if dia_id:
        metadata["dia_id"] = dia_id
    if session is not None:
        metadata["session"] = session
    if speaker:
        metadata["speaker"] = speaker
    if extra_meta:
        metadata.update(extra_meta)
    return {
        "content": text,
        "type": "knowledge",
        "source": f"locomo-{sample_id}",
        "importance": 5,
        "tags": ["locomo", f"locomo-{kind}", f"locomo-sample-{sample_id.split('-')[-1]}"],
        "metadata": metadata,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--data",
        default=r".data\locomo\data\locomo10.json",
    )
    p.add_argument("--base-url", default="http://127.0.0.1:1880")
    p.add_argument("--limit-samples", type=int, default=None)
    p.add_argument("--batch-size", type=int, default=200,
                   help="Max chunks per POST /chunks/batch (cap 500)")
    p.add_argument("--tag-prefix", default="locomo")
    args = p.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    if args.limit_samples:
        data = data[:args.limit_samples]
    print(f"Ingesting {len(data)} samples from {args.data}", file=sys.stderr)

    # Build all chunks
    all_chunks: List[Dict[str, Any]] = []
    for s in data:
        sample_id = s["sample_id"]
        # 1) Dialogue turns
        for sess_key, sess_val in s.get("conversation", {}).items():
            if not sess_key.startswith("session_") or not isinstance(sess_val, list):
                continue
            try:
                sess_num = int(sess_key.split("_")[1])
            except (IndexError, ValueError):
                sess_num = None
            for turn in sess_val:
                c = _make_chunk(
                    turn.get("text", ""),
                    sample_id=sample_id,
                    kind="turn",
                    dia_id=turn.get("dia_id"),
                    session=sess_num,
                    speaker=turn.get("speaker"),
                )
                if c is not None:
                    all_chunks.append(c)
        # 2) Observations (RAG-ready factual statements)
        for obs_key, obs_val in s.get("observation", {}).items():
            try:
                sess_num = int(obs_key.split("_")[1])
            except (IndexError, ValueError):
                sess_num = None
            for speaker, items in obs_val.items():
                for item in items:
                    if not isinstance(item, list) or len(item) < 2:
                        continue
                    text, dia_id = item[0], item[1]
                    c = _make_chunk(
                        text,
                        sample_id=sample_id,
                        kind="observation",
                        dia_id=dia_id,
                        session=sess_num,
                        speaker=speaker,
                    )
                    if c is not None:
                        all_chunks.append(c)
        # 3) Session summaries
        for sum_key, sum_text in s.get("session_summary", {}).items():
            try:
                sess_num = int(sum_key.split("_")[1])
            except (IndexError, ValueError):
                sess_num = None
            c = _make_chunk(
                sum_text,
                sample_id=sample_id,
                kind="summary",
                session=sess_num,
            )
            if c is not None:
                all_chunks.append(c)
    print(f"Built {len(all_chunks)} chunks", file=sys.stderr)

    # POST in batches
    total_created = 0
    total_errors = 0
    n_batches = (len(all_chunks) + args.batch_size - 1) // args.batch_size
    t0 = time.perf_counter()
    for i in range(0, len(all_chunks), args.batch_size):
        batch = all_chunks[i:i+args.batch_size]
        b_idx = i // args.batch_size + 1
        try:
            resp = _post_batch(args.base_url, batch)
            total_created += resp.get("created", 0)
            total_errors += len(resp.get("errors", []))
        except Exception as e:
            total_errors += len(batch)
            print(f"  batch {b_idx}/{n_batches} FAILED: {e}", file=sys.stderr)
            continue
        elapsed = time.perf_counter() - t0
        rate = total_created / elapsed if elapsed > 0 else 0
        eta = (len(all_chunks) - (i + len(batch))) / rate if rate > 0 else 0
        print(
            f"  batch {b_idx}/{n_batches}: +{resp.get('created', 0)} "
            f"created ({total_created} total, {rate:.1f}/s, ETA {eta:.0f}s)",
            file=sys.stderr,
        )
    elapsed = time.perf_counter() - t0
    print(f"\nDone in {elapsed:.1f}s", file=sys.stderr)
    print(f"  Total created : {total_created}", file=sys.stderr)
    print(f"  Total errors  : {total_errors}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())