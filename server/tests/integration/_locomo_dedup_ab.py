"""Consumer-side A/B: dedup + aggregate-query recall expansion for LOCOMO cat1.

Isolated verification WITHOUT touching the production service or re-embedding:
  baseline     : current behaviour (top-5 grounded_context, no dedup)
  dedup        : near-duplicate folding on retrieved contexts
  dedup_expand : dedup + wider recall (limit 8) for aggregate/multi-value
                 questions + strip trailing date annotations for those queries

All modes share the same /search corpus, same reader/judge model, same prompts.
Only the *context list* handed to the reader differs.

Usage:
  python _locomo_dedup_ab.py --mode baseline --cats 1 --out .sprint/dedup_baseline.json
  python _locomo_dedup_ab.py --mode dedup --cats 1 --out .sprint/dedup_only.json
  python _locomo_dedup_ab.py --mode dedup_expand --cats 1 --out .sprint/dedup_expand.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _locomo_answer_eval import (  # noqa: E402
    DEFAULT_DATA, CORTEX_URL, RELAY_URL, RELAY_MODEL, TOP_K,
    UNANSWERABLE, llm_chat, reader_answer, judge_score, f1, em, tokens,
)

TAGS = ["locomo-v2"]

# ---------------- near-duplicate folding ----------------
_DT_PREFIX = re.compile(r"^\[[^\]]*\]\s*")
_SPEAKER = re.compile(r"^[A-Z][A-Za-z'’\- ]{0,30}:\s*")
_TAIL_DATE = re.compile(r"\s*\[(?:Known event date|Dates):[^\]]*\]\s*$")
_WORD = re.compile(r"\w+", re.UNICODE)


def normalize(text: str, *, strip_dates: bool = True) -> str:
    t = _DT_PREFIX.sub("", text.strip())
    t = _SPEAKER.sub("", t)
    if strip_dates:
        t = _TAIL_DATE.sub("", t)
    return t.strip().lower()


def token_set(text: str) -> set:
    return set(_WORD.findall(normalize(text)))


def dedup_contexts(ctxs: List[str], *, strip_dates: bool = False,
                   threshold: float = 0.72) -> List[str]:
    """Fold near-duplicate contexts by token Jaccard (text-only fallback)."""
    kept: List[str] = []
    kept_sets: List[set] = []
    for raw in ctxs:
        s = token_set(raw)
        if not s:
            continue
        best_i = None
        best_j = 0.0
        for i, ks in enumerate(kept_sets):
            if not ks:
                continue
            j = len(s & ks) / len(s | ks) if (s | ks) else 0.0
            if j >= threshold and j > best_j:
                best_j, best_i = j, i
        if best_i is None:
            kept.append(raw)
            kept_sets.append(s)
        else:
            if len(normalize(raw)) > len(normalize(kept[best_i])):
                kept[best_i] = raw
                kept_sets[best_i] = s
    return kept


def _display(hit: Dict[str, Any], *, strip_dates: bool = False) -> str:
    t = str(hit.get("grounded_context") or hit.get("content") or hit.get("text") or "").strip()
    if strip_dates:
        t = _TAIL_DATE.sub("", t)
    return t


def dedup_hits(hits: List[Dict[str, Any]], *, strip_dates: bool = False,
               drop_loose_summary: bool = True) -> List[str]:
    """Dedup retrieved hits using metadata.dia_id as the primary key.

    Rationale (measured on LOCOMO): the same dialogue turn is stored as both
    a 'turn' and an 'observation' chunk with the same dia_id. Without dia_id
    folding these near-duplicates consume 2-3 of the top-5 slots. summary
    chunks (dia_id=None) are session-level blurbs that are semantically close
    but carry no specific fact; when a specific dia_id exists for the topic,
    they are noise, so we drop them (unless nothing else remains).
    """
    # group by dia_id, keep the most informative variant per group
    by_dia: Dict[str, List[Dict[str, Any]]] = {}
    no_dia: List[Dict[str, Any]] = []
    for h in hits:
        m = h.get("metadata") or {}
        dia = m.get("dia_id")
        if dia is None or str(dia).strip() == "":
            no_dia.append(h)
        else:
            by_dia.setdefault(str(dia), []).append(h)

    chosen: List[str] = []
    # rank groups by their best score, preserve retrieval order
    seen_first: Dict[str, int] = {}
    for idx, h in enumerate(hits):
        m = h.get("metadata") or {}
        dia = m.get("dia_id")
        if dia is not None and str(dia).strip():
            seen_first.setdefault(str(dia), idx)
    for dia, first_idx in sorted(seen_first.items(), key=lambda x: x[1]):
        variants = by_dia[dia]
        # prefer observation (clean fact) > turn > summary; then longest
        def rank(hh):
            m = hh.get("metadata") or {}
            kind_priority = {"observation": 2, "turn": 1, "summary": 0}.get(
                str(m.get("kind", "")), 0)
            return (kind_priority, len(normalize(_display(hh, strip_dates=strip_dates))))
        best = max(variants, key=rank)
        chosen.append(_display(best, strip_dates=strip_dates))

    if drop_loose_summary and chosen:
        return chosen
    # fallback: append de-duplicated no-dia chunks if we have too few
    for h in no_dia:
        t = _display(h, strip_dates=strip_dates)
        if t and t not in chosen:
            chosen.append(t)
    return chosen


# ---------------- aggregate-query detection ----------------
_AGG_PAT = re.compile(
    r"\bhow many\b|\bwhat (?:books|foods?|recipes?|activities|events|"
    r"countries|places|music|games|writings?|scripts?|letters?|"
    r"desserts?|pets?|dogs?|hobbies|sports|languages|subjects)\b|"
    r"\bwhich (?:books|countries|events|activities|family member|mediums)\b|"
    r"\blist\b|\ball the\b|\bwhat (?:has|have|did|do)\b.*\b(?:done|read|"
    r"visited|recommended|written|participated|pursued|made)\b",
    re.IGNORECASE,
)


def is_aggregate(q: str) -> bool:
    return bool(_AGG_PAT.search(q or ""))


# ---------------- retrieve variants ----------------
def _fetch(base_url: str, question: str, limit: int) -> List[Dict[str, Any]]:
    body = {"query": question, "limit": limit, "hybrid": True,
            "min_importance": 0, "include_metadata": True, "tags": TAGS}
    req = urllib.request.Request(
        f"{base_url}/search", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=40) as r:
        payload = json.loads(r.read())
    return payload.get("results", [])


def retrieve_for_mode(base_url: str, question: str, mode: str) -> List[str]:
    if mode == "baseline":
        hits = _fetch(base_url, question, TOP_K)
        return [_display(h) for h in hits][:TOP_K]

    agg = is_aggregate(question)
    if mode == "dedup":
        # fold from a wider pool, drop loose session summaries
        hits = _fetch(base_url, question, 12)
        return dedup_hits(hits, strip_dates=False, drop_loose_summary=True)[:TOP_K]

    if mode == "dedup_expand":
        # aggregate questions: wider recall so scattered facts can be gathered;
        # strip date annotations (not needed) and fold by dia_id.
        pool = 14 if agg else 10
        hits = _fetch(base_url, question, pool)
        cap = 8 if agg else TOP_K
        return dedup_hits(hits, strip_dates=agg, drop_loose_summary=True)[:cap]

    raise ValueError(f"unknown mode {mode}")


def one_case(q: Dict[str, Any], base_url: str, mode: str) -> Dict[str, Any]:
    question = q["question"]
    gold_raw = str(q.get("answer", "")).strip()
    cat = str(q.get("category", ""))
    is_unans = (gold_raw == "")
    gold = UNANSWERABLE if is_unans else gold_raw
    rec: Dict[str, Any] = {
        "qa_id": q.get("id") or q.get("qa_id", ""),
        "category": cat, "question": question, "gold": gold,
        "is_unanswerable": is_unans, "mode": mode,
        "is_aggregate": (not is_unans) and is_aggregate(question),
    }
    try:
        ctx = retrieve_for_mode(base_url, question, mode)
    except Exception as e:
        rec.update(error=f"retrieve: {e}", candidate="", f1=0.0, em=0.0, judge=None)
        return rec
    rec["n_ctx"] = len(ctx)
    cand = reader_answer(question, ctx)
    rec["candidate"] = cand
    rec["rejected"] = (UNANSWERABLE in tokens(cand) or "cannot" in cand.lower()
                       or "not enough" in cand.lower())
    if is_unans:
        rec["f1"] = 1.0 if rec["rejected"] else 0.0
        rec["em"] = rec["f1"]
    else:
        rec["f1"] = f1(cand, gold_raw)
        rec["em"] = em(cand, gold_raw)
    rec["judge"] = judge_score(question, gold, cand)
    return rec


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    ans = [r for r in records if not r.get("is_unanswerable") and not r.get("error")]
    agg = [r for r in ans if r.get("is_aggregate")]
    def avg(xs, key):
        vals = [r[key] for r in xs if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0.0
    def block(xs):
        return {
            "n": len(xs),
            "judge": round(avg(xs, "judge"), 4),
            "f1": round(avg(xs, "f1"), 4),
            "em": round(avg(xs, "em"), 4),
            "reject": round(sum(1 for r in xs if r.get("rejected")) / len(xs), 4) if xs else 0.0,
            "avg_ctx": round(sum(r.get("n_ctx", 0) for r in xs) / len(xs), 2) if xs else 0.0,
        }
    return {
        "all_answerable": block(ans),
        "aggregate_subset": block(agg),
        "n_errors": sum(1 for r in records if r.get("error")),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["baseline", "dedup", "dedup_expand"], required=True)
    p.add_argument("--cats", nargs="*", default=["1"], help="categories to evaluate")
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--base-url", default=CORTEX_URL)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--out", default="")
    p.add_argument("--raw", default="")
    p.add_argument("--limit", type=int, default=0, help="debug: cap number of queries")
    args = p.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    queries = [q for s in data for q in s.get("qa", [])
               if str(q.get("category", "")) in set(args.cats)]
    if args.limit:
        queries = queries[:args.limit]
    print(f"mode={args.mode} cats={args.cats} n={len(queries)} workers={args.workers}",
          file=sys.stderr)

    records: List[Dict[str, Any]] = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one_case, q, args.base_url, args.mode): i
                for i, q in enumerate(queries)}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                records.append(fut.result())
            except Exception as e:
                records.append({"error": f"worker: {e}", "category": "?", "f1": 0.0,
                                "em": 0.0, "judge": None, "is_unanswerable": False,
                                "is_aggregate": False})
            if n % 50 == 0 or n == len(queries):
                el = time.perf_counter() - t0
                print(f"  {n}/{len(queries)}  {el:.0f}s  {n/el:.2f}/s", file=sys.stderr)

    summary = summarize(records)
    summary["meta"] = {
        "mode": args.mode, "cats": args.cats, "n": len(queries),
        "relay_model": RELAY_MODEL, "tags": TAGS,
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False), file=sys.stderr)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(
            {"summary": summary, "records": records}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    if args.raw:
        with open(args.raw, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
