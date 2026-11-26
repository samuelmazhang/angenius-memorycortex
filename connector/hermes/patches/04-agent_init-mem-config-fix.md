# Patch 04 — agent_init.py `_init_memory` builtin memory init (V6.5.2, 2026-09-17)

## What this fixes

Every hermes session prints at startup:

```
WARNING run_agent: Memory provider 'memory-cortex' is selected but reports unavailable
```

And the agent runs the whole session with `_memory_manager = None`. Cortex never
gets any writes from hermes. `hermes memory status` shows `available ✓` because
that command does **not** go through the broken init code, which made this very
hard to spot.

`/dashboard.total_memories` stays flat across weeks (2369 from 2026-08-04 to
2026-09-17), confirming that hermes has not written to cortex in over a month.

## Target file

`agent/agent_init.py`, function `_init_memory`, around line 1253.

## Root cause

`hermes-agent/agent/agent_init.py::_init_memory` had this block:

```python
if not skip_memory or _memory_toolset_requested:
    # Memory is optional — don't break agent init
    with suppress(Exception):
        from tools.memory_tool import (
            MemoryStore,
            get_builtin_memory_config,        # ← does not exist
            get_builtin_memory_store_flags,   # ← does not exist
        )
        mem_config = get_builtin_memory_config(_agent_cfg)   # never runs
```

Two helper functions (`get_builtin_memory_config`,
`get_builtin_memory_store_flags`) were imported but **never defined** in
`tools/memory_tool.py`. The whole `with suppress(Exception):` swallowed the
`ImportError`, leaving `mem_config` unbound. A few lines later the plugin
init path tried to read it:

```python
_mem_provider_name = mem_config.get("provider", "") if mem_config else ""
```

This raises `UnboundLocalError: cannot access local variable 'mem_config' where
it is not associated with a value`. That error is also swallowed by a second
`except Exception as _mpe:` that logs the misleading `reports unavailable`
warning. Two swallowed exceptions = one misleading WARNING + zero cortex writes.

## Fix

Apply this patch to
`A:\Application\Hermes-Agent\Program\hermes-agent\agent\agent_init.py`
in the `_init_memory` function (around line 1253).

**Change 1**: pre-bind `mem_config` as `{}` outside the suppress block so a
builtin-memory init failure cannot UnboundLocalError downstream.

**Change 2**: replace the missing helpers with direct config reads, matching the
pattern in `tools/memory_tool.py::load_on_disk_store()` (line ~2480).

### Before

```python
if not skip_memory or _memory_toolset_requested:
    # Memory is optional — don't break agent init
    with suppress(Exception):
        from tools.memory_tool import (
            MemoryStore, get_builtin_memory_config, get_builtin_memory_store_flags,
        )
        mem_config = get_builtin_memory_config(_agent_cfg)
        agent._memory_enabled, agent._user_profile_enabled = get_builtin_memory_store_flags(
            _agent_cfg
        )
        agent._memory_nudge_interval = int(mem_config.get("nudge_interval", 10))
        if agent._memory_enabled or agent._user_profile_enabled:
            agent._memory_store = MemoryStore(
                memory_char_limit=mem_config.get("memory_char_limit", 2200),
                user_char_limit=mem_config.get("user_char_limit", 1375),
                memory_enabled=agent._memory_enabled,
                user_profile_enabled=agent._user_profile_enabled,
            )
            agent._memory_store.load_from_disk()
```

### After

```python
if not skip_memory or _memory_toolset_requested:
    mem_config: dict = {}  # pre-bind so a builtin-memory init failure does not UnboundLocalError below
    # Memory is optional — don't break agent init
    with suppress(Exception):
        from tools.memory_tool import (
            MemoryStore,
        )
        from hermes_cli.config import load_config as _load_config
        mem_config = (_load_config() or {}).get("memory", {}) or {}
        agent._memory_enabled = bool(mem_config.get("enabled", True))
        agent._user_profile_enabled = bool(mem_config.get("user_profile", True))
        agent._memory_nudge_interval = int(mem_config.get("nudge_interval", 10))
        if agent._memory_enabled or agent._user_profile_enabled:
            agent._memory_store = MemoryStore(
                memory_char_limit=int(mem_config.get("memory_char_limit", 2200)),
                user_char_limit=int(mem_config.get("user_char_limit", 1375)),
                memory_enabled=agent._memory_enabled,
                user_profile_enabled=agent._user_profile_enabled,
            )
            agent._memory_store.load_from_disk()
```

The downstream `if mem_config else ""` fallback on the plugin path is kept —
it's now dead code (`mem_config` is always `{}` or a populated dict, never
`None`) but it costs nothing and makes the file robust against partial reverts.

## Why the original code looks like it should work

`hermes_state_common.py` patch 1 + `hermes_state.py` patches 2-5 in
`HERMES_INTERNAL_PATCHES.md` add the `session_summaries` schema and SessionDB
methods. They are NOT related to this bug — this bug is upstream of those
patches, in `agent_init.py` itself, and predates V6.3.1.

If you are reading this for the first time after a fresh hermes install, the
fix below is **required** even if you only intend to use the built-in
memory_store. The bug blocks the memory-cortex plugin init path even when
`memory.provider` is unset, because the imported-name failure happens before
the provider check.

## Verification

Run `scripts/verify_init_memory.py` (in this directory). It builds a mock
agent with all required attributes and calls
`agent_init._init_memory(agent, _agent_cfg, skip_memory=False, platform='cli')`.

### Expected output after fix is applied

```
run_agent - INFO - Memory provider 'memory-cortex' activated
  agent._memory_manager: <agent.memory_manager.MemoryManager object at ...>
  providers: ['CortexMemoryProvider']
  agent._memory_enabled: True
  agent._user_profile_enabled: True
```

### Expected output before fix is applied

```
run_agent - WARNING - Memory provider 'memory-cortex' is selected but reports unavailable
  agent._memory_store: None
  agent._memory_manager: None
  agent._memory_enabled: True
  agent._user_profile_enabled: True
```

(`_memory_enabled` and `_user_profile_enabled` are still True because the
second block (`if not skip_memory:`) reads `mem_config.get(...)` which
**also** raises UnboundLocalError, swallowed silently — so neither the
builtin store nor the plugin ever loads.)

## Production restart required

The fix only takes effect on the next `hermes` process start. Currently-running
hermes sessions still have the broken code in memory.

After restarting hermes, watch `agent.log` for the first turn — it should show:

```
agent.memory_manager: Memory provider 'memory-cortex' registered (4 tools)
run_agent: Memory provider 'memory-cortex' activated
```

instead of the previous `reports unavailable` warning.

## Cross-references

- Source: `agent/agent_init.py`
- Function: `_init_memory` (around line 1253)
- Verification: `scripts/verify_init_memory.py` (this directory)
- Apply-script: `scripts/apply_patches.py` (this directory)
- Index: `HERMES_INTERNAL_PATCHES.md` patch #4