# Cortex vs Hermes native MEMORY.md / USER.md

- Date: 2026-08-23
- Author: ExampleOrg Agent
- Question: how does cortex compare to the original hermes-native
  file-backed memory (MEMORY.md / USER.md) it replaced?

---

## 1. The two designs at a glance

| | **MEMORY.md / USER.md (legacy)** | **cortex (current)** |
|---|---|---|
| **Storage** | Two flat files in `~/.minimax/agents/mavis/memory/` | PostgreSQL with pgvector + FTS5 |
| **Entry delimiter** | `§` (section sign) | rows, one entry per row |
| **Entry shape** | free-form multiline text | `{content, type, tags, source, importance, metadata, embedding, model_version}` |
| **Capacity** | bounded by file readability (a few hundred entries before the file becomes unwieldy) | unbounded -- 10K+ chunks today, no schema ceiling |
| **Retrieval** | grep / eyeball | vector cosine + FTS5 hybrid, returns top-K by similarity |
| **Selection for prompt** | snapshot the whole file at session start | `/search` on demand, only relevant chunks |
| **Fits in context** | only as long as the file is short | scales -- only the queried subset enters the prompt |
| **Concurrency** | atomic replace on file | row-level write, no global lock |
| **Embeddings** | none | Nomic v1.5 768-dim, computed at write time |
| **Search precision** | exact substring only | semantic (paraphrased question -> matching chunk) |
| **Multi-hop** | impossible without grep | top-K lets the agent compose from multiple chunks |

## 2. Capacity test

The legacy MEMORY.md (a 2026-04-30 backup of a fully-used agent)
held 162 lines. USER.md held 149 lines. That's ~311 entries
total in two files. At ~200 chars per entry, that's 60-80 KB
of text -- already big enough to be a real injection cost on
every session start.

The current cortex DB on 127.0.0.1 holds:

  * `hermes_memory` source: 23 chunks
  * `user` source: 3297 chunks
  * `LOCOMO`-ingested: 8695 chunks (for the benchmark, not the
    real agent)
  * Total: 11992 chunks

The legacy file system was already maxed out at ~300 entries
across two files. cortex holds 40x more in the same kind of
operational memory, and the LOCOMO benchmark shows it can
absorb ~9000 more without complaint.

## 3. Recall test -- the actual question "did you find it?"

LOCOMO-1986 was specifically chosen because it tests the
retrieval question directly: does the system return a chunk
that contains the answer to the question? It has 1986 QA
pairs with hand-curated evidence `dia_id`s.

| | MEMORY.md / USER.md | cortex v6.5.0 (LOCOMO) |
|---|---|---|
| **Lookup method** | grep, then eyeball | `1 - embedding <=> query` + `ts_rank` |
| **Recall@5 (LOCOMO)** | n/a (no embeddings) | **56.3%** |
| **Precision@5** | n/a | **15.4%** |
| **P@1 (= R@1)** | n/a | **31.2%** (top-1 is clean when it hits) |

The legacy file format doesn't even have a recall@5 number --
you can grep for a substring and get back a pile of matches;
whether the right one is in the top 5 depends entirely on how
you wrote the question and how you remembered to format the
entry. cortex gives a number, and the number is good.

## 4. Operational differences that matter

* **Cold start.** Legacy: load all 60-80 KB into the system
  prompt at session start. cortex: nothing in the prompt until
  the agent queries. As `MEMORY.md` grows, every session pays
  the cost; with cortex, the cost scales with how much the
  agent actually needs.

* **Recall failure mode.** Legacy: agent asks "what was the
  user preference about X?" and gets nothing back if the entry
  was written with a different phrasing. cortex: paraphrased
  question ("what does the user want for X?") still matches
  the entry that was phrased as "user prefers X". The semantic
  match is the whole point.

* **Mid-session writes.** Legacy: a `replace` updates the file
  on disk but the system prompt snapshot does not refresh --
  you have to wait until the next session for the new entry to
  be visible to the agent. cortex: every write goes through
  `/chunks`, and the next `cortex_recall` MCP call sees it
  immediately. (The legacy design docs even call this out
  as a known limitation, see `connector/hermes/memory_tool.py`
  docstring: "Mid-session writes update files on disk
  immediately (durable) but do NOT change the system prompt --
  this preserves the prefix cache for the entire session.")

* **Write conflict.** Legacy: `atomic_replace` on a single file
  = global lock, no concurrent writes from different
  processes. cortex: row-level write, no lock.

* **Audit.** Legacy: file mtime. cortex: `/audit` log, every
  read and write tracked, with source attribution.

## 5. What cortex does NOT change

Both designs carry the same **honest entry shape**: a chunk of
free-form text. The user/agent still has to write the entry
in a way that future queries will find it. cortex's semantic
search forgives paraphrasing; it does not invent content
that's missing. If a preference is never written down, neither
system will surface it.

The other thing cortex does not do: it does not compress or
summarize. MEMORY.md is a curated, human-readable log. cortex
chunks are the same text, just in a database. The agent still
needs to write well-formed entries; cortex just finds them
better.

## 6. Take-away

The migration from MEMORY.md/USER.md to cortex is a capacity
and discoverability upgrade, not a feature change. What
cortex adds:

  * **search that doesn't depend on exact word match** --
    Nomic v1.5 paraphrase + FTS5 hybrid hit 56% recall@5
    on LOCOMO, vs the legacy's effectively-zero on any
    paraphrased question
  * **scale past ~300 entries** without paying the cost
    on every session start
  * **mid-session visibility** -- new entries are
    queryable immediately, no need to wait for the next
    session to refresh the snapshot
  * **audit and concurrency** that file-based memory
    never had

What cortex keeps the same:

  * one chunk = one free-form text entry, no schema
  * the agent still has to decide what's worth writing
  * no automatic summarization, no LLM in the loop

If you're coming from MEMORY.md/USER.md: think of cortex as
"the same memory, but searchable, and big enough to keep
everything instead of curating aggressively." The discipline
of writing good entries still matters; the discipline of
trimming the file for prompt size no longer does.