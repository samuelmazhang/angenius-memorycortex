"""Verify Patch 04 — agent_init._init_memory no longer UnboundLocalError on
builtin-memory init failure.

Run from the hermes venv with `Program/hermes-agent` on sys.path:

    /a/Application/Hermes-Agent/Program/hermes-agent/venv/Scripts/python.exe \\
        ".connector\\hermes\\scripts\\verify_init_memory.py"

Exit codes:
    0 = fix is in place (provider activated)
    1 = fix is missing (provider NOT activated — UnboundLocalError swallowed)
    2 = script error (env problem)
"""
import sys
import os

HERMES_REPO = r"A:/Application/Hermes-Agent/Program/hermes-agent"
if HERMES_REPO not in sys.path:
    sys.path.insert(0, HERMES_REPO)
os.chdir(HERMES_REPO)


class MockAgent:
    """Minimal stand-in matching the fields `_init_memory` and
    `_memory_provider_init_kwargs` read. Real AIAgents have all of these."""

    def __init__(self):
        self.enabled_toolsets = ["memory"]
        self.disabled_toolsets = []
        self._memory_store = None
        self._memory_manager = None
        self._memory_enabled = False
        self._user_profile_enabled = False
        self._memory_nudge_interval = 10
        self._turns_since_memory = 0
        self._iters_since_skill = 0
        self.session_id = "patch-04-verify-2026-09-17"
        self.user_id = "hermes"
        self._emit_warning = lambda *a, **kw: None
        self._emit_status = lambda *a, **kw: None
        self._session_db = None
        # gateway identity fields — all None so identity path is a no-op
        for ident in (
            "_user_id",
            "_user_id_alt",
            "_user_name",
            "_chat_id",
            "_chat_name",
            "_chat_type",
            "_thread_id",
            "_gateway_session_key",
        ):
            setattr(self, ident, None)


def main():
    try:
        from agent import agent_init
    except Exception as exc:
        print(f"FAIL: cannot import agent.agent_init: {type(exc).__name__}: {exc}")
        return 2

    agent = MockAgent()
    cfg = {
        "memory": {
            "enabled": True,
            "provider": "memory-cortex",
            "memory_char_limit": 2200,
            "user_char_limit": 1375,
        }
    }

    try:
        agent_init._init_memory(agent, cfg, skip_memory=False, platform="cli")
    except Exception as exc:
        print(f"FAIL: _init_memory raised: {type(exc).__name__}: {exc}")
        return 2

    activated = agent._memory_manager is not None
    providers = []
    if agent._memory_manager:
        ps = agent._memory_manager.providers
        if isinstance(ps, list):
            providers = [type(p).__name__ for p in ps]
        else:
            providers = list(ps.keys())

    print(f"  _memory_manager: {agent._memory_manager}")
    print(f"  _memory_enabled: {agent._memory_enabled}")
    print(f"  _user_profile_enabled: {agent._user_profile_enabled}")
    print(f"  providers: {providers}")

    if activated and "CortexMemoryProvider" in providers:
        print("PASS: Patch 04 is in place — memory-cortex provider activated.")
        return 0

    print("FAIL: Patch 04 missing — memory-cortex provider not activated.")
    print("      Apply patches/04-agent_init-mem-config-fix.md to fix.")
    return 1


if __name__ == "__main__":
    sys.exit(main())