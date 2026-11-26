# Cortex v6.5.0 LOCOMO-1986 Memory Quality Report (n=1986 queries)

Two numbers answer the two halves of the design goal:

  * **recall@K** = did we find the relevant chunk?
    (i.e. 'do not miss necessary memory')

  * **precision@K** = is the top-K clean of noise?
    (i.e. 'do not return irrelevant content')


## Strategy summary

| Strategy | recall@1 | recall@3 | recall@5 | recall@10 | precision@1 | precision@3 | precision@5 | precision@10 | MRR | p50 ms
|---|---|---|---|---|---|---|---|---|---|---|
| **A. v6.5.0 (Nomic + FTS5 hybrid)** | 31.2% | 48.6% | 56.7% | 56.7% | 31.2% | 20.3% | 15.4% | 7.7% | 0.405 | 136 |
| C. FTS5 only (PG direct)         | 0.0% | 0.1% | 0.1% | 0.1% | 0.0% | 0.0% | 0.0% | 0.0% | 0.001 | 32 |

## Lift (v6.5.0 vector advantage over FTS5-only)

- **recall@5 lift**: 56.6% (0.1% -> 56.7%)
- **recall@10 lift**: 56.6%
- **precision@5 lift**: 15.4% (0.0% -> 15.4%)
- **precision@10 lift**: 7.7%
- **MRR lift**: +0.405

## Per-category breakdown

LOCOMO categories: 1-hop, 2-hop, temporal, open-domain, adversarial.

| Cat | n | A R@5 | A P@5 | C R@5 | C P@5 | A MRR | C MRR
|---|---|---|---|---|---|---|---|
| 1 | 282 | 58.5% | 19.2% | 0.0% | 0.0% | 0.388 | 0.000
| 2 | 321 | 67.3% | 17.8% | 0.0% | 0.0% | 0.517 | 0.000
| 3 | 96 | 36.5% | 10.2% | 2.1% | 0.6% | 0.228 | 0.010
| 4 | 841 | 60.6% | 15.9% | 0.0% | 0.0% | 0.436 | 0.000
| 5 | 446 | 44.8% | 11.7% | 0.0% | 0.0% | 0.316 | 0.000

## Reading the table

- **recall@K**: fraction of queries where ANY of the top-K hits' `metadata.dia_id` is in the QA's evidence list.
  Design-goal mapping: 'do not miss necessary memory'.

- **precision@K**: fraction of top-K hits whose `metadata.dia_id` is in the evidence list.
  Design-goal mapping: 'do not return irrelevant content'.

- **MRR**: mean reciprocal rank of the first correct hit.

- **Latency**: client-measured round trip (A: HTTP, C: in-process psycopg2).
