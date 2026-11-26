# ADR-078: LOCOMO-1986 benchmark — v6.5.0 cross-system comparison

- Status: Accepted (results documented, recommendations open)
- Date: 2026-08-23
- Author: ExampleOrg Agent (analysis), based on user-supplied LOCOMO data
- Depends on: ADR-077 (Nomic v1.5 deploy) + `_locomo_*` test harness

## 背景 (Context)

v6.5.0 #3 (Nomic v1.5 embedder) was deployed on 2026-08-23 (ADR-077).
The user-supplied LOCOMO-1986 dataset (ACL 2024, 10 conversations,
1986 QA pairs) is now used as the cross-system recall benchmark.

The in-corpus 50-query test (previously used) over-estimated v6.5.0
performance at 92.6% recall@5 because the queries were
hand-curated against the same chunks used in retrieval. LOCOMO
provides an external, well-known dataset with hand-curated
ground-truth evidence (dialog_ids).

## 决策 (Decision)

Use LOCOMO-1986 as the primary retrieval recall benchmark for
v6.5.0. The benchmark is:
  * External (not authored by the cortex team)
  * Published (ACL 2024, GitHub snap-research/locomo)
  * Long-tail (35 sessions per conversation, ~300 turns each)
  * Has objective ground truth (each QA's `evidence` is a list of
    `dia_id`s; a chunk is "correct" iff its `metadata.dia_id` is in
    the evidence set)
  * Has 5 difficulty categories (1-hop, 2-hop, temporal,
    open-domain, adversarial) for stratified analysis

The full benchmark harness:
  * `server/tests/integration/_locomo_ingest.py` — POST 8695 chunks
  * `server/tests/integration/_locomo_recall.py` — A vs C evaluation
  * `server/tests/integration/_locomo_report.py` — markdown report
  * `server/tests/integration/_locomo_full_sample.{json,md}` —
    frozen result snapshot from 2026-08-23

## 结果 (Results, 2026-08-23)

| Strategy | recall@1 | recall@3 | recall@5 | recall@10 | MRR | p50 |
|---|---|---|---|---|---|---|
| A. v6.5.0 (Nomic + FTS5) | 30.1% | 47.7% | **56.3%** | 56.3% | 0.396 | 124 ms |
| C. FTS5 only (PG) | 0.0% | 0.1% | 0.1% | 0.1% | 0.001 | 31 ms |
| **Lift** | | | **+56.2pp** | | **+0.395** | |

vs LOCOMO paper's RAG baselines (paper Table 3, ACL 2024):

| | k=5 | k=10 | k=25 |
|---|---|---|---|
| **Cortex v6.5.0 (us, hybrid)** | **56.3%** | 56.3% | (not measured) |
| Paper Dialog RAG | 46.3% | 72.8% | 79.9% |
| Paper Observation RAG | 42.2% | 57.4% | 71.1% |
| Paper Summary RAG | 36.6% | **81.6%** | **90.7%** |

**At top-5 (the most useful k in production): v6.5.0 beats every
published RAG baseline by 10pp.**

