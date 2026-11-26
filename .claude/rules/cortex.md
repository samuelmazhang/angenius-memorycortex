# cortex memory — Claude Code integration rules

This project ships a Claude Code connector for the cortex memory service
(`connector/claude/`). Cortex is wired in via `~/.claude/settings.json` as
two hooks:

| Hook | Trigger | Purpose |
|---|---|---|
| `recall_hook.py` | `UserPromptSubmit` | recall relevant memories before you answer + handle `记住: <text>` saves |
| `session_end_hook.py` | `SessionEnd` | save a session-closure marker |

You do NOT need to invoke the client explicitly for routine recall — it's
already done for you. The injected context block looks like:

```
<cortex_recall>
  query: <your last user message>
  hits: N
  [1] sim=0.xx tags=[...] content snippet...
</cortex_recall>
```

## What you should do

1. **Read the `<cortex_recall>` block at the start of every turn.** It
   contains past relevant context. If the user is asking about something
   you have no immediate knowledge of, the recall block probably has it.
2. **For explicit `记住: ...` requests**, just confirm the hook handled
   it (look for the `[cortex] 已记住 → id=...` echo in your context).
   Don't double-save.
3. **For session-level persistence** (e.g. user says "save this whole
   session"), use the client directly:
   ```python
   from connector.claude import Cortex
   cx = Cortex()
   cx.remember(summary, type="session_summary", tags=[...], importance=7)
   ```
4. **For out-of-band recall** (when you suspect relevant memories that
   the hook didn't surface), call `cx.recall(topic, limit=10)` explicitly.
5. **To wipe data for a specific chunk**, use `cx.forget(chunk_id)`.

## What you should NOT do

1. Don't re-call cortex on every message — the UserPromptSubmit hook
   already does this with 2s timeout.
2. Don't `forget()` chunks without explicit user confirmation. `forget()`
   hard-deletes and removes relations — irreversible.
3. Don't run destructive ops (`reconsolidate`, `merge_duplicates`,
   `forgetting_cleanup`, `storage_evict`, `graph_mine`, `link_decay`)
   without `confirm=True`. These methods raise `PermissionError`
   otherwise — that's the guard, not a bug.
4. Don't change the embedding model mid-session without telling the user.
   Cortex chunks may be on different `model_version`s (mpnet 384-d vs
   nomic-v1.5 768-d). Search still works but per-chunk dispatch will
   pick the right model for scoring.

## Save patterns

| User says | What happens |
|---|---|
| `记住: some text` | saved as `type=knowledge`, `importance=6`, tags=[claude-hook, explicit-save] |
| `remember: some text` | same as above |
| `save: some text` | same as above |
| normal message ending session | SessionEnd hook saves a `session_summary` marker with session_id tag |

## Hook config knobs (env vars)

| Var | Default | Effect |
|---|---|---|
| `CORTEX_HOOK_ENABLED` | `1` | set to `0` to disable both hooks for a session |
| `CORTEX_HOOK_TIMEOUT` | `2.0` (recall) / `3.0` (end) | per-hook seconds cap |
| `CORTEX_CONNECTOR_DIR` | hardcoded repo path | override if repo moves |

## Connector layout

```
connector/claude/
├── claude_cortex_client.py      # 80 methods, 89 endpoints
├── hooks/
│   ├── recall_hook.py           # UserPromptSubmit → recall + "记住:" save
│   └── session_end_hook.py      # SessionEnd → session-summary marker
├── scripts/claude_smoke.py      # standalone 89-endpoint sweep
├── __init__.py
└── README.md
```

## What cortex knows about (typical contents)

- `type`: `general | knowledge | preference | skill | fact | project |
   session_summary | tool_experience | environment-fingerprint | test |
   context`
- `tags`: free-form, often `["DragonQuant", "Sprint-100-105-FINAL", "ADR-..."]`
- `importance`: 1–10, drives tier + TTL (≤3=7d, ≤5=30d, ≤7=180d, ≥8=permanent)
- `source`: `claude-code | claude-code-hook | user | hermes_memory | ...`

Live server: `http://127.0.0.1:1880` (override via `CORTEX_URL`).
Endpoint coverage: 89/89 (validated 2026-09-08).