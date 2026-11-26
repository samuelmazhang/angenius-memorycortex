"""A/B: does structured event date (grounded context) improve answer layer?

Same questions, same retrieved chunks, same reader/judge model. The ONLY
variable is whether the reader sees the resolved absolute event date from
memory_chunks.metadata.primary_event_date (grounded) vs raw content (plain).

  plain    : reader sees chunk content only ("8 May ... yesterday")
  grounded : reader sees content + "[Known event date: 2023-05-07]"

Usage:
  python _locomo_grounded_ab.py --n 100
"""
from __future__ import annotations
import argparse, json, re, time, urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://127.0.0.1:1880"
RELAY = "http://127.0.0.1:8787/v1"
DATA = r".data\locomo\data\locomo10.json"
WORD = re.compile(r"\w+", re.UNICODE)
UNANS = "unanswerable"


def llm(messages, max_tokens):
    body = {"model": "free-auto", "messages": messages, "temperature": 0.0, "max_tokens": max_tokens}
    req = urllib.request.Request(RELAY + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    for a in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read())
            return ((d.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        except Exception:
            time.sleep(1.2 * (a + 1))
    return ""


def retrieve(q, k=5):
    body = {"query": q, "limit": k, "hybrid": True, "min_importance": 0, "include_metadata": True}
    req = urllib.request.Request(BASE + "/search", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        p = json.loads(r.read())
    return p.get("results", [])[:k]


def tokens(s): return [t.lower() for t in WORD.findall(s or "")]
def f1(p, g):
    p, g = tokens(p), tokens(g)
    if not p or not g: return 0.0
    n = sum((Counter(p) & Counter(g)).values())
    if not n: return 0.0
    pr, rc = n / len(p), n / len(g)
    return 2 * pr * rc / (pr + rc)
def em(p, g): return 1.0 if " ".join(tokens(p)) == " ".join(tokens(g)) else 0.0

SYS = "You answer questions using ONLY the provided memory excerpts. Give a short, direct answer (a few words, no explanation). If the answer cannot be determined, reply exactly \"Unanswerable\"."

def reader(q, chunks, grounded):
    lines = []
    for c in chunks:
        t = str(c.get("content") or "").strip()
        if not t: continue
        if grounded:
            md = c.get("metadata") or {}
            ped = md.get("primary_event_date") if isinstance(md, dict) else None
            dates = md.get("event_dates") if isinstance(md, dict) else None
            tag = f" [Known event date: {ped}]" if ped else (f" [Dates: {', '.join(dates)}]" if dates else "")
            lines.append(f"- {t}{tag}")
        else:
            lines.append(f"- {t}")
    ctx = "\n".join(lines) or "(no memories)"
    return llm([{"role":"system","content":SYS},
                {"role":"user","content":f"Memories:\n{ctx}\n\nQuestion: {q}\nAnswer:"}], 64)

JUDGE = "You are a strict QA grader. Return JSON only. Score 1.0 if candidate is semantically equivalent to gold, 0.5 if partial, 0.0 if wrong. If gold is Unanswerable, score 1.0 only if candidate also says Unanswerable. JSON: {\"score\": <0|0.5|1>, \"rationale\": \"...\"}"
def judge(q, gold, cand):
    t = llm([{"role":"system","content":"You are a strict QA grader. Return JSON only."},
             {"role":"user","content":f"{JUDGE}\n\nQuestion: {q}\nGold: {gold}\nCandidate: {cand}"}], 96)
    try:
        m = re.search(r"\{.*\}", t, re.S)
        return max(0.0, min(1.0, float(json.loads(m.group(0) if m else t).get("score"))))
    except Exception:
        return None


def run_case(case, grounded):
    q = case["question"]; gold = str(case.get("answer", "")).strip()
    cat = str(case.get("category", ""))
    is_un = (gold == "")
    try:
        ch = retrieve(q)
        cand = reader(q, ch, grounded)
    except Exception as e:
        return {"cat": cat, "is_un": is_un, "f1": 0.0, "em": 0.0, "judge": None, "err": str(e)}
    rej = UNANS in tokens(cand) or "cannot" in cand.lower() or "not enough" in cand.lower()
    if is_un:
        f = 1.0 if rej else 0.0
        j = judge(q, "Unanswerable", cand)
        return {"cat": cat, "is_un": is_un, "f1": f, "em": f, "judge": j, "cand": cand}
    return {"cat": cat, "is_un": is_un, "f1": f1(cand, gold), "em": em(cand, gold),
            "judge": judge(q, gold, cand), "cand": cand, "gold": gold}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    args = ap.parse_args()
    data = json.load(open(DATA, encoding="utf-8"))
    cases = [q for s in data for q in s.get("qa", [])]
    # prioritise temporal (cat 2) so the signal is visible, then fill others
    temporal = [c for c in cases if str(c.get("category")) == "2" and str(c.get("answer", "")).strip()]
    others = [c for c in cases if str(c.get("category")) != "2" and str(c.get("answer", "")).strip()]
    sample = (temporal + others)[: args.n]
    print(f"cases={len(sample)} (temporal={min(len(temporal),args.n)})", flush=True)

    results = {}
    for mode in ("plain", "grounded"):
        out = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(run_case, c, mode == "grounded"): i for i, c in enumerate(sample)}
            for fut in as_completed(futs):
                out.append(fut.result())
        results[mode] = out
        print(f"  {mode}: done {len(out)}", flush=True)

    def agg(rows, cats=None):
        rs = [r for r in rows if r.get("judge") is not None and (cats is None or r["cat"] in cats)]
        if not rs: return None
        return dict(n=len(rs), f1=round(sum(r["f1"] for r in rs)/len(rs), 4),
                    em=round(sum(r["em"] for r in rs)/len(rs), 4),
                    judge=round(sum(r["judge"] for r in rs)/len(rs), 4))
    for label, cats in [("temporal(cat2)", {"2"}), ("all-answerable", None)]:
        p = agg(results["plain"], cats); g = agg(results["grounded"], cats)
        print(f"\n[{label}]")
        print(f"  plain    : {p}")
        print(f"  grounded : {g}")
        if p and g:
            print(f"  delta    : F1 {g['f1']-p['f1']:+.4f}  EM {g['em']-p['em']:+.4f}  judge {g['judge']-p['judge']:+.4f}")
    outp = r"..sprint\grounded_ab.json"
    json.dump(results, open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nsaved {outp}")

if __name__ == "__main__":
    main()
