# Hermes Internal Patches — V6.3.1 (2026-08-18) + V6.5.2 (2026-09-17)

These are the **hermes-side code changes** that the connector depends on
but does NOT own. They live in the `hermes-agent` checkout
(`A:\Application\Hermes-Agent\Program\hermes-agent\`) and must be re-applied
on every fresh hermes install.

The README used to say "apply them by hand against the V5.3.7 release
commit". This file is the **first formal landing** — every diff below is
verified on the live `Program/hermes-agent/` checkout (HEAD 2026-08-14,
patched through 2026-09-17).

**Versions:**
- **V6.3.1** (2026-08-18): session_summaries + hooks — patches 1-6 below.
- **V6.5.2** (2026-09-17): cortex takeover fixes — patches 7-8 below.

## What is patched and why

| # | File | What | Why |
|---|------|------|-----|
| 1 | `hermes_state_common.py` | `session_summaries` table + 2 indexes | Local fast cache for cortex session summaries. UNIQUE(session_id, source) guarantees idempotent upserts. |
| 2 | `hermes_state.py` | SessionDB 3 methods | `upsert_session_summary`, `update_session_summary_cortex_id`, `list_session_summaries` — back the cortex ↔ local dual-write in `tools/session_summarizer.py`. |
| 3 | `hermes_state.py` | `_maybe_progressive_summary` helper | One COUNT query per `append_message`; only fires the async summarizer when assistant count crosses PROGRESSIVE_THRESHOLD (default 50). |
| 4 | `hermes_state.py` | `end_session` hook | Fires `summarize_session_async(self, session_id, source="end")` after the session-end UPDATE commits. Honors `HERMES_DISABLE_SESSION_SUMMARY=1`. |
| 5 | `hermes_state.py` | `append_message` hook | Tail-call to `_maybe_progressive_summary(session_id)` after the message row commits. |
| 6 | `agent/conversation_loop.py` | First-turn system prompt injection | Pulls the user's most recent N=5 session summaries via `get_prior_summaries` and appends them to `system_message` before the builder runs. Continuation path is untouched — prefix cache is broken ONLY on first turn of a new session, which already rebuilds anyway. |
| 7 | `agent/agent_init.py` | `_init_memory` builtin-memory init block | pre-bind `mem_config` as `{}` + replace missing `get_builtin_memory_config`/`get_builtin_memory_store_flags` with `load_config()` reads. **Without this: every session prints `Memory provider 'memory-cortex' is selected but reports unavailable` and runs with `_memory_manager = None`.** See `patches/04-agent_init-mem-config-fix.md`. |
| 8 | `tools/memory_tool.py` | `MemoryStore.add` cortex-branch predicate + `_add_to_cortex` char-limit gate | Inverted predicate `if _is_cortex_unavailable()` → `if use_cortex and not _is_cortex_unavailable()`, plus move the local-file char-limit parity check into `if not use_cortex:` so cortex gets a clean pass-through. **Without this: every `memory_tool(action='add')` falls into the .md file path even when `use_cortex=True`, and the local 2,200-char limit blocks writes to cortex.** Cortex server's `total_memories` was frozen at 2369 for 33 days (2026-08-04 → 2026-09-17) because of this bug. See `patches/05-memory_tool-cortex-takeover-fix.md`. |

Patches 7 and 8 are reproducible by `patches/04-agent_init-mem-config-fix.md` and
`patches/05-memory_tool-cortex-takeover-fix.md` + `scripts/apply_patches.py`.
See `README.md` "Deployment patches" for the end-to-end runbook.

## 1) `hermes_state_common.py` — `session_summaries` table

Insert this `CREATE TABLE` block right after `gateway_hygiene_state`
(line ~384 in the 979ca57a5 commit):

```sql
CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL DEFAULT '',
    summary_text TEXT NOT NULL,
    short TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'end',
    trigger TEXT NOT NULL DEFAULT '',
    cortex_chunk_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (session_id, source)
);
```

Add these two CREATE INDEX lines at the end of the SCHEMA_SQL block
(just before the closing `"""`, around line ~456 in 979ca57a5):

```sql
CREATE INDEX IF NOT EXISTS idx_session_summaries_session ON session_summaries(session_id);
CREATE INDEX IF NOT EXISTS idx_session_summaries_user_created
    ON session_summaries(user_id, created_at DESC);
```

Both are `IF NOT EXISTS` so re-running on an existing DB is a no-op.

## 2 + 3 + 4 + 5) `hermes_state.py` — 3 SessionDB methods + 2 hooks

### 2a) Insert immediately after `SessionDB.get_session` (ends ~line 7907)

```python
# ── Session summary storage (V5.3.7) ─────────────────────────────────────
# Three methods back tools/session_summarizer.py.  Idempotent upsert keyed
# on (session_id, source) so re-summarizing the same session replaces the
# prior row (and the cortex_chunk_id is overwritten, not duplicated).
# Safe to call before SCHEMA_SQL has run on a fresh DB — relies on the
# session_summaries CREATE TABLE that lives in hermes_state_common.py.

def upsert_session_summary(
    self,
    session_id: str,
    summary_text: str,
    short: str,
    parsed: Dict[str, Any],
    source: str = "end",
    trigger: str = "",
) -> Optional[int]:
    """Insert or replace a session summary row.

    Returns the row id, or ``None`` if the session doesn't exist
    (FK would reject; we pre-check and silently no-op so end-of-session
    callers don't have to handle the race).
    """
    if not session_id:
        return None
    now = time.time()
    # Resolve user_id via existing get_session; empty when no attribution.
    user_id = ""
    sess = self.get_session(session_id)
    if isinstance(sess, dict):
        user_id = str(sess.get("user_id") or "")
    parsed_json = json.dumps(parsed, ensure_ascii=False)

    def _do(conn):
        conn.execute(
            "INSERT INTO session_summaries "
            "(session_id, user_id, summary_text, short, parsed_json, "
            " source, trigger, cortex_chunk_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?) "
            "ON CONFLICT(session_id, source) DO UPDATE SET "
            " user_id = excluded.user_id, "
            " summary_text = excluded.summary_text, "
            " short = excluded.short, "
            " parsed_json = excluded.parsed_json, "
            " trigger = excluded.trigger, "
            " updated_at = excluded.updated_at",
            (session_id, user_id, summary_text, short, parsed_json,
             source, trigger, now, now),
        )
        return conn.execute(
            "SELECT id FROM session_summaries "
            "WHERE session_id = ? AND source = ?",
            (session_id, source),
        ).fetchone()[0]

    try:
        return self._execute_write(_do)
    except sqlite3.IntegrityError as exc:
        # FK failure means the session row vanished between lookup and
        # write — benign race. Callers should treat None as "no row".
        logger.debug(
            "[summary] upsert_session_summary FK race for %s: %s",
            session_id, exc,
        )
        return None

def update_session_summary_cortex_id(
    self, row_id: int, cortex_chunk_id: str
) -> None:
    """Backfill the cortex chunk id after a successful cortex push."""
    if not row_id or not cortex_chunk_id:
        return

    def _do(conn):
        conn.execute(
            "UPDATE session_summaries SET cortex_chunk_id = ?, "
            "updated_at = ? WHERE id = ?",
            (cortex_chunk_id, time.time(), row_id),
        )

    try:
        self._execute_write(_do)
    except sqlite3.DatabaseError as exc:
        logger.debug(
            "[summary] update_session_summary_cortex_id failed for id=%s: %s",
            row_id, exc,
        )

def list_session_summaries(
    self, user_id: str, limit: int = 5
) -> List[Dict[str, Any]]:
    """Return the most recent ``limit`` summaries for ``user_id``.

    Used at next-session boot to inject prior context into the system
    prompt. Empty list when ``user_id`` is falsy or no rows match.
    """
    if not user_id:
        return []
    with self._read_ctx() as conn:
        rows = conn.execute(
            "SELECT id, session_id, summary_text, short, "
            "       parsed_json, source, trigger, cortex_chunk_id, "
            "       created_at, updated_at "
            "FROM session_summaries "
            "WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["parsed"] = json.loads(d.pop("parsed_json") or "{}")
        except (TypeError, ValueError):
            d["parsed"] = {}
        out.append(d)
    return out
```

### 4) Patch `SessionDB.end_session` — fire async summarizer

Find `end_session` and add the hook AFTER `self._execute_write(_do)`:

```python
def end_session(self, session_id: str, end_reason: str) -> None:
    """Mark a session as ended. ...[unchanged docstring]..."""
    def _do(conn):
        conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (time.time(), end_reason, session_id),
        )
    self._execute_write(_do)

    # V5.3.7 — fire-and-forget session summarizer after end_session.
    # No-op when disabled by env var (HERMES_DISABLE_SESSION_SUMMARY=1)
    # or when the module can't import (e.g. LM Studio / connector missing).
    # The thread is daemon=True so it cannot block interpreter shutdown.
    if os.environ.get("HERMES_DISABLE_SESSION_SUMMARY") == "1":
        return
    try:
        from tools import session_summarizer  # tools/session_summarizer.py
    except Exception as exc:
        logger.debug("[summary] import failed at end_session: %s", exc)
        return
    try:
        session_summarizer.summarize_session_async(self, session_id, source="end")
    except Exception as exc:
        logger.debug("[summary] end_session hook failed: %s", exc)
```

### 3 + 5) Patch `SessionDB.append_message` — progressive hook + helper

Find `append_message`'s closing `return self._execute_write(...)` block
and replace it with this (preserves the same return semantics — calls
`_maybe_progressive_summary` between commit and return, swallows errors
inside the helper):

```python
# Transcript append is THE critical write: its failure aborts the
# user's turn (session_persistence_failed). Use the long patience so
# a sibling process legitimately holding the write lock for seconds
# (VACUUM, TRUNCATE checkpoint at close, an older pre-bounded-merge
# process's FTS optimize) can't destroy a healthy turn (#74478).
msg_id = self._execute_write(
    _do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S
)
# V5.3.7 — progressive summarizer hook (cheap; one COUNT per append).
# Fire-and-forget: any exception here is swallowed inside the helper.
self._maybe_progressive_summary(session_id)
return msg_id
```

Then insert the `_maybe_progressive_summary` helper between
`append_message` and `append_messages_batch`:

```python
# V5.3.7 — progressive summarizer hook. Fires after every append_message;
# only does work when the assistant-message count for this session has
# just crossed a multiple of PROGRESSIVE_THRESHOLD (default 50). Cheap
# path: one COUNT(*) read on a session-local index.
def _maybe_progressive_summary(self, session_id: str) -> None:
    if os.environ.get("HERMES_DISABLE_SESSION_SUMMARY") == "1":
        return
    try:
        threshold = int(os.environ.get(
            "SESSION_SUMMARY_PROGRESSIVE", "50"))
    except (TypeError, ValueError):
        threshold = 50
    if threshold <= 0:
        return
    try:
        with self._read_ctx() as conn:
            row = conn.execute(
                "SELECT "
                "  (SELECT COUNT(*) FROM messages "
                "     WHERE session_id = ? AND role = 'assistant' AND active = 1), "
                "  (SELECT user_id FROM sessions WHERE id = ?)",
                (session_id, session_id),
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        logger.debug("[summary] progressive COUNT failed: %s", exc)
        return
    if not row:
        return
    assistant_count, _user_id = row[0], row[1]
    if assistant_count <= 0 or assistant_count % threshold != 0:
        return
    try:
        from tools import session_summarizer
    except Exception as exc:
        logger.debug("[summary] progressive import failed: %s", exc)
        return
    try:
        session_summarizer.summarize_session_async(
            self, session_id, source=f"progressive:{assistant_count}"
        )
    except Exception as exc:
        logger.debug("[summary] progressive hook failed: %s", exc)
```

## 6) `agent/conversation_loop.py` — first-turn prior-summary injection

Find `_restore_or_build_system_prompt` (around line 909), locate the line
`agent._cached_system_prompt = agent._build_system_prompt(system_message)`
that's in the "first turn of a new session" branch, and insert this block
immediately BEFORE that line:

```python
    # First turn of a new session (or recovering from a broken stored
    # prompt) — build from scratch.
    # V5.3.7 — inject prior session summaries from session_summaries table
    # into the system_message so the agent starts with cross-session context.
    # Only fires on first turn (this branch is new-session only); continuation
    # reuses stored_prompt above and never reaches here. Fail-open: any
    # exception (DB read, missing module) just logs and continues.
    if agent._session_db:
        try:
            from tools import session_summarizer as _ss
            sess_row = agent._session_db.get_session(agent.session_id) or {}
            uid = str(sess_row.get("user_id") or "")
            if uid:
                priors = _ss.get_prior_summaries(agent._session_db, uid, limit=5)
                prior_block = _ss.format_prior_summaries_for_prompt(priors)
                if prior_block:
                    system_message = (
                        (system_message or "") + "\n\n" + prior_block
                    )
                    logger.info(
                        "[summary] injected %d prior summaries into "
                        "first-turn system prompt for session %s",
                        len(priors), agent.session_id,
                    )
        except Exception as exc:
            logger.debug(
                "[summary] prior-summary injection failed (non-fatal): %s", exc,
            )

    agent._cached_system_prompt = agent._build_system_prompt(system_message)
```

## Re-application order on a fresh hermes install

1. `git checkout` the hermes-agent tree (or fresh clone)
2. Run `pip install` / venv setup — `os`, `threading`, `json`, `sqlite3`,
   `time`, `logging` are all stdlib, no extra deps
3. Apply patch 1 (hermes_state_common.py) — table + indexes
4. Apply patches 2a/4/3+5 (hermes_state.py) — 3 methods + 2 hooks + 1 helper
5. Apply patch 6 (agent/conversation_loop.py) — first-turn injection
6. Sync the V6.3.1 connector:
   `HERMES_TOOLS_DIR=<hermes>/tools python <connector>/hermes/scripts/install_session_summarizer.py sync`
7. Smoke test:
   ```
   ./venv/Scripts/python.exe -c "from hermes_state import SessionDB; \
   print(hasattr(SessionDB, 'upsert_session_summary'), \
         hasattr(SessionDB, 'list_session_summaries'))"
   # expect: True True
   ```
8. Run the connector tests:
   ```
   ./venv/Scripts/python.exe -m pytest \
     "<connector>/hermes/tests/test_memory_tool_e2e.py" \
     "<connector>/hermes/tests/test_cortex_backend_blackbox.py" \
     "<connector>/hermes/tests/test_session_summarizer_blackbox.py" \
     -p no:cacheprovider --import-mode=importlib -q
   # expect: 55 passed, 2 skipped
   ```

## Rollback

```bash
cd /a/Application/Hermes-Agent/UserData/hermes-agent
git checkout -- hermes_state.py hermes_state_common.py agent/conversation_loop.py tools/memory_tool.py
rm -f tools/cortex_memory_backend.py tools/session_summarizer.py
# The session_summaries table stays in state.db — harmless (no callers).
```

## Verified on

- hermes-agent commit `979ca57a5` (HEAD 2026-08-18)
- Python 3.11.15 (venv at `A:\Application\Hermes-Agent\UserData\hermes-agent\venv`)
- Cortex server V6.3.1 at `http://127.0.0.1:1880`

Connector tests: 55 passed, 2 skipped (LM Studio 9b unreachable —
expected skip).