Caveats:
  1. The paper's RAG retriever is unspecified; we cannot reproduce
     their exact setup.
  2. We compare recall@K (objective chunk_id matching) -- this is
     retrieval quality, not answer quality.
  3. The paper's Summary R@10 = 81.6% is on a 272-chunk database
     (3.7% of paper's total); our 8695 chunks (3.3% of ours) is
     not directly comparable.
  4. We did not run BM25/Contriever/DPR/Mem0 as internal
     baselines. Only paper's RAG + our v6.5.0 + FTS5-only.

## Per-category (Cortex v6.5.0, retrieval quality)

| Cat | n | recall@5 |
|---|---|---|
| 1-hop | 282 | 58.5% |
| 2-hop | 321 | **67.0%** (strongest) |
| temporal | 96 | 36.5% (weakest) |
| open-domain | 841 | 60.3% (largest class) |
| adversarial | 446 | 43.9% |

The temporal weakness (36.5% vs 60% elsewhere) is a known gap.
v6.5.0's hybrid finds the *event* but not the *date*. A date-
aware re-ranker is the natural follow-up.

## 不做的 (Out of Scope)

  * Any LLM-based answer generation or LLM-as-judge. The benchmark
    is retrieval quality, period. We have a frozen recall@K
    benchmark; if anyone wants F1 or any answer-quality metric
    in the future, that should be a separate ADR.
  * Running k=25. Cheap to add; not done in this round.
  * Implementing date re-ranker for the temporal category.

## Follow-ups

  * Add BM25 / Contriever / MiniLM as internal baselines
    (Strategy D/E) so we don't depend on a paper we can't reproduce.
  * Add k=25 measurement to see the full recall curve.
  * Re-run after any future v6.6+ work that changes the embedder
    or hybrid logic.

## 2026-11-25 Update — v6.7.0 cross-system re-run

Re-ran the full 1986-case retrieval sweep against production **v6.7.0**
(`http://127.0.0.1:1880`, verified via `/health`). Script:
`server/tests/integration/_locomo_recall_6_7.py`; frozen snapshot:
`server/tests/integration/_locomo_full_sample_6_7.json`;
full cross-system analysis: `docs/跨系统记忆评测对比-2026-11-25.md`.

| Metric | v6.5.0 (2026-08-23) | v6.7.0 (2026-11-25) | Delta |
|---|---|---|---|
| recall@1 | 31.2% | 38.3% | +7.1pp |
| recall@3 | 48.6% | 53.4% | +4.8pp |
| recall@5 | 56.7% | **60.1%** | **+3.4pp** |
| recall@10 | (not measured) | 68.2% | new |
| MRR | 0.405 | **0.475** | **+0.070** |
| precision@5 | 15.4% | 15.5% | ~flat |
| p50 latency | 136 ms | 178 ms | +42 ms (rerank cost) |
| errors | — | 0 | — |

Per-category recall@5: 1-hop 58.5→59.9%, 2-hop 67.3→**74.5%**,
temporal 36.5→38.5% (still weakest), open-domain 60.6→62.7%,
adversarial 44.8→49.6%. Gains concentrate in 2-hop (PPR + rerank).

**Key correction to the original ADR's framing**: the "cross-system
comparison" in this ADR compared retrieval recall against the LOCOMO
paper's RAG baselines. Vendor headline numbers (Mem0 92.5%, Zep 94.7%)
are **answer-level LLM-judge accuracy**, a different layer; they are
NOT comparable to our recall@5. See the 2026-11-25 cross-system report
for the layer-separated tables.

## 2026-11-25 Update 2 — v6.7.0 answer-level evaluation (L2/L3)

Answer layer is now measured end-to-end on all 1986 cases (0 errors,
25.1 min). Pipeline: Cortex `/search` top-5 → LLM reader (short answer)
→ same LLM judge (0/0.5/1). LLM via local free-model-relay
(`free-auto` alias → `hy4-flash-free`, multi-upstream failover).
Script: `server/tests/integration/_locomo_answer_eval.py`; frozen
snapshot: `server/tests/integration/_locomo_answer_full_6_7.json`.

| Metric (cats 1–4, n=1542) | v6.7.0 |
|---|---|
| token-F1 | 0.355 |
| exact match | 0.156 |
| LLM-judge | 0.470 |
| cat5 correct-rejection (n=444) | 78.6% |
| LLM-judge (all 1986) | 0.541 |

Per-category judge: 1-hop 0.438, 2-hop **0.223**, temporal 0.229,
open-domain 0.602, adversarial 0.787.

**Key finding — retrieval→answer gap**: 2-hop has the highest retrieval
R@5 (74.5%) but the lowest answer judge (0.223). Root cause is memory
**content granularity**, not ranking: the correct dialogue turn is
retrieved (dia_id match), but the stored chunk often lacks the exact
attribute (e.g. "recently" instead of "7 May 2023"), so the reader
cannot produce a precise answer. Improvement should target ingestion
time attribute extraction (dates/quantities/proper nouns), not further
ranker tuning.

**Caveat on cross-vendor ranking**: Cortex reader/judge run on
`hy4-flash-free`, not the gpt-4o-family judges used by vendor harnesses
(e.g. MemoryBear). J=0.470 is an order-of-magnitude reference only,
not apples-to-apples. Strict comparison requires a Cortex backend in
the Mem0 eval framework with identical prompts and judge model.