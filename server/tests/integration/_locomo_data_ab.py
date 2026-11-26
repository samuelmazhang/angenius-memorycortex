"""A/B: does data-content enrichment (absolute dates on turns) fix answer-layer?

Compare, on the SAME reader/judge model (hy4-flash-free via relay), two
contexts built from the SAME source data:

  OLD  = current ingested chunks (/search top-5, observation-first, "recently")
  NEW  = evidence-grounded chunks rebuilt from locomo10.json with the
         session absolute datetime attached to each dialogue turn/observation,
         ranked by lexical relevance to the question.

This isolates the DATA-CONTENT variable (user hypothesis) from the model
variable (already shown: stronger models are rate-limited on batch).
"""
from __future__ import annotations

import json, re, sys, time, importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

spec = importlib.util.spec_from_file_location("ans", "_locomo_answer_eval.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

DATA = r".data\locomo\data\locomo10.json"
SAMPLE = "conv-26"


def build_enriched_index(sample):
    """dia_id -> text with ABSOLUTE session date prepended."""
    conv = sample["conversation"]
    idx = {}
    for k, v in conv.items():
        if not (isinstance(k, str) and k.startswith("session_") and isinstance(v, list)):
            continue
        sess = k.split("_")[1]
        dt = conv.get(f"session_{sess}_date_time", "")
        for t in v:
            if not isinstance(t, dict):
                continue
            dia = t.get("dia_id")
            if not dia:
                continue
            txt = str(t.get("text", "")).strip()
            if not txt:
                continue
            idx[dia] = f"[{dt}] {t.get('speaker','')}: {txt}"
    # observation chunks with absolute date too
    for ok, ov in sample.get("observation", {}).items():
        mm = re.search(r"session_(\d+)", ok)
        if not mm:
            continue
        dt = conv.get(f"session_{mm.group(1)}_date_time", "")
        for speaker, items in ov.items():
            for item in items:
                if isinstance(item, list) and len(item) >= 2 and isinstance(item[1], str):
                    dia = item[1]
                    txt = str(item[0]).strip()
                    if txt and dia not in idx:
                        idx[dia] = f"[{dt}] {speaker}: {txt} (observation: {txt})"
    return idx


def lexical_rank(question, idx, k=5):
    """Simple word-overlap rank over enriched chunks (no LLM, deterministic)."""
    qw = set(m.tokens(question))
    scored = []
    for dia, txt in idx.items():
        tw = set(m.tokens(txt))
        sc = len(qw & tw) / (len(qw) + 1e-9)
        scored.append((sc, dia, txt))
    scored.sort(key=lambda x: -x[0])
    return [t for sc, d, t in scored[:k] if sc > 0]


def one(q, idx, base_url):
    question = q["question"]
    gold_raw = str(q.get("answer", "")).strip()
    is_unans = (gold_raw == "")
    gold = m.UNANSWERABLE if is_unans else gold_raw
    # OLD context from production /search
    try:
        old_ctx = m.retrieve(base_url, question)
    except Exception as e:
        return {"error": str(e)}
    cand_old = m.reader_answer(question, old_ctx)
    # NEW context from enriched local index
    new_ctx = lexical_rank(question, idx)
    cand_new = m.reader_answer(question, new_ctx)
    def sc(c):
        if is_unans:
            return 1.0 if (m.UNANSWERABLE in m.tokens(c) or "cannot" in c.lower()) else 0.0
        return m.f1(c, gold_raw)
    return {
        "question": question[:100], "gold": gold, "cat": str(q.get("category")),
        "old_ctx": old_ctx[:3], "new_ctx": new_ctx[:3],
        "cand_old": cand_old, "cand_new": cand_new,
        "f1_old": sc(cand_old), "f1_new": sc(cand_new),
        "judge_old": m.judge_score(question, gold, cand_old),
        "judge_new": m.judge_score(question, gold, cand_new),
    }


def main():
    data = json.loads(Path(DATA).read_text(encoding="utf-8"))
    sample = [s for s in data if s["sample_id"] == SAMPLE][0]
    idx = build_enriched_index(sample)
    # temporal + multi-hop + single-hop answerable questions
    qs = [q for q in sample["qa"] if str(q.get("answer", "")).strip()]
    qs = [q for q in qs if str(q.get("category")) in ("2", "3", "4")][:60]
    print(f"A/B on {SAMPLE}: {len(qs)} answerable QA, idx={len(idx)} chunks, "
          f"reader/judge={m.RELAY_MODEL}", file=sys.stderr)
    recs = []
    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=m.WORKERS) as ex:
        futs = [ex.submit(one, q, idx, m.CORTEX_URL) for q in qs]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                recs.append(f.result())
            except Exception as e:
                recs.append({"error": str(e)})
            if i % 20 == 0:
                print(f"  {i}/{len(qs)} {time.perf_counter()-t:.0f}s", file=sys.stderr)
    ok = [r for r in recs if not r.get("error")]
    def avg(k):
        v = [r[k] for r in ok if r.get(k) is not None]
        return sum(v)/len(v) if v else 0.0
    print(f"\nN={len(ok)} errors={len(recs)-len(ok)}")
    print(f"F1   old={avg('f1_old'):.4f}  new={avg('f1_new'):.4f}  delta={avg('f1_new')-avg('f1_old'):+.4f}")
    print(f"Judge old={avg('judge_old'):.4f}  new={avg('judge_new'):.4f}  delta={avg('judge_new')-avg('judge_old'):+.4f}")
    print("\n=== sample improvements ===")
    for r in ok:
        if r["f1_new"] > r["f1_old"] + 0.3:
            print(f"\nQ: {r['question']}")
            print(f"  gold={r['gold']!r}")
            print(f"  OLD cand={r['cand_old']!r} f1={r['f1_old']:.2f}")
            print(f"  NEW cand={r['cand_new']!r} f1={r['f1_new']:.2f}")
            print(f"  NEW ctx={r['new_ctx'][:2]}")
    out = Path(r"..sprint\data_ab_conv26.json")
    out.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
