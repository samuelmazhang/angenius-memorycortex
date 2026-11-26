"""LOCOMO answer-layer eval for Cortex 6.7.0 (cross-system comparison).

真正的答案层口径（区别于retrieval-only）:
  retrieve (Cortex /search) -> reader (LLM 生成短答案) -> score
    * token-F1 / EM on cats 1-4 (1540 可回答题)  == 论文 LOCOMO 口径
    * LLM-judge accuracy 0/0.5/1 (Mem0 口径)
    * cat5 (adversarial, 444 空答案) correct-rejection rate

LLM 通过本地 free-model-relay (127.0.0.1:8787/v1, free-auto 别名, 自动故障转移)
调用, 纯标准库 urllib, 无 httpx/openai 依赖。

Usage:
  python _locomo_answer_eval.py --sample 40 --out .sprint/ans_sample.json
  python _locomo_answer_eval.py --full --out .sprint/ans_full.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DATA = r".data\locomo\data\locomo10.json"
CORTEX_URL = "http://127.0.0.1:1880"
RELAY_URL = os.getenv("LOCOMO_RELAY_URL", "http://127.0.0.1:8787/v1")
RELAY_MODEL = os.getenv("LOCOMO_RELAY_MODEL", "free-auto")
TOP_K = 5
WORKERS = 8
UNANSWERABLE = "unanswerable"

_WORD = re.compile(r"\w+", re.UNICODE)


# ---------------------------------------------------------------- LLM call
def llm_chat(messages: List[Dict[str, str]], *, max_tokens: int = 128,
             temperature: float = 0.0, retries: int = 3) -> Optional[str]:
    body = {"model": RELAY_MODEL, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    req = urllib.request.Request(
        f"{RELAY_URL}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read())
            return ((d.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        except urllib.error.HTTPError as e:
            if attempt == retries - 1:
                return None
            time.sleep(1.5 * (attempt + 1))
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(1.0 * (attempt + 1))
    return None


# ---------------------------------------------------------------- retrieve
def retrieve(base_url: str, question: str, k: int = TOP_K,
             tags: Optional[List[str]] = None) -> List[str]:
    body = {"query": question, "limit": k, "hybrid": True,
            "min_importance": 0, "include_metadata": True}
    if tags:
        body["tags"] = tags
    req = urllib.request.Request(
        f"{base_url}/search", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    out: List[str] = []
    for h in payload.get("results", []):
        t = str(h.get("grounded_context") or h.get("content") or h.get("text") or "").strip()
        if t:
            out.append(t)
    return out[:k]


# ---------------------------------------------------------------- prompts
READER_SYS = (
    "You answer questions using ONLY the provided memory excerpts. "
    "Give a short, direct answer (a few words, no explanation). "
    'If the answer cannot be determined from the excerpts, reply exactly "Unanswerable".'
)
READER_TMPL = "Memories:\n{context}\n\nQuestion: {question}\nAnswer:"

JUDGE_SYS = "You are a strict QA grader. Always return JSON only."
JUDGE_TMPL = (
    "Compare the candidate answer to the gold answer.\n"
    "Score 1.0 if correct (semantically equivalent to gold), 0.5 if partially correct, "
    '0.0 if wrong. If gold is "Unanswerable", score 1.0 only if candidate also says '
    '"Unanswerable" (or clearly states it cannot be determined).\n'
    'Return JSON: {{"score": <0.0|0.5|1.0>, "rationale": "<short reason>"}}\n\n'
    "Question: {question}\nGold answer: {gold}\nCandidate answer: {candidate}\n"
)


def reader_answer(question: str, contexts: List[str]) -> str:
    ctx = "\n".join(f"- {c}" for c in contexts[:TOP_K]) or "(no memories)"
    text = llm_chat([
        {"role": "system", "content": READER_SYS},
        {"role": "user", "content": READER_TMPL.format(context=ctx, question=question)},
    ], max_tokens=64)
    return (text or "").strip()


def judge_score(question: str, gold: str, candidate: str) -> Optional[float]:
    text = llm_chat([
        {"role": "system", "content": JUDGE_SYS},
        {"role": "user", "content": JUDGE_TMPL.format(
            question=question, gold=gold, candidate=candidate)},
    ], max_tokens=96)
    if not text:
        return None
    try:
        m = re.search(r"\{.*\}", text, re.S)
        d = json.loads(m.group(0) if m else text)
        s = float(d.get("score"))
        return max(0.0, min(1.0, s))
    except Exception:
        return None


# ---------------------------------------------------------------- metrics
def tokens(s: str) -> List[str]:
    return [t.lower() for t in _WORD.findall(s or "")]


def f1(pred: str, gold: str) -> float:
    p, g = tokens(pred), tokens(gold)
    if not p or not g:
        return 0.0
    common = Counter(p) & Counter(g)
    n = sum(common.values())
    if not n:
        return 0.0
    pr, rc = n / len(p), n / len(g)
    return 2 * pr * rc / (pr + rc)


def em(pred: str, gold: str) -> float:
    return 1.0 if " ".join(tokens(pred)) == " ".join(tokens(gold)) else 0.0


def one_case(q: Dict[str, Any], base_url: str,
             tags: Optional[List[str]] = None) -> Dict[str, Any]:
    question = q["question"]
    gold_raw = str(q.get("answer", "")).strip()
    cat = str(q.get("category", ""))
    is_unans = (gold_raw == "")
    gold = UNANSWERABLE if is_unans else gold_raw
    rec: Dict[str, Any] = {
        "qa_id": q.get("id") or q.get("qa_id", ""),
        "category": cat, "question": question, "gold": gold,
        "is_unanswerable": is_unans,
    }
    try:
        ctx = retrieve(base_url, question, tags=tags)
    except Exception as e:
        rec.update(error=f"retrieve: {e}", candidate="", f1=0.0, em=0.0, judge=None)
        return rec
    cand = reader_answer(question, ctx)
    rec["candidate"] = cand
    # cat5: correct rejection if reader says unanswerable
    rec["rejected"] = UNANSWERABLE in tokens(cand) or "cannot" in cand.lower() or "not enough" in cand.lower()
    if is_unans:
        rec["f1"] = 1.0 if rec["rejected"] else 0.0
        rec["em"] = rec["f1"]
    else:
        rec["f1"] = f1(cand, gold_raw)
        rec["em"] = em(cand, gold_raw)
    rec["judge"] = judge_score(question, gold, cand)
    return rec


def aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    ans = [r for r in records if not r.get("is_unanswerable") and not r.get("error")]
    unans = [r for r in records if r.get("is_unanswerable") and not r.get("error")]
    def avg(xs, key):
        vals = [r[key] for r in xs if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0.0
    judged = [r["judge"] for r in records if r.get("judge") is not None]
    by_cat: Dict[str, Dict[str, Any]] = {}
    for r in records:
        c = by_cat.setdefault(r["category"], {"n": 0, "f1": 0.0, "judge": []})
        c["n"] += 1
        c["f1"] += (r.get("f1") or 0.0)
        if r.get("judge") is not None:
            c["judge"].append(r["judge"])
    return {
        "n_total": len(records),
        "n_errors": sum(1 for r in records if r.get("error")),
        "answerable_14": {
            "n": len(ans),
            "f1": avg(ans, "f1"),
            "em": avg(ans, "em"),
            "judge": avg(ans, "judge"),
            "judged_n": sum(1 for r in ans if r.get("judge") is not None),
        },
        "all_nonempty": {
            "n": len(ans), "f1": avg(ans, "f1"), "em": avg(ans, "em"),
        },
        "unanswerable_cat5": {
            "n": len(unans),
            "correct_rejection": avg(unans, "f1"),
        },
        "judge_all": (sum(judged) / len(judged)) if judged else 0.0,
        "judge_total_judged": len(judged),
        "per_category": {
            c: {"n": d["n"], "f1": d["f1"] / d["n"],
                "judge": (sum(d["judge"]) / len(d["judge"])) if d["judge"] else 0.0}
            for c, d in sorted(by_cat.items())
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--base-url", default=CORTEX_URL)
    p.add_argument("--sample", type=int, default=0, help="0 = full 1986")
    p.add_argument("--sample-id", default=None,
                   help="only evaluate this sample_id (e.g. conv-26)")
    p.add_argument("--workers", type=int, default=WORKERS)
    p.add_argument("--out", default=".sprint/answer_eval.json")
    p.add_argument("--raw", default="", help="optional per-case jsonl dump")
    p.add_argument("--tags", nargs="*", default=None,
                   help="restrict /search to these tags (e.g. locomo-v2)")
    args = p.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    if args.sample_id:
        data = [s for s in data if s.get("sample_id") == args.sample_id]
        print(f"Restricted to sample_id={args.sample_id}: {len(data)} samples",
              file=sys.stderr)
    queries = []
    for s in data:
        for q in s.get("qa", []):
            queries.append(q)
    if args.sample > 0:
        # balanced sample across categories, deterministic
        by_cat: Dict[str, List[Any]] = {}
        for q in queries:
            by_cat.setdefault(str(q.get("category", "")), []).append(q)
        picked = []
        per = max(1, args.sample // len(by_cat))
        for c in sorted(by_cat):
            picked.extend(by_cat[c][:per])
        queries = picked[:args.sample]
    print(f"LOCOMO answer eval: {len(queries)} queries, {args.workers} workers, "
          f"relay={RELAY_URL} model={RELAY_MODEL} tags={args.tags}", file=sys.stderr)

    records: List[Dict[str, Any]] = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one_case, q, args.base_url, args.tags): i for i, q in enumerate(queries)}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                records.append(fut.result())
            except Exception as e:
                records.append({"error": f"worker: {e}", "category": "?", "f1": 0.0,
                                "em": 0.0, "judge": None, "is_unanswerable": False})
            if n % 50 == 0 or n == len(queries):
                el = time.perf_counter() - t0
                print(f"  {n}/{len(queries)}  elapsed={el:.0f}s  "
                      f"rate={n / el:.2f}/s", file=sys.stderr)
            if n % 200 == 0 and args.raw:
                # checkpoint: 防止长跑中断丢数据
                ckpt = str(args.raw) + ".ckpt"
                with open(ckpt, "w", encoding="utf-8") as cf:
                    for r in records:
                        cf.write(json.dumps(r, ensure_ascii=False) + "\n")
                print(f"  checkpoint -> {ckpt} ({len(records)} rows)", file=sys.stderr)

    summary = aggregate(records)
    summary["meta"] = {
        "version": "6.7.0", "relay_model": RELAY_MODEL, "top_k": TOP_K,
        "tags": args.tags,
        "workers": args.workers, "sample": args.sample,
        "elapsed_sec": time.perf_counter() - t0,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": "retrieve(/search) -> reader(LLM short answer) -> token-F1/EM + LLM-judge(0/0.5/1)",
    }
    out = {"summary": summary, "records": records}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.raw:
        with open(args.raw, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
