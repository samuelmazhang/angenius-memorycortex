# Cortex full takeover (2026-08-23)

## Decision

Removed the legacy `MEMORY.md` and `USER.md` file-based memory
in favor of cortex (PostgreSQL + pgvector + FTS5). cortex is
now the **only** memory backend for the hermes memory tool.

## What was removed

- `~/.minimax/agents/mavis/memory/MEMORY.md`
  (9263 bytes, 114 lines, last modified 2026-08-22)
  -- BootstrapBlazor component reference table
- `USER.md` was already absent (no action needed)

Backup: `~/.cortex-backups/2026-08-23-cortex-takeover/MEMORY.md`
(repo's `backups/` directory is gitignored -- backup lives
outside the repo, in user home, so it's preserved indefinitely
without bloating git history)

## Why this is safe

- `CORTEX_TAKEOVER` default in `connector/hermes/memory_tool.py`
  is `"1"` (cortex). The `.md` files are only consulted when
  `CORTEX_TAKEOVER=0` is set explicitly. Deleting them does
  not change the production path.
- The MEMORY.md content (BootstrapBlazor component mapping) is
  already fully present in cortex: 1282 chunks with content
  matching `%blazor%` (source=`user`), versus 4 mentions in
  MEMORY.md. The cortex chunks are more granular (one per
  component, not one summary table).
- USER.md was not present, so the user-preference channel
  was already empty / pure cortex.

## State after removal

  * `~/.minimax/agents/mavis/memory/` is now empty.
  * cortex on 127.0.0.1:1880 is healthy (11992 vectors).
  * All hermes memory operations go through `cortex_recall` /
    `cortex_remember` MCP tools, which read from / write to
    the cortex database.

## Rollback

If a future user wants the file fallback back for offline
sessions:

  1. Restore `backups/2026-08-23-cortex-takeover/MEMORY.md`
     to the original path.
  2. Set `CORTEX_TAKEOVER=0` in the hermes runtime environment
     to make the memory tool read the file again.

The .md backend code is still in
`connector/hermes/memory_tool.py`; it just won't be used by
default.