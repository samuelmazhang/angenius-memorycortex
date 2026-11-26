#!/usr/bin/env python3
"""install_session_summarizer.py - sync session_summarizer.py + cortex_memory_backend.py from
connector repo to hermes-agent's tools/ directory.

Usage:
    python install_session_summarizer.py           # 同步 (default action)
    python install_session_summarizer.py check    # 只校验,不真复制
    python install_session_summarizer.py diff     # 显示 connector vs hermes 差异

Env:
    HERMES_TOOLS_DIR  default A:\\Application\\Hermes-Agent\\UserData\\hermes-agent\\tools
                       (Windows) — the LIVE running hermes venv lives here.
                       Override if you run hermes from a different clone.
    CONNECTOR_DIR     default <repo>/connector/hermes

Files synced (when source mtime > destination mtime, OR --force):
    - session_summarizer.py (V6.3.1)
    - cortex_memory_backend.py (V6.3.1)
    - cortex_memory_store.py (Hermes v0.22 split-layout takeover port)
    - memory_tool.py (from memory_tool_v022.py — the v0.22 thin wrapper)

Does NOT touch:
    - hermes_state.py / hermes_state_common.py / conversation_loop.py
      These are HERMES-internal patches that DEPEND on this V6.3.1 connector
      (3 SessionDB methods + 3 hooks + session_summaries schema). The full
      diff list lives in connector/hermes/HERMES_INTERNAL_PATCHES.md.
      Apply them with the patch sequence in that file BEFORE first run;
      the install_session_summarizer sync then completes the connector side.

Layout note (2026-11-10):
    Hermes v0.22.x SPLIT the memory tool — ``tools/memory_tool.py`` became a
    thin wrapper and the store class moved to ``tools/memory_tool_store.py``.
    The cortex takeover therefore lives in ``tools/cortex_memory_store.py``
    (subclass of upstream MemoryStore), NOT in the pre-v0.22 monolith.

    ``connector/hermes/memory_tool.py`` is the OLD pre-v0.22 monolith and is
    kept for reference / patch replay only — copying it over the live
    ``tools/memory_tool.py`` would DOWNGRADE the runtime and break the
    takeover.  The live v0.22 wrapper is version-controlled here as
    ``memory_tool_v022.py`` and is what gets synced.
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

# V6.3.1: live hermes runs from UserData/, NOT Program/. Program/ is a
# stale check-out that's no longer the running instance. If you have
# multiple hermes clones, override via HERMES_TOOLS_DIR.
DEFAULT_HERMES_TOOLS = (
    r"A:\Application\Hermes-Agent\UserData\hermes-agent\tools"  # Windows — live hermes
)
DEFAULT_CONNECTOR_DIR = Path(__file__).resolve().parent.parent  # connector/hermes/

# (source-in-connector -> filename-in-hermes-tools)
# memory_tool.py is mapped from memory_tool_v022.py on purpose: the live
# Hermes v0.22 layout uses the thin wrapper, and the pre-v0.22 monolith in
# this repo would break the cortex takeover if copied over it.
FILES_TO_SYNC = [
    "session_summarizer.py",
    "cortex_memory_backend.py",
    "cortex_memory_store.py",
    ("memory_tool_v022.py", "memory_tool.py"),
]


def find_hermes_tools_dir() -> Path:
    """Resolve hermes tools dir, with Windows-path normalization for MSYS callers."""
    raw = os.environ.get("HERMES_TOOLS_DIR", DEFAULT_HERMES_TOOLS)
    p = Path(raw)
    if p.exists():
        return p
    # MSYS fallback: try /a/Application/... (Windows git-bash path style)
    msys_alt = Path("/a/Application/Hermes-Agent/UserData/hermes-agent/tools")
    if msys_alt.exists():
        return msys_alt
    print(f"ERROR: hermes tools dir not found: {raw} (also tried {msys_alt})", file=sys.stderr)
    sys.exit(2)


def copy_file(src: Path, dst: Path, force: bool) -> bool:
    """Copy src to dst if src is newer or force=True. Returns True if copied."""
    if not src.exists():
        print(f"  SKIP (missing source): {src}")
        return False
    if dst.exists() and not force:
        src_mtime = src.stat().st_mtime
        dst_mtime = dst.stat().st_mtime
        if src_mtime <= dst_mtime:
            print(f"  SKIP (up to date): {dst.name}")
            return False
    shutil.copy2(src, dst)
    print(f"  COPIED: {src.name} -> {dst}")
    return True


def _pairs() -> list[tuple[str, str]]:
    """Normalise FILES_TO_SYNC into (source_name, dest_name) pairs."""
    out = []
    for entry in FILES_TO_SYNC:
        if isinstance(entry, tuple):
            out.append((entry[0], entry[1]))
        else:
            out.append((entry, entry))
    return out


def action_sync(hermes_tools: Path, connector_dir: Path, force: bool) -> int:
    n_copied = 0
    for src_name, dst_name in _pairs():
        src = connector_dir / src_name
        dst = hermes_tools / dst_name
        if copy_file(src, dst, force=force):
            n_copied += 1
    print(f"\nDone: {n_copied} file(s) synced into {hermes_tools}")
    return 0 if n_copied >= 0 else 1


def action_check(hermes_tools: Path, connector_dir: Path) -> int:
    n_drift = 0
    for src_name, dst_name in _pairs():
        src = connector_dir / src_name
        dst = hermes_tools / dst_name
        if not dst.exists():
            print(f"  MISSING in hermes: {dst_name} (from {src_name})")
            n_drift += 1
            continue
        if src.stat().st_mtime > dst.stat().st_mtime:
            print(f"  STALE in hermes: {dst_name} (connector {src_name} newer)")
            n_drift += 1
            continue
        print(f"  OK: {dst_name}")
    if n_drift:
        print(f"\n{n_drift} drift(s) detected. Run: python install_session_summarizer.py sync")
        return 1
    print("\nAll files in sync.")
    return 0


def action_diff(hermes_tools: Path, connector_dir: Path) -> int:
    import subprocess
    for src_name, dst_name in _pairs():
        src = connector_dir / src_name
        dst = hermes_tools / dst_name
        if not dst.exists():
            print(f"\n--- {dst_name}: MISSING in hermes (source {src_name}) ---")
            continue
        print(f"\n--- diff: {dst_name} (from {src_name}) ---")
        subprocess.run(
            ["diff", "-u", str(dst), str(src)],
            check=False,
        )
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument(
        "action",
        nargs="?",
        default="sync",
        choices=["sync", "check", "diff"],
        help="sync (default) | check | diff",
    )
    p.add_argument("--force", action="store_true", help="sync even when dest is newer")
    args = p.parse_args()

    connector_dir = DEFAULT_CONNECTOR_DIR
    hermes_tools = find_hermes_tools_dir()

    if args.action == "sync":
        return action_sync(hermes_tools, connector_dir, force=args.force)
    elif args.action == "check":
        return action_check(hermes_tools, connector_dir)
    elif args.action == "diff":
        return action_diff(hermes_tools, connector_dir)


if __name__ == "__main__":
    sys.exit(main())
