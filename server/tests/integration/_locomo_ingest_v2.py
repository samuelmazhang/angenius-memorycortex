"""Ingest LOCOMO v2 chunks with absolute session timestamps (enriched data).

Root cause this fixes (confirmed by _locomo_data_ab.py A/B, 2026-11-25):
the old 2026-11-14 ingest stored observation rewrites like
  "Caroline attended an LGBTQ support group recently"
losing the relative-time anchor. Source turns say "yesterday" and each
session has an absolute datetime ("1:56 pm on 8 May, 2023"); combining
them yields the gold answer ("7 May 2023").

v2 ingest prepends the absolute session datetime to each turn chunk so the
reader can resolve relative time references. Injected under a NEW tag
``locomo-v2`` (and ``locomo-v2-sample-XX``), leaving the old ``locomo``
corpus untouched — evaluation selects ``tags=["locomo-v2"]``.

Usage:
  python _locomo_ingest_v2.py --samples conv-26 --base-url http://127.0.0.1:1880
  python _locomo_ingest_v2.py   # all 10 samples
"""
from __future__ import annotations

import argparse, json, sys, time, urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA = r".data\locomo\data\locomo10.json"
TAG = "locomo-v2"


def post_batch(url, items):
    body = {"chunks": items}
    req = urllib.request.Request(
        f"{url}/chunks/batch", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def make_chunk(text, sample_id, kind, dt, dia_id=None, speaker=None, session=None):
    text = (text or "").strip()
    if not text:
        return None
    # Prepend absolute datetime so relative references (yesterday/last week)
    # are resolvable by the reader.
    prefix = f"[{dt}] " if dt else ""
    content = prefix + (f"{speaker}: " if speaker else "") + text
    meta = {"sample_id": sample_id, "kind": kind, "v2": True}
    if dt:
        meta["session_datetime"] = dt
    if dia_id:
        meta["dia_id"] = dia_id
    if session is not None:
        meta["session"] = session
    if speaker:
        meta["speaker"] = speaker
    return {
        "content": content,
        "type": "knowledge",
        "source": f"locomo-v2-{sample_id}",
        "importance": 5,
        "tags": [TAG, f"{TAG}-{kind}", f"{TAG}-sample-{sample_id.split('-')[-1]}"],
        "metadata": meta,
    }


def build(sample):
    chunks = []
    sid = sample["sample_id"]
    conv = sample["conversation"]
    # dialogue turns with absolute datetime
    for k, v in conv.items():
        if not (isinstance(k, str) and k.startswith("session_") and isinstance(v, list)):
            continue
        parts = k.split("_")
        sess = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        dt = conv.get(f"session_{sess}_date_time", "") if sess is not None else ""
        for t in v:
            if not isinstance(t, dict):
                continue
            c = make_chunk(t.get("text", ""), sid, "turn", dt,
                           dia_id=t.get("dia_id"), speaker=t.get("speaker"), session=sess)
            if c:
                chunks.append(c)
    # observations with absolute datetime
    for ok, ov in (sample.get("observation") or {}).items():
        import re
        mm = re.search(r"session_(\d+)", ok)
        sess = int(mm.group(1)) if mm else None
        dt = conv.get(f"session_{sess}_date_time", "") if sess is not None else ""
        for speaker, items in ov.items():
            for item in items:
                if isinstance(item, list) and len(item) >= 2:
                    c = make_chunk(str(item[0]), sid, "observation", dt,
                                   dia_id=item[1] if isinstance(item[1], str) else None,
                                   speaker=speaker, session=sess)
                    if c:
                        chunks.append(c)
    # session summaries
    for sk, st in (sample.get("session_summary") or {}).items():
        import re
        mm = re.search(r"session_(\d+)", sk)
        sess = int(mm.group(1)) if mm else None
        dt = conv.get(f"session_{sess}_date_time", "") if sess is not None else ""
        c = make_chunk(str(st), sid, "summary", dt, session=sess)
        if c:
            chunks.append(c)
    return chunks


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=DATA)
    p.add_argument("--base-url", default="http://127.0.0.1:1880")
    p.add_argument("--samples", nargs="*", default=None,
                   help="sample_ids to ingest (default: all)")
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--tag", default=TAG)
    args = p.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    if args.samples:
        data = [s for s in data if s["sample_id"] in args.samples]
        print(f"Ingesting samples: {args.samples}", file=sys.stderr)
    else:
        print(f"Ingesting all {len(data)} samples", file=sys.stderr)

    all_chunks = []
    for s in data:
        all_chunks.extend(build(s))
    print(f"Built {len(all_chunks)} v2 chunks", file=sys.stderr)

    created = errors = 0
    t0 = time.perf_counter()
    for i in range(0, len(all_chunks), args.batch_size):
        batch = all_chunks[i:i+args.batch_size]
        try:
            resp = post_batch(args.base_url, batch)
            created += resp.get("created", 0)
            errors += len(resp.get("errors", []))
            print(f"  batch {i//args.batch_size+1}: +{resp.get('created',0)} "
                  f"({created} total, errs={len(resp.get('errors', []))})", file=sys.stderr)
        except Exception as e:
            errors += len(batch)
            print(f"  batch FAILED: {e}", file=sys.stderr)
    print(f"\nDone {time.perf_counter()-t0:.0f}s: created={created} errors={errors}",
          file=sys.stderr)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
