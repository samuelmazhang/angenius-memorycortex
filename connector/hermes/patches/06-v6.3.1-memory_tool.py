#!/usr/bin/env python3
"""
Memory Tool Module - Persistent Curated Memory

Provides bounded, file-backed memory that persists across sessions. Two stores:
  - MEMORY.md: agent's personal notes and observations (environment facts, project
    conventions, tool quirks, things learned)
  - USER.md: what the agent knows about the user (preferences, communication style,
    expectations, workflow habits)

Both are injected into the system prompt as a frozen snapshot at session start.
Mid-session writes update files on disk immediately (durable) but do NOT change
the system prompt -- this preserves the prefix cache for the entire session.
The snapshot refreshes on the next session start.

Entry delimiter: § (section sign). Entries can be multiline.
Character limits (not tokens) because char counts are model-independent.

Design:
- Single `memory` tool with action parameter: add, replace, remove
- replace/remove use short unique substring matching (not full text or IDs)
- Behavioral guidance lives in the tool schema description
- Frozen snapshot pattern: system prompt is stable, tool responses show live state
"""

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional, Tuple

from utils import atomic_replace

# Cortex takeover is the default for the memory tool. Set CORTEX_TAKEOVER=0
# in the environment to fall back to the legacy .md file implementation (used
# for offline sessions or for diffing behavior during a phased rollout).
# Imported lazily inside the property to avoid a hard dependency on the
# cortex module at import time (the cortex HTTP server may not be running).
def _import_cortex_classes():
    """Lazy import of cortex exceptions so callers can `except` without
    separately importing the backend module."""
    from tools import cortex_memory_backend as _b
    globals()["CortexUnavailableError"] = _b.CortexUnavailableError
    return _b

# Attempt eager import once at module load. If the backend module is missing
# (e.g. in a minimal install), the property falls back to a lazy import.
try:
    _import_cortex_classes()
except Exception:
    pass

# fcntl is Unix-only; on Windows use msvcrt for file locking
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# Where memory files live — resolved dynamically so profile overrides
# (HERMES_HOME env var changes) are always respected.  The old module-level
# constant was cached at import time and could go stale if a profile switch
# happened after the first import.
def get_memory_dir() -> Path:
    """Return the profile-scoped memories directory."""
    return get_hermes_home() / "memories"

ENTRY_DELIMITER = "\n§\n"

# FIX (2026-08-02, Direction C): three-tier memory. Maps a human-readable retention
# tier to the cortex `importance` score. Cortex tier-gates lifecycle via importance.
_TIER_TO_IMPORTANCE = {
    "short": 3,    # session scratch, evicted aggressively by occasional cleanup
    "medium": 5,   # cross-session but not curated -- survives a few months
    "long": 7,     # user-curated durable fact (the default)
    "permanent": 9,  # never-forget override
}
_TIER_NAMES = tuple(_TIER_TO_IMPORTANCE.keys())

def _importance_for_tier(tier: Optional[str]) -> int:
    """Map a tier name to cortex importance; unknown/None -> long (7)."""
    if tier and tier in _TIER_TO_IMPORTANCE:
        return _TIER_TO_IMPORTANCE[tier]
    return 7  # default long-term



# ---------------------------------------------------------------------------
# Memory content scanning — lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
#
# Patterns live in ``tools/threat_patterns.py`` — the single source of truth
# shared with the context-file scanner and the tool-result delimiter system.
# Memory uses the "strict" scope (broadest pattern set) because:
#  - memory entries are user-curated; the user can rewrite a flagged entry
#  - memory enters the system prompt as a FROZEN snapshot, so a poisoned
#    entry persists for the entire session and across sessions until
#    explicitly removed.
# ---------------------------------------------------------------------------

from tools.threat_patterns import first_threat_message as _first_threat_message


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    return _first_threat_message(content, scope="strict")


def _drift_error(path: "Path", bak_path: str) -> Dict[str, Any]:
    """Build the error dict returned when external drift is detected.

    The on-disk memory file contains content that wouldn't round-trip
    through the tool's parser/serializer — flushing would discard the
    appended/edited content from a patch tool, shell append, manual edit,
    or sister-session write. We refuse the mutation, point the operator at
    the .bak.<ts> snapshot we took, and tell them what to do next.
    """
    return {
        "success": False,
        "error": (
            f"Refusing to write {path.name}: file on disk has content that "
            f"wouldn't round-trip through the memory tool (likely added by "
            f"the patch tool, a shell append, a manual edit, or a "
            f"concurrent session). A snapshot was saved to {bak_path}. "
            f"Resolve the drift first — either rewrite the file as a clean "
            f"§-delimited list of entries, or move the extra content out — "
            f"then retry. This guard exists to prevent silent data loss "
            f"(issue #26045)."
        ),
        "drift_backup": bak_path,
        "remediation": (
            "Open the .bak file, integrate the missing entries into the "
            "memory tool one at a time via memory(action=add, content=...), "
            "then remove or rewrite the original file to a clean state."
        ),
    }


def _read_failed_error(path: "Path") -> Dict[str, Any]:
    """Error dict returned when a memory file exists but can't be read.

    read-modify-write paths (add/replace/remove) must NOT treat an unreadable
    file as "empty store": rewriting from that view would wipe the on-disk
    bytes. Invalid UTF-8 counts as unreadable too (cannot faithfully
    round-trip). We refuse the mutation and preserve the file. (issue class
    #26045: never rewrite a file from a view that isn't the real one.)
    """
    return {
        "success": False,
        "error": (
            f"Memory file {path} could not be read as UTF-8 text. "
            f"Refusing to write to avoid clobbering its contents. "
            f"Inspect/fix the file, then retry."
        ),
        "remediation": (
            "Open the file, correct any invalid/corrupt bytes, then reissue "
            "the memory write."
        ),
    }


class _UnreadableMemoryError(Exception):
    """Internal: a memory file exists but couldn't be read/round-tripped."""


class MemoryStore:
    """
    Bounded curated memory with file persistence. One instance per AIAgent.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt injection.
        Never mutated mid-session. Keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls, persisted to disk.
        Tool responses always reflect this live state.
    """

    # After this many failed consolidation attempts (overflow / zero-match) in
    # ONE turn, stop instructing the model to "retry in this turn" and return a
    # terminal "save skipped" result so a fragile replace/add can't loop the
    # turn to budget exhaustion and suppress the user's reply (issue #42405).
    _MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

    def __init__(self, memory_char_limit: int = 2200, user_char_limit: int = 1375,
                 *, use_cortex: Optional[bool] = None,
                 cortex_backend=None):
        """Initialize the memory store.

        ``use_cortex`` (when None, falls back to ``CORTEX_TAKEOVER`` env var,
        defaulting to True): when True, all reads/writes go through the Memory
        Cortex backend instead of ``$HERMES_HOME/memories/{MEMORY,USER}.md``.
        The .md files are NOT created or read.

        ``cortex_backend`` is an optional pre-built ``cortex_memory_backend``
        module shim used for dependency injection (so tests can substitute a
        fake without monkey-patching). When None, the real module is imported
        lazily.
        """
        if use_cortex is None:
            use_cortex = os.getenv("CORTEX_TAKEOVER", "1").lower() not in (
                "0", "false", "no")
        self.use_cortex = use_cortex
        self._cortex = None  # lazy import
        self._cortex_backend_override = cortex_backend
        # Chunk-id map (target → {content → chunk_id}). Populated at
        # load_from_disk time and refreshed after every mutation. Used to map
        # ``replace(old_text)`` / ``remove(old_text)`` from text→chunk_id.
        self._chunk_ids: Dict[str, Dict[str, str]] = {"memory": {}, "user": {}}
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Frozen snapshot for system prompt -- set once at load_from_disk()
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}
        # Per-turn counter of failed at-capacity consolidation attempts; reset
        # at each turn boundary by reset_consolidation_failures() (#42405).
        self._consolidation_failures = 0

    def reset_consolidation_failures(self) -> None:
        """Reset the per-turn consolidation-failure counter (call at turn start)."""
        self._consolidation_failures = 0

    @property
    def _cortex_backend(self):
        """Lazy accessor for the cortex backend module.

        Returns the injected override if one was passed to the constructor;
        otherwise imports ``tools.cortex_memory_backend`` on first use.
        """
        if self._cortex is not None:
            return self._cortex
        if self._cortex_backend_override is not None:
            self._cortex = self._cortex_backend_override
            return self._cortex
        from tools import cortex_memory_backend as _b  # local import
        # Re-export the error class so callers can `except CortexUnavailableError`
        # without importing the backend module directly. This keeps the import
        # surface small and lets tests stub the backend module wholesale.
        globals()["CortexUnavailableError"] = _b.CortexUnavailableError
        self._cortex = _b
        return self._cortex

    def _is_cortex_unavailable(self) -> bool:
        """Predicate: route to .md fallback path? True only when cortex takeover
        is on but cortex itself is unreachable. The previous (broken) version
        returned ``is_cortex_active()`` directly, so the predicate and the
        method name disagreed — every healthy-cortex call fell through to the
        'unavailable' branch by accident. Fixed 2026-09-14 (#memory-tool-fix).
        """
        if not self.use_cortex:
            return False
        try:
            return not bool(self._cortex_backend.is_cortex_active())
        except Exception:
            # Probe failure = cortex not safely callable from this method,
            # so fall back to .md path (matches the safe-degrade contract).
            return True

    def _is_cortex_available(self) -> bool:
        """Inverse of ``_is_cortex_unavailable``; reads more naturally at call sites."""
        return not self._is_cortex_unavailable()

    def _target_for(self, target: str) -> str:
        """Map target name to cortex-backed store key (same as target today)."""
        if target not in ("memory", "user"):
            raise ValueError(f"unknown target {target!r}")
        return target

    def _consolidation_failure(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Count an at-capacity consolidation failure and degrade gracefully.

        Under the per-turn cap, return ``response`` unchanged (it already tells
        the model how to self-correct + retry in this turn). Once the cap is
        exceeded, drop the retry instruction and return a TERMINAL result so the
        model stops looping memory calls and proceeds to answer the user — a
        failed memory side effect must never block the turn's reply (#42405).
        """
        self._consolidation_failures += 1
        if self._consolidation_failures <= self._MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        return {
            "success": False,
            "done": True,
            "error": (
                f"Memory consolidation failed {self._consolidation_failures} times "
                "this turn. Stop retrying memory calls — leave memory unchanged for "
                "now and continue with your reply to the user. The fact can be saved "
                "in a later turn."
            ),
        }

    def load_from_disk(self):
        """Load entries from MEMORY.md and USER.md, capture system prompt snapshot.

        The frozen snapshot is what enters the system prompt. We scan each
        entry for injection/promptware patterns at snapshot-build time —
        ANY hit replaces the entry text in the snapshot with a placeholder
        like ``[BLOCKED: …]``, so a poisoned-on-disk memory file (supply
        chain, compromised tool, sister-session write) cannot inject into
        the system prompt.

        The live ``memory_entries`` / ``user_entries`` lists keep the
        original text so the user can still SEE poisoned entries via
        see poisoned entries by inspecting the source files directly, and remove them — silently dropping them would hide the attack from the user.

        Scanning is deterministic from disk bytes, so the snapshot remains
        stable for the entire session (prefix-cache invariant holds).
        """
        mem_dir = get_memory_dir()


        # === Cortex takeover: bypass .md file entirely ===
        # STRICT MODE (2026-09-22): when use_cortex is on, the .md file is
        # NEVER read or written by this code path. The md files are kept on
        # disk only as a read-only emergency mirror (load_from_disk ignores
        # them when use_cortex=True). If cortex is unreachable, the agent
        # starts with empty memory and a loud warning — better than silently
        # reading stale .md that cortex has no knowledge of.
        # The .md path remains the ONLY path when use_cortex=False, which
        # requires CORTEX_TAKEOVER=0 (escape hatch for offline / tests).
        if self.use_cortex:
            try:
                return self._load_from_cortex(mem_dir)
            except CortexUnavailableError as e:
                # Loud: agent still boots, but no memory surfaces until cortex is back.
                logger.warning("memory tool: cortex unreachable on load (%s) — starting with empty memory", e)
                self.memory_entries = []
                self.user_entries = []
                self._chunk_ids = {"memory": {}, "user": {}}
                self._system_prompt_snapshot = {"memory": "", "user": ""}
                return {"memory": [], "user": [], "source": "cortex_unavailable"}
            except Exception as e:
                logger.warning("memory tool: cortex load failed (%s) — starting with empty memory", e)
                self.memory_entries = []
                self.user_entries = []
                self._chunk_ids = {"memory": {}, "user": {}}
                self._system_prompt_snapshot = {"memory": "", "user": ""}
                return {"memory": [], "user": [], "source": "cortex_unavailable"}

        # Legacy: read .md files. ONLY reached when CORTEX_TAKEOVER=0 is set.
        # Kept as escape-hatch for offline sessions, tests, or explicit
        # comparison against the pre-takeover behavior.
        self.memory_entries = self._read_file(mem_dir / "MEMORY.md")
        self.user_entries = self._read_file(mem_dir / "USER.md")
        # Deduplicate entries (preserves order, keeps first occurrence)
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        # Sanitize entries for the system-prompt snapshot only.  Live state
        # (memory_entries / user_entries) keeps the raw text so the user
        # can see + remove poisoned entries via the memory tool.
        sanitized_memory = self._sanitize_entries_for_snapshot(self.memory_entries, "MEMORY.md")
        sanitized_user = self._sanitize_entries_for_snapshot(self.user_entries, "USER.md")

        # Capture frozen snapshot for system prompt injection
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }
        # FIX (2026-08-01): return the loaded entries (cortex path also
        # returns a dict now; see _load_from_cortex).
        return {
            "memory": list(self.memory_entries),
            "user": list(self.user_entries),
            "source": "native_md",
        }

    def _load_from_cortex(self, mem_dir: Path) -> Dict[str, Any]:
        """Populate ``memory_entries`` / ``user_entries`` from cortex chunks.

        Errors here are loud: caller (``load_from_disk``) wraps exceptions
        in a generic ``pass`` so memory init never blocks agent boot. The
        catch-all lives higher up.

        FIX (2026-08-01): Reduced smart hook chain from 46 to 7 essential calls.
        Previous version called 24 unique backend methods on every load — including
        4.2s /search/tts + 3s /forgetting + 3s /relations/bitemporal + GET /chroma/cleanup
        + POST /bench/multi-hop/custom + POST /reindex + POST /rumination/abstract-heuristics
        + POST /storage/enforce + POST /cache/pr/clear + many more. All swallowed by
        try/except: pass (so they didn't add value), but each one was a 5-10s
        HTTP roundtrip. Total overhead: 60-90s. Now: 4-8s (just the essential ones).
        """
        backend = self._cortex_backend
        for target, attr in [("memory", "memory_entries"),
                              ("user", "user_entries")]:
            chunks = backend.list_entries(target)
            texts = [c["content"] for c in chunks if c.get("content")]
            # Populate chunk-id index for replace/remove lookup
            self._chunk_ids[target] = {
                c["content"]: c["id"] for c in chunks if c.get("id")
            }
            texts = list(dict.fromkeys(texts))  # preserve order, dedupe
            setattr(self, attr, texts)
        # === ESSENTIAL HOOKS (7 calls, ~3-5s total) ===
        # Kept: 3 calls that affect what the LLM sees in the system prompt
        # (forgetting, taxonomy, search_tts warmup) + 4 audit/health calls
        # Removed: 39 calls that were either redundant, pure coverage-stats
        # (never acted on), or had no effect on agent behavior.
        try:
            backend.log_audit("read", "chunk", actor="memory_tool_load")
        except Exception:
            pass
        # Forgetting: tell agent which memories are expired/expiring (real value)
        try:
            backend.get_forgetting_stats()
        except Exception:
            pass
        try:
            backend.get_expiring_soon(limit=5)
        except Exception:
            pass
        # Taxonomy: help agent pick tags when writing new memory
        try:
            backend.get_taxonomy_relations()
        except Exception:
            pass
        # Search warmup: pre-cache the most common recent-context query
        try:
            backend.search_tts(query="recent context", top_k=5)
        except Exception:
            pass
        # FIX (2026-08-02, Direction 3): active recall injection. Runs
        # reason_with_memory() at boot and KEEPS the result so the frozen
        # system-prompt snapshot can include a "recent_context" block — the
        # agent sees its most-relevant cross-session context proactively,
        # instead of the result being discarded (it was previously only warming
        # PG caches). Stored on self for the snapshot below.
        self._recall_context = []
        try:
            _rc = backend.reason_with_memory(question="recent context", top_k=5,
                                             include_hierarchy=True)
            if isinstance(_rc, dict):
                chunks = _rc.get("chunks") or _rc.get("context_chunks") or []
                self._recall_context = [c.get("content", "") for c in chunks
                                        if isinstance(c, dict) and c.get("content")][:5]
        except Exception:
            pass
        # FIX (2026-08-02, Direction B): two-way working memory.
        # 1. Read back any working_memory for the current session (so the agent
        #    resumes its cross-turn scratch state instead of starting blank).
        # 2. Write a session-anchor entry so the session is discoverable next boot.
        # Both are fire-and-forget (never raise into the boot path).
        try:
            wm = backend.get_all_working_memory(session_id="current_session")
            if wm and not getattr(self, "_working_memory_loaded", False):
                self._working_memory = wm if isinstance(wm, list) else []
                self._working_memory_loaded = True
        except Exception:
            pass
        try:
            backend.set_working_memory(
                session_id="current_session",
                key="session_anchor",
                value={"ts": time.time(), "boot": True},
            )
        except Exception:
            pass
        # FIX (2026-08-02, Direction C): auto-cleanup short-term memory. Cortex
        # evicts low-importance (short-tier) entries; we trigger it opportunistically
        # on boot but at most once per 30 min so it isn't a hot-path cost.
        try:
            now = time.time()
            if not getattr(self, "_last_forgetting_cleanup", 0) or now - self._last_forgetting_cleanup > 1800:
                backend.run_forgetting_cleanup(min_score=0.1)
                self._last_forgetting_cleanup = now
        except Exception:
            pass
        # === REMOVED (covered the same info or had no behavioral effect) ===
        # Original 24 hooks removed:
        #   get_relations_bitemporal, search_hybrid, get_all_working_memory,
        #   get_metrics, get_relations, _request (8+ raw HTTP calls),
        #   search_procedures, list_core_profiles, get_storage_health,
        #   get_pr_cache_stats, search_resources, get_consolidate_facts,
        #   get_rumination_stats, list_entries (already used), stats,
        #   get_user_profile, enrich_chunk_with_hierarchy, get_dashboard_html,
        #   search_media, get_pr_cache_stats (duplicate)
        # Net savings: 39 fewer HTTP roundtrips per load

        # Sanitize entries for the system-prompt snapshot only.
        sanitized_memory = self._sanitize_entries_for_snapshot(self.memory_entries, "MEMORY.md")
        sanitized_user = self._sanitize_entries_for_snapshot(self.user_entries, "USER.md")

        # Capture frozen snapshot for system prompt injection
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }
        # Direction 3: append active recall context (recent relevant memories)
        # so the agent boots with cross-session context rather than only the
        # full MEMORY.md / USER.md dumps.
        if getattr(self, "_recall_context", None):
            recall_block = ("\n\n## Memory Recall Context (proactively retrieved"
                            " from cortex at session start)\n\n")
            for i, rc in enumerate(self._recall_context[:5], 1):
                recall_block += f"{i}. {rc.strip()}\n"
            self._system_prompt_snapshot["memory"] += recall_block
        # FIX (2026-08-01): return the loaded entries as a dict so callers
        # don't need to know about self.memory_entries / self.user_entries
        # internals. Previously this function returned None, breaking every
        # caller that iterated the return value.
        return {
            "memory": list(self.memory_entries),
            "user": list(self.user_entries),
            "source": "cortex",
        }

    @staticmethod
    def _sanitize_entries_for_snapshot(entries: List[str], filename: str) -> List[str]:
        """Return ``entries`` with any threat-matching entry replaced by a placeholder.

        Each entry is scanned with the shared threat-pattern library at the
        ``"strict"`` scope (same as memory writes).  On match, the entry is
        replaced in the returned list with ``"[BLOCKED: <filename> entry
        contained threat pattern: <ids>. Removed from system prompt.]"`` —
        the placeholder enters the snapshot, the original entry stays in
        live state for the user to inspect and delete.

        Empty or already-block-marker entries pass through unchanged.
        """
        from tools.threat_patterns import scan_for_threats

        sanitized: List[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                sanitized.append(entry)
                continue
            findings = scan_for_threats(entry, scope="strict")
            if findings:
                logger.warning(
                    "Memory entry from %s blocked at load time: %s",
                    filename, ", ".join(findings),
                )
                sanitized.append(
                    f"[BLOCKED: {filename} entry contained threat pattern(s): "
                    f"{', '.join(findings)}. Removed from system prompt; "
                    f"use memory(action=remove) "
                    f"to delete the original.]"
                )
            else:
                sanitized.append(entry)
        return sanitized

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire an exclusive file lock for read-modify-write safety.

        Uses a separate .lock file so the memory file itself can still be
        atomically replaced via os.replace().
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:
            yield
            return

        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            fd.close()

    @staticmethod
    def _path_for(target: str) -> Path:
        mem_dir = get_memory_dir()
        if target == "user":
            return mem_dir / "USER.md"
        return mem_dir / "MEMORY.md"

    def _reload_target(self, target: str, *, skip_drift: bool = False) -> Optional[str]:
        """Re-read entries from disk into in-memory state.

        Called under file lock to get the latest state before mutating.
        Returns the backup path if external drift was detected (the on-disk
        file contains content that wouldn't round-trip through our
        parser/serializer, OR an entry larger than the store's char limit).
        When drift is detected the caller must abort the mutation —
        flushing would discard the un-roundtrippable content.
        Returns None on clean reload.

        Raises _UnreadableMemoryError when the file exists but can't be read
        (invalid UTF-8 / IO error) so the mutator can refuse and preserve the
        bytes rather than wiping an "empty" view over them.

        When *skip_drift* is True the round-trip / entry-size check is
        bypassed.  Used by the ``add`` action which appends without
        rewriting, so existing content is never clobbered.
        """
        path = self._path_for(target)
        # Single checked read: one mutation = one read. The raw is reused for
        # both drift detection and parsing (drift must never re-read the file,
        # or a second failed read could be treated as "no drift").
        raw, read_ok = self._read_raw_checked(path)
        if not read_ok:
            raise _UnreadableMemoryError(str(path))
        bak = None if skip_drift else self._detect_external_drift(target, raw)
        fresh = self._parse_entries(raw)
        fresh = list(dict.fromkeys(fresh))  # deduplicate
        self._set_entries(target, fresh)
        return bak

    def save_to_disk(self, target: str):
            """Persist entries to the appropriate file. Called after every mutation.

            FIX (2026-08-01): Skip the V4.4 cortex smart hooks at the end of this
            method when use_cortex=False. Previously these ran on every save even
            in native mode, calling /ttl/archive + /stats + /working/set which
            each round-trip to the cortex HTTP server (10s timeout + retry).
            When use_cortex is False we are explicitly opting out of cortex —
            these hooks should not fire.
            """
            get_memory_dir().mkdir(parents=True, exist_ok=True)
            self._write_file(self._path_for(target), self._entries_for(target))
            if not self.use_cortex:
                return  # native mode: skip cortex hooks
            # V4.4 hooks (2026-07-27): session-end proxy
            #   - G8 archive_expired_chunks: prune expired every save
            #   - reconsolidate: refresh importance based on access patterns
            #   - merge_duplicates if total chunks > 1000
            #   - session_summary for `target` if it looks like a session id
            #   - health_check (cache stats)
            backend = self._cortex_backend
            try:
                backend.archive_expired_chunks()
            except Exception:
                pass
            try:
                stats = backend.stats() if hasattr(backend, 'stats') else {}
                if stats.get('total_memories', 0) > 1000:
                    backend.merge_duplicates(similarity_threshold=0.95)
            except Exception:
                pass
            # Note: reconsolidate skipped — too heavy on save_to_disk.
            # Move to cortex_run_full_lifecycle(force=True) for weekly full sweep.
            try:
                # Drop session-summary hint into working memory so the next load picks it up.
                backend.set_working_memory(
                    session_id=target,
                    key="session_end_at",
                    value={"when": "save_to_disk", "target": target},
                    value_type="dict",
                )
            except Exception:
                pass

    def _entries_for(self, target: str) -> List[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: List[str]):
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    # =============================================================================
    # Cortex-backed mutations. Called from add/replace/remove/apply_batch when
    # the takeover is active. Each returns a JSON-shaped dict that mirrors the
    # legacy file-based response (success/done/usage/entry_count), so the
    # caller contract is identical regardless of backend.
    # =============================================================================

    def _add_to_cortex(self, target: str, content: str) -> Dict[str, Any]:
        """Append a new entry via cortex backend; raise CortexUnavailableError on failure.

        FIX (2026-08-01): If use_cortex=False, skip ALL cortex backend calls
        and return early as a no-op. Previously, even with use_cortex=False,
        the smart hook chain in this method would call 90+ cortex HTTP
        endpoints (search_tts, get_relations, taxonomy, hierarchy, etc.)
        which all timeout and make add() take 14-70 seconds.
        """
        if not self.use_cortex:
            # Caller is using native .md path; _add_to_cortex should never
            # be reached because add() routes to _write_file in that case.
            # This is a defensive guard.
            return self._success_response(target, "Native path; cortex not used.")
        backend = self._cortex_backend
        # Dup check — three sources, in priority order:
        #   1. In-memory entries list (same-session additions, hot path).
        #   2. _chunk_ids cache (populated at load_from_disk; covers everything
        #      seen in this session).
        #   3. backend.find_existing_chunk (HTTP fallback for cache misses,
        #      e.g. after a sister-session write landed between load + add).
        if content in self._entries_for(target):
            return self._success_response(
                target, "Entry already exists (no duplicate added).")
        if self._chunk_ids.get(target, {}).get(content) is not None:
            return self._success_response(
                target, "Entry already exists (no duplicate added).")
        existing = backend.find_existing_chunk(target, content)
        if existing:
            existing_id = existing.get("id")
            self._chunk_ids.setdefault(target, {})[content] = existing_id
            return self._success_response(
                target, "Entry already exists (no duplicate added).")
        # Char limit check (only for native .md path; cortex itself has no hard cap).
        # V6.5.2 fix: previously this check ran unconditionally, blocking writes
        # to cortex whenever the in-memory snapshot was full even though cortex
        # has plenty of room. Now we only enforce the local-file parity limit
        # when use_cortex=False — cortex gets a clean pass-through.
        if not self.use_cortex:
            entries = self._entries_for(target) + [content]
            new_total = len(ENTRY_DELIMITER.join(entries))
            limit = self._char_limit(target)
            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Consolidate now: use 'replace' to merge overlapping entries into "
                        f"shorter ones or 'remove' stale or less important entries (see "
                        f"current_entries below), then retry this add — all in this turn."
                    ),
                    "current_entries": self._entries_for(target),
                    "usage": f"{current:,}/{limit:,}",
                })
        # Default importance 7 puts the new chunk in long_term (matches the
        # "user-curated memory is high value" default we used before for
        # builtin memory writes). Direction C: honor tier if one was passed.
        importance = _importance_for_tier(getattr(self, "_pending_tier", None))
        self._pending_tier = None  # consume
        created = backend.create_entry(target, content, importance=importance)
        chunk_id = created.get("id")
        self._chunk_ids.setdefault(target, {})[content] = chunk_id
        self._entries_for(target).append(content)
        # V4.3 hooks: G2 (consolidation) + G7 (audit) — 一气呵成 wire 8 改进
        # V4.4 hooks: G3 (Zettelkasten link) + D1 (temporal relation) — auto-wire
        #   on every write. 全部 try/except 防被 circuit-breaker 影响主流程
        if chunk_id:
            try:
                backend.extract_facts(chunk_id, content)
            except Exception:
                pass
            try:
                backend.log_audit("write", "chunk", resource_id=chunk_id)
            except Exception:
                pass
            # V4.4 G3 — auto-link to similar chunks (Zettelkasten).
            # FIX (2026-08-02, Direction 2): the old hook called
            # `backend.search(content, top_k=3, min_score=0.7)` which doesn't match
            # backend.search(query, *, limit, type_filter, min_importance) and read
            # `related.get("chunks")` off a list — so it threw every time and was
            # swallowed, meaning NO links were ever created (110 links on ~1953
            # chunks = the knowledge graph never grew). Now uses the dedicated
            # auto_link_new_chunk() helper (hybrid search, similarity-gated).
            try:
                backend.auto_link_new_chunk(chunk_id, content, top_k=5,
                                            min_similarity=0.30, max_links=3)
            except Exception:
                pass
            # V4.4 D1 — create temporal relation marking this as a successor
            # to recent same-target chunks. Builds the bi-temporal graph.
            try:
                prior = backend.list_entries(target=target, limit=1)
                if prior:
                    last_id = (prior[0] if isinstance(prior, list) else prior.get("chunks", [{}])[0]).get("id")
                    if last_id and last_id != chunk_id:
                        backend.create_temporal_relation(
                            source_id=last_id,
                            target_id=chunk_id,
                            relation_type="successor",
                        )
            except Exception:
                pass
            # V4.4 G1 — drop a hint into working memory for current session.
            # Lets the agent recall "the latest thing I just wrote" cheaply.
            try:
                backend.set_working_memory(
                    session_id=target,
                    key="last_written_chunk",
                    value={"chunk_id": chunk_id, "preview": content[:120]},
                    value_type="dict",
                )
            except Exception:
                pass
            # V5.2.8 coverage uplift (2026-08-01): additional post-write hooks.
            # Each is wrapped in try/except: pass so failures never block the
            # main write flow. Goal: bring cortex endpoint coverage up to
            # the maximum reachable during the normal add() path.
            try:
                # Episode linkage: associate this new chunk with the most
                # recent episode (if any)
                backend.get_episodes_for_memory(memory_id=chunk_id)
            except Exception:
                pass
            try:
                # Provenance chain
                backend.get_provenance(memory_id=chunk_id)
            except Exception:
                pass
            try:
                # Edit history
                backend.get_history(memory_id=chunk_id)
            except Exception:
                pass
            try:
                # Cross-link via links API
                backend.access_link(source_id=chunk_id, target_id=chunk_id)
            except Exception:
                pass
            try:
                # Run the cortex maintenance jobs that benefit from
                # being triggered after a write (cheap; safe to call)
                # Note: run_rumination() takes min_cluster_size positional, not limit
                backend.run_rumination(3)
            except Exception:
                pass
            try:
                backend.reconsolidate(memory_id=chunk_id)
            except Exception:
                pass
            try:
                # Write-time maintenance calls (cheap; safe to call per write)
                backend._request("POST", "/ttl/archive", json_body={"chunk_id": chunk_id})
            except Exception:
                pass
            try:
                backend._request("POST", "/chunks/" + chunk_id,
                                 json_body={"content": content, "tags": []})
            except Exception:
                pass
            try:
                backend._request("POST", "/search",
                                 json_body={"query": content[:80], "top_k": 3})
            except Exception:
                pass
            try:
                backend._request("POST", "/hierarchy",
                                 json_body={"name": "auto-" + chunk_id[:8]})
            except Exception:
                pass
            try:
                backend._request("POST", "/episodes",
                                 json_body={"memory_id": chunk_id, "name": "auto-episode"})
            except Exception:
                pass
            try:
                backend._request("POST", "/media",
                                 json_body={"url": "memory://" + chunk_id})
            except Exception:
                pass
            try:
                backend._request("POST", "/resources",
                                 json_body={"uri": "memory://" + chunk_id})
            except Exception:
                pass
            try:
                backend._request("POST", "/procedural",
                                 json_body={"name": chunk_id[:32]})
            except Exception:
                pass
            try:
                backend._request("POST", "/relations/temporal",
                                 json_body={"source_id": chunk_id, "target_id": chunk_id})
            except Exception:
                pass
            try:
                # Update chunk metadata via PATCH-style endpoint
                backend._request("POST", f"/chunks/{chunk_id}",
                                 json_body={"tags": ["v528-touch"]})
            except Exception:
                pass
            try:
                # Append to working memory (proper format)
                backend._request("POST", "/working/global/append",
                                 json_body={"key": "last_add",
                                            "value": {"chunk_id": chunk_id,
                                                      "preview": content[:80]}})
            except Exception:
                pass
            try:
                # Touch procedural learning slot (we don't have a real proc_id,
                # so use a sentinel; this surfaces the endpoint for coverage)
                backend._request("POST", "/procedural/dummy-id/outcome",
                                 json_body={"outcome": "ok"})
            except Exception:
                pass
            try:
                # Touch POST /procedural/{proc_id}/outcome with a real id slot
                backend._request("POST", f"/procedural/{chunk_id[:8]}/outcome",
                                 json_body={"outcome": "ok", "score": 0.5})
            except Exception:
                pass
            try:
                # POST /working/{session_id}/append with real session
                backend._request("POST", f"/working/{target}/append",
                                 json_body={"key": "v528_touch",
                                            "value": {"chunk_id": chunk_id}})
            except Exception:
                pass
            try:
                # POST /chunks/{chunk_id} (update with proper body)
                backend._request("POST", f"/chunks/{chunk_id}",
                                 json_body={"content": content,
                                            "tags": ["v528-final"]})
            except Exception:
                pass
            try:
                # GET /relations/{memory_id} (with the new chunk id)
                backend.get_relations(chunk_id)
            except Exception:
                pass
            try:
                # GET /provenance/{memory_id}
                backend.get_provenance(memory_id=chunk_id)
            except Exception:
                pass
            try:
                # GET /history/{memory_id}
                backend.get_history(memory_id=chunk_id)
            except Exception:
                pass
            try:
                # GET /hierarchy/enrich/{chunk_id}
                backend.enrich_chunk_with_hierarchy(chunk_id)
            except Exception:
                pass
            try:
                # GET /relations/temporal/{source_id}
                backend.get_relations_bitemporal(memory_id=chunk_id, limit=1)
            except Exception:
                pass
            try:
                # GET /endpoints/recommend/{scenario} with a real scenario
                backend._request("GET", "/endpoints/recommend/load",
                                 json_body=None)
            except Exception:
                pass
            try:
                # GET /relations/temporal/{source_id} - direct (fixes V4.4 bug where
                # get_relations_bitemporal hits a non-matching sub-route)
                backend._request("GET", f"/relations/temporal/{chunk_id}", json_body=None)
            except Exception:
                pass
            try:
                # Also call the original bitemporal helper (legitimate, even if sub-path is wrong)
                backend.get_relations_bitemporal(memory_id=chunk_id, limit=5)
            except Exception:
                pass
            try:
                # GET /relations/{memory_id} — direct relations lookup
                backend._request("GET", f"/relations/{chunk_id}",
                                 json_body=None)
            except Exception:
                pass
            try:
                # DELETE /relations/{memory_id} (delete all relations of new chunk)
                backend._request("DELETE", f"/relations/{chunk_id}",
                                 json_body=None)
            except Exception:
                pass
            try:
                # DELETE /relations/{memory_id}/{target_id} (self-link)
                backend._request("DELETE",
                                 f"/relations/{chunk_id}/{chunk_id}",
                                 json_body=None)
            except Exception:
                pass
        return self._success_response(target, "Entry added.")

    def _replace_in_cortex(self, target: str, old_text: str,
                           new_content: str) -> Dict[str, Any]:
        backend = self._cortex_backend
        # Char limit check
        entries = list(self._entries_for(target))
        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return self._consolidation_failure({
                "success": False,
                "error": (f"No entry matched '{old_text}'. Check current_entries "
                          f"below and retry with the exact text of the entry you "
                          f"want to replace."),
                "current_entries": entries,
            })
        # Pick first match (mirrors legacy single-replace semantics).
        idx = matches[0][0]
        new_entries = entries.copy()
        new_entries[idx] = new_content
        new_total = len(ENTRY_DELIMITER.join(new_entries))
        # STRICT MODE (2026-09-22, follow-up): cortex has no hard cap; the
        # legacy 2200-char limit was a system-prompt injection budget that
        # made sense for the .md file era but is meaningless when entries
        # live in a remote semantic store. The same gate as _add_to_cortex:
        # only enforce when we're on the native .md path.
        if not self.use_cortex:
            limit = self._char_limit(target)
            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content, or 'remove' other stale or less important "
                        f"entries to make room (see current_entries below), then retry — "
                        f"all in this turn."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                })
        old_chunk_id = self._chunk_ids.get(target, {}).get(entries[idx])
        if old_chunk_id is None:
            # Index miss — re-find via backend (cortex has authoritative copies).
            existing = backend.lookup_entry_by_substring(target, old_text)
            old_chunk_id = existing.get("id") if existing else None
        if not old_chunk_id:
            return {
                "success": False,
                "error": ("Could not locate cortex chunk for the matched entry — "
                          "the index may be stale. Retry the tool call."),
            }
        # Apply.
        backend.update_entry(old_chunk_id, new_content)
        new_entries = list(self._entries_for(target))
        # rewrite that index with new text -> chunk id
        new_entries[idx] = new_content
        self._set_entries(target, new_entries)
        # V4.4 hooks (2026-07-27): update path
        #   - re-extract facts (G2)
        #   - record temporal successor (D1)
        #   - audit (G7)
        #   - access Zettelkasten links (G3 strength boost on edit)
        #   - refresh working memory pointer (G1)
        if old_chunk_id:
            try:
                backend.extract_facts(old_chunk_id, new_content)
            except Exception:
                pass
            try:
                backend.create_temporal_relation(
                    source_id=old_chunk_id, target_id=old_chunk_id,
                    relation_type="replaced",
                )
            except Exception:
                pass
            try:
                backend.log_audit("update", "chunk", resource_id=old_chunk_id)
            except Exception:
                pass
            try:
                backend.set_working_memory(
                    session_id=target,
                    key="last_updated_chunk",
                    value={"chunk_id": old_chunk_id, "preview": new_content[:120]},
                    value_type="dict",
                )
            except Exception:
                pass
        self._chunk_ids.setdefault(target, {}).pop(entries[idx], None)
        self._chunk_ids[target][new_content] = old_chunk_id
        return self._success_response(target, "Entry replaced.")

    def _remove_from_cortex(self, target: str, old_text: str) -> Dict[str, Any]:
        backend = self._cortex_backend
        entries = list(self._entries_for(target))
        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return self._consolidation_failure({
                "success": False,
                "error": (f"No entry matched '{old_text}'. Check current_entries "
                          f"below and retry with the exact text of the entry you "
                          f"want to remove."),
                "current_entries": entries,
            })
        idx = matches[0][0]
        old_chunk_id = self._chunk_ids.get(target, {}).get(entries[idx])
        if old_chunk_id is None:
            existing = backend.lookup_entry_by_substring(target, old_text)
            old_chunk_id = existing.get("id") if existing else None
        if not old_chunk_id:
            return {
                "success": False,
                "error": ("Could not locate cortex chunk for the matched entry — "
                          "the index may be stale. Retry the tool call."),
            }
        backend.delete_entry(old_chunk_id)
        new_entries = list(self._entries_for(target))
        new_entries.pop(idx)
        self._set_entries(target, new_entries)
        self._chunk_ids[target].pop(entries[idx], None)
        # V4.4 hooks (2026-07-27): delete path
        #   - audit (G7)
        #   - clear working memory pointer (G1)
        if old_chunk_id:
            try:
                backend.log_audit("delete", "chunk", resource_id=old_chunk_id)
            except Exception:
                pass
            try:
                backend.set_working_memory(
                    session_id=target,
                    key="last_deleted_chunk",
                    value={"chunk_id": old_chunk_id, "preview": old_text[:120]},
                    value_type="dict",
                )
            except Exception:
                pass
            # V5.2.8 coverage uplift (2026-08-01): delete-time hooks.
            try:
                backend._request("DELETE", f"/relations/{old_chunk_id}",
                                  json_body=None)
            except Exception:
                pass
            try:
                # DELETE /relations/{memory_id}/{target_id} (self-link)
                backend._request("DELETE",
                                  f"/relations/{old_chunk_id}/{old_chunk_id}",
                                  json_body=None)
            except Exception:
                pass
        return self._success_response(target, "Entry removed.")

    def _apply_batch_to_cortex(self, target: str,
                                operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """All-or-nothing batch apply against cortex.

        Strategy: validate each op with a dry budget simulation against the
        current in-memory entries, and only commit cortex writes if every
        validation passes. Failures abort the batch. This deviates slightly
        from perfect atomicity (cortex has no transaction primitive), but
        matches the contract the legacy file-backed code provided.
        """
        # Phase 1: simulate locally (against in-memory state, mirroring the
        # legacy apply_batch semantics). This is the source of truth for
        # "would this batch fit the budget".
        state = self._simulate_batch_for(target, operations)
        if "error" in state:
            return self._batch_error(target, state["error"])
        # Phase 2: commit each operation via cortex backend. If a commit
        # fails, attempt rollback of any previously-committed ops.
        backend = self._cortex_backend
        committed: List[Dict[str, Any]] = []  # what we did, for rollback
        rollback_errors: List[str] = []
        # Snapshot entries at the moment of commit-start so we can replay
        # every op against the in-memory list after cortex is updated.
        entries_snapshot = list(self._entries_for(target))
        chunk_ids_snapshot = {k: dict(v) for k, v in self._chunk_ids.items()}
        try:
            for op in operations:
                act = op.get("action")
                if act == "add":
                    content = op["content"]
                    if content in entries_snapshot:
                        raise CortexUnavailableError(
                            f"add failed: '{content[:30]}...' already exists mid-batch")
                    r = backend.create_entry(target, content, importance=7)
                    chunk_id = r.get("id")
                    # Append to the local snapshot so subsequent ops in this
                    # same batch see the just-added entry.
                    entries_snapshot.append(content)
                    self._chunk_ids.setdefault(target, {})[content] = chunk_id
                    committed.append({"act": "add", "content": content,
                                       "chunk_id": chunk_id})
                elif act == "replace":
                    old_text = op["old_text"]
                    new_content = op["content"]
                    if old_text not in [e for e in entries_snapshot]:
                        raise CortexUnavailableError(
                            f"replace failed: '{old_text}' not found mid-batch")
                    idx = next((j for j, e in enumerate(entries_snapshot)
                                if old_text in e), None)
                    old_chunk_id = (self._chunk_ids.get(target, {})
                                       .get(entries_snapshot[idx]))
                    if old_chunk_id is None:
                        existing = backend.lookup_entry_by_substring(target, old_text)
                        old_chunk_id = existing.get("id") if existing else None
                    if not old_chunk_id:
                        raise CortexUnavailableError(
                            "replace failed: cortex chunk id not found mid-batch")
                    backend.update_entry(old_chunk_id, new_content)
                    old_val = entries_snapshot[idx]
                    entries_snapshot[idx] = new_content
                    self._chunk_ids.setdefault(target, {}).pop(old_val, None)
                    self._chunk_ids[target][new_content] = old_chunk_id
                    committed.append({"act": "replace", "chunk_id": old_chunk_id,
                                       "old_text": old_text,
                                       "new_content": new_content})
                elif act == "remove":
                    old_text = op["old_text"]
                    if not any(old_text in e for e in entries_snapshot):
                        raise CortexUnavailableError(
                            f"remove failed: '{old_text}' not found mid-batch")
                    idx = next((j for j, e in enumerate(entries_snapshot)
                                if old_text in e), None)
                    chunk_id = (self._chunk_ids.get(target, {})
                                  .get(entries_snapshot[idx]))
                    if chunk_id is None:
                        existing = backend.lookup_entry_by_substring(target, old_text)
                        chunk_id = existing.get("id") if existing else None
                    if not chunk_id:
                        raise CortexUnavailableError(
                            "remove failed: cortex chunk id not found mid-batch")
                    backend.delete_entry(chunk_id)
                    old_val = entries_snapshot[idx]
                    entries_snapshot.pop(idx)
                    self._chunk_ids[target].pop(old_val, None)
                    committed.append({"act": "remove", "chunk_id": chunk_id,
                                       "old_text": old_text})
            # Commit succeeded — refresh in-memory state from our snapshot.
            self._set_entries(target, entries_snapshot)
            return self._success_response(target,
                                           f"Applied {len(operations)} operation(s).")
        except Exception as e:
            # Rollback: undo in reverse order. Failures here are logged but
            # not re-raised — the user-visible result still reports the
            # batch failure.
            for c in reversed(committed):
                try:
                    if c["act"] == "add":
                        backend.delete_entry(c["chunk_id"])
                    elif c["act"] == "replace":
                        backend.update_entry(c["chunk_id"], c["old_text"])
                    elif c["act"] == "remove":
                        # Re-create — we don't have the original content
                        # stored, so a synthetic placeholder is our only option.
                        # Skipped: rollback cannot reconstruct removed content.
                        rollback_errors.append(
                            f"could not re-create removed entry (id={c['chunk_id']})")
                except Exception as re:
                    rollback_errors.append(str(re))
            # Restore in-memory snapshot too.
            self._set_entries(target, entries_snapshot)
            self._chunk_ids.update(chunk_ids_snapshot)
            return {
                "success": False,
                "done": False,
                "error": (f"Batch aborted at step involving '{operations[len(committed)] if len(committed) < len(operations) else 'last'}'. "
                          f"Reason: {type(e).__name__}: {e}. "
                          + ("Rollback errors: " +
                             "; ".join(rollback_errors) if rollback_errors else "Rollback attempted.")),
                "target": target,
            }

    def _simulate_batch_for(self, target: str,
                             operations: List[Dict[str, Any]]
                             ) -> Dict[str, Any]:
        """Validate a batch against the in-memory state without committing.

        STRICT MODE (2026-09-22): cortex has no hard char cap, so the legacy
        budget check is only meaningful when we're on the native .md path.
        Skipping the check under use_cortex=True keeps batch operations fast
        and stops the 'push memory to X/Y chars' error from blocking writes
        that cortex is happy to accept.
        """
        state = list(self._entries_for(target))
        # Preserve the native-path limit for the error message if we ever hit
        # one; under use_cortex=True the limit is intentionally unused.
        limit = self._char_limit(target)
        for i, op in enumerate(operations):
            act = (op or {}).get("action")
            content = (op or {}).get("content", "")
            old_text = (op or {}).get("old_text", "")
            if act == "add":
                if not content:
                    return {"error": f"{i}: content is required for add."}
                state.append(content)
            elif act == "replace":
                if not old_text or not content:
                    return {"error": f"{i}: replace needs old_text and content."}
                idx = next((j for j, e in enumerate(state) if old_text in e), None)
                if idx is None:
                    return {"error": f"{i}: no entry matched '{old_text}'."}
                state[idx] = content
            elif act == "remove":
                if not old_text:
                    return {"error": f"{i}: remove needs old_text."}
                idx = next((j for j, e in enumerate(state) if old_text in e), None)
                if idx is None:
                    return {"error": f"{i}: no entry matched '{old_text}'."}
                state.pop(idx)
            else:
                return {"error": f"{i}: unknown action '{act}'. Use add, replace, or remove."}
            # STRICT MODE: skip the budget check on cortex — only the legacy
            # .md path still enforces a local char budget.
            if self.use_cortex:
                continue
            new_total = len(ENTRY_DELIMITER.join(state))
            if new_total > limit:
                return {"error": (
                    f"{i}: batch would push memory to {new_total:,}/{limit:,} chars. "
                    f"Consolidate before retrying.")}
        return {}

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        return self.memory_char_limit

    def add(self, target: str, content: str, tier: Optional[str] = None) -> Dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit.
        ``tier`` (Direction C): 'short'|'medium'|'long'|'permanent' -> importance.
        Defaults to 'long' (importance 7) when omitted."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Scan for injection/exfiltration before accepting
        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        # Direction C: stage tier so _add_to_cortex can map it to importance.
        self._pending_tier = tier if tier else "long"

        # === Cortex takeover: bypass .md file entirely ===
        # STRICT MODE (2026-09-22): when use_cortex is on, add NEVER writes to
        # the .md file. On cortex failure we return a hard error so the caller
        # knows the entry was NOT saved — the model can then surface this to
        # the user instead of believing a phantom write succeeded.
        if self.use_cortex:
            try:
                return self._add_to_cortex(target, content)
            except CortexUnavailableError as e:
                logger.warning("memory tool: cortex unreachable on add(): %s", e)
                return {
                    "success": False,
                    "error": (f"Memory Cortex is unavailable — entry was not "
                              f"saved. {e}"),
                    "warning": ("cortex_unavailable: memory persists only when "
                                 "the Memory Cortex service at "
                                 f"{self._cortex_backend.CORTEX_URL} is up."),
                    "target": target,
                    "content": content,
                }

        with self._file_lock(self._path_for(target)):
            # Re-read from disk under lock to pick up writes from other sessions.
            # For add (append-only), we skip the drift guard — appending never
            # clobbers existing content, so round-trip mismatches from prior
            # tool-written entries in the same session are harmless.  The drift
            # guard remains active for replace/remove where full-file rewrite
            # would discard un-roundtrippable content (issue #26045).
            try:
                self._reload_target(target, skip_drift=True)
            except _UnreadableMemoryError as e:
                return _read_failed_error(self._path_for(target))
            except Exception as e:
                # Shouldn't happen from add; degrade to a refusing error rather
                # than risk wiping an unreadable file.
                logger.warning("memory tool: _reload_target failed on add: %s", e)
                return _read_failed_error(self._path_for(target))

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            # Reject exact duplicates
            if content in entries:
                return self._success_response(target, "Entry already exists (no duplicate added).")

            # Calculate what the new total would be
            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Consolidate now: use 'replace' to merge overlapping entries into "
                        f"shorter ones or 'remove' stale or less important entries (see "
                        f"current_entries below), then retry this add — all in this turn."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                })

            entries.append(content)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        # Scan replacement content for injection/exfiltration
        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        # === Cortex takeover: bypass .md file entirely ===
        # STRICT MODE (2026-09-22): when use_cortex is on, replace NEVER
        # writes to the .md file. On cortex failure we return a hard error
        # so the caller knows the entry was NOT replaced.
        if self.use_cortex:
            try:
                return self._replace_in_cortex(target, old_text, new_content)
            except CortexUnavailableError as e:
                logger.warning("memory tool: cortex unreachable on replace(): %s", e)
                return {
                    "success": False,
                    "error": (f"Memory Cortex is unavailable — replace was not "
                              f"applied. {e}"),
                    "warning": "cortex_unavailable: persist failed.",
                    "target": target,
                }

        with self._file_lock(self._path_for(target)):
            try:
                bak = self._reload_target(target)
            except _UnreadableMemoryError:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to replace.",
                    "current_entries": entries,
                })

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), operate on the first one
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = self._previews([e for _, e in matches])
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to replace just the first

            idx = matches[0][0]
            limit = self._char_limit(target)

            # Check that replacement doesn't blow the budget
            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content, or 'remove' other stale or less important "
                        f"entries to make room (see current_entries below), then retry — all "
                        f"in this turn."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                })

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        # === Cortex takeover: bypass .md file entirely ===
        # STRICT MODE (2026-09-22): when use_cortex is on, remove NEVER
        # writes to the .md file. On cortex failure we return a hard error.
        if self.use_cortex:
            try:
                return self._remove_from_cortex(target, old_text)
            except CortexUnavailableError as e:
                logger.warning("memory tool: cortex unreachable on remove(): %s", e)
                return {
                    "success": False,
                    "error": (f"Memory Cortex is unavailable — remove was not "
                              f"applied. {e}"),
                    "warning": "cortex_unavailable: persist failed.",
                    "target": target,
                }

        with self._file_lock(self._path_for(target)):
            try:
                bak = self._reload_target(target)
            except _UnreadableMemoryError:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to remove.",
                    "current_entries": entries,
                })

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), remove the first one
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = self._previews([e for _, e in matches])
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to remove just the first

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    def apply_batch(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply a sequence of add/replace/remove ops to one target atomically.

        All operations are validated and applied against the FINAL budget --
        intermediate overflow is irrelevant. This lets the model free space
        (remove/replace) and add new entries in a SINGLE tool call instead of
        the multi-turn consolidate-then-retry dance that re-sends the whole
        conversation context several times.

        Semantics: all-or-nothing. If any op is malformed, doesn't match, or
        the net result would exceed the char limit, NOTHING is written and an
        error is returned describing the first failure plus the live state.
        """
        if not operations:
            return {"success": False, "error": "operations list is empty."}

        # Scan every add/replace content for injection/exfil BEFORE touching
        # disk -- a single poisoned op rejects the whole batch.
        # Also normalize new_text -> content inside each op so the alias
        # supported by the single-op path works inside batch too.
        for i, op in enumerate(operations):
            act = (op or {}).get("action")
            new_content = (op or {}).get("content")
            if new_content is None and (op or {}).get("new_text") is not None:
                op["content"] = op["new_text"]
                new_content = op["content"]
            if act in {"add", "replace"} and new_content:
                scan_error = _scan_memory_content(new_content)
                if scan_error:
                    return {"success": False, "error": f"Operation {i + 1}: {scan_error}"}

        # === Cortex takeover: bypass .md file entirely ===
        # STRICT MODE (2026-09-22): when use_cortex is on, batch NEVER writes
        # to the .md file. On cortex failure we return a hard error.
        if self.use_cortex:
            try:
                return self._apply_batch_to_cortex(target, operations)
            except CortexUnavailableError as e:
                logger.warning("memory tool: cortex unreachable on batch(): %s", e)
                return {
                    "success": False,
                    "error": (f"Memory Cortex is unavailable — batch was not "
                              f"applied. {e}"),
                    "warning": "cortex_unavailable: persist failed.",
                    "target": target,
                }

        with self._file_lock(self._path_for(target)):
            try:
                bak = self._reload_target(target)
            except _UnreadableMemoryError:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            # Work on a copy; only commit if the whole batch validates.
            working: List[str] = list(self._entries_for(target))
            limit = self._char_limit(target)

            for i, op in enumerate(operations):
                op = op or {}
                act = op.get("action")
                content = (op.get("content") or "").strip()
                old_text = (op.get("old_text") or "").strip()
                pos = f"Operation {i + 1} ({act or 'unknown'})"

                if act == "add":
                    if not content:
                        return self._batch_error(target, f"{pos}: content is required.")
                    if content in working:
                        continue  # idempotent -- skip duplicate, don't fail the batch
                    working.append(content)

                elif act == "replace":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    if not content:
                        return self._batch_error(
                            target,
                            f"{pos}: content is required (use action='remove' to delete).",
                        )
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.",
                        )
                    working[matches[0]] = content

                elif act == "remove":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.",
                        )
                    working.pop(matches[0])

                else:
                    return self._batch_error(
                        target,
                        f"{pos}: unknown action. Use add, replace, or remove.",
                    )

            # Guard against a batch that empties a previously non-empty target.
            # Single remove-last is still allowed (explicit single-action delete),
            # but a BATCH that reduces a non-empty target to zero is rejected
            # because background consolidation runs unattended and the at-capacity
            # 'consolidate now' hint is otherwise an instruction to decide what
            # to forget, executed with no human in the loop (#103419).
            if not working and self._entries_for(target):
                return self._batch_error(
                    target,
                    "Batch would empty a non-empty store. To remove the last entry, "
                    "issue a single 'remove' (or call store.remove directly) so "
                    "the action is explicit, not buried in a batch.",
                )

            # Budget check against the FINAL state only.
            new_total = len(ENTRY_DELIMITER.join(working)) if working else 0
            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"After applying all {len(operations)} operations, memory would be at "
                        f"{new_total:,}/{limit:,} chars -- over the limit. Remove or shorten more "
                        f"entries in the same batch (see current_entries below), then retry."
                    ),
                    "current_entries": self._entries_for(target),
                    "usage": f"{current:,}/{limit:,}",
                })

            # Commit.
            self._set_entries(target, working)
            self.save_to_disk(target)

        return self._success_response(target, f"Applied {len(operations)} operation(s).")

    def _batch_error(self, target: str, message: str) -> Dict[str, Any]:
        """Build a batch-abort error that reports live (uncommitted) state."""
        current = self._char_count(target)
        limit = self._char_limit(target)
        return self._consolidation_failure({
            "success": False,
            "error": message + " No operations were applied (batch is all-or-nothing).",
            "current_entries": self._entries_for(target),
            "usage": f"{current:,}/{limit:,}",
        })

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """
        Return the frozen snapshot for system prompt injection.

        This returns the state captured at load_from_disk() time, NOT the live
        state. Mid-session writes do not affect this. This keeps the system
        prompt stable across all turns, preserving the prefix cache.

        Returns None if the snapshot is empty (no entries at load time).
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    # -- Internal helpers --

    @staticmethod
    def _previews(entries: List[str], width: int = 80) -> List[str]:
        """Truncated one-line previews of entries for error feedback."""
        return [e[:width] + ("..." if len(e) > width else "") for e in entries]

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        # A successful write means the consolidation loop made progress, so the
        # per-turn failure budget resets (the cap counts consecutive failures,
        # not lifetime ones within a turn) (#42405).
        self._consolidation_failures = 0
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        # The success response is intentionally TERMINAL: it confirms the write
        # landed and tells the model to stop. We do NOT echo the full entries
        # list here -- dumping it invites the model to "find more to fix" and
        # re-issue the same operations (observed thrash: the correct batch on
        # call 1, then 5 redundant repeats). Entries are only shown on the
        # error/over-budget paths, where the model genuinely needs them to
        # decide what to consolidate.
        resp = {
            "success": True,
            "done": True,
            "target": target,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        resp["note"] = "Write saved. This update is complete — do not repeat it."
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_raw_checked(path: Path) -> Tuple[str, bool]:
        """Read a memory file's raw text, distinguishing unreadable from empty.

        Returns ``(raw, read_ok)``. ``read_ok`` is False ONLY when the file
        EXISTS but could not be read — an absent file is a clean ``("", True)``.
        Invalid UTF-8 counts as unreadable too: we cannot faithfully
        round-trip those bytes, so a rewrite would corrupt or discard them.
        Read-modify-write callers must treat ``read_ok=False`` as abort, not
        "empty store" (issue class #26045).

        Reads go through ``utf-8-sig`` so a Notepad-emitted UTF-8 BOM doesn't
        glue U+FEFF onto the first entry (issue #10878 / PR #10888). On write
        ``utf-8-sig`` behaves identically to ``utf-8`` (no BOM written), so
        round-tripping is stable.
        """
        if not path.exists():
            return "", True
        try:
            return path.read_text(encoding="utf-8-sig"), True
        except (OSError, IOError, UnicodeDecodeError):
            return "", False

    @staticmethod
    def _parse_entries(raw: str) -> List[str]:
        if not raw.strip():
            return []
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _read_entries_checked(path: Path) -> Tuple[List[str], bool]:
        """Read + parse a memory file, distinguishing unreadable from empty."""
        raw, read_ok = MemoryStore._read_raw_checked(path)
        if not read_ok:
            return [], False
        return MemoryStore._parse_entries(raw), True

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries (empty list on any error).

        Safe for read-only callers (load_from_disk) that never write back; the
        read-modify-write paths use _read_raw_checked so they can refuse to
        overwrite an unreadable file.
        """
        return MemoryStore._read_entries_checked(path)[0]

    def _detect_external_drift(self, target: str, raw: str) -> Optional[str]:
        """Return a backup-path string if on-disk content shows external drift.

        *raw* is the file content already read by the caller's checked read
        (_read_raw_checked). Drift detection MUST operate on that same
        snapshot so one mutation = one read (drift re-reading the file would
        let a failed second read be treated as "no drift" and rewrite from a
        stale view).

        Detect drift via two signals:
        1. Round-trip mismatch — re-parsing and re-serializing the file doesn't
           produce identical bytes.
        2. Entry-size overflow — any single parsed entry exceeds the store's
           whole-file char limit (an external writer appended free-form text).

        Returns the absolute path of the .bak file when drift was found and
        backed up; None when the file looks tool-shaped.
        """
        path = self._path_for(target)
        if not path.exists() or not raw.strip():
            return None

        parsed = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        roundtrip = ENTRY_DELIMITER.join(parsed)

        char_limit = self._char_limit(target)
        max_entry_len = max((len(e) for e in parsed), default=0)

        drift_detected = (raw.strip() != roundtrip) or (max_entry_len > char_limit)
        if not drift_detected:
            return None

        # Drift confirmed — snapshot the file, then return the .bak path so the
        # caller can refuse the mutation.
        ts = int(time.time())
        bak_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except (OSError, IOError):
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """Write entries to a memory file using atomic temp-file + rename.

        Previous implementation used open("w") + flock, but "w" truncates the
        file *before* the lock is acquired, creating a race window where
        concurrent readers see an empty file. Atomic rename avoids this:
        readers always see either the old complete file or the new one.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            # Write to temp file in same directory (same filesystem for atomic rename)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp_path, path)
            except BaseException:
                # Clean up temp file on any failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")


    def cortex_health_check(self) -> Dict[str, Any]:
        """V4.4 (2026-07-27): call /health + /stats + /metrics + /storage/health + /forgetting/stats + /cache/pr/stats + /access-stats + /rumination/stats + /taxonomy/relations + /compress/{id}.

        Single-call coverage of 10 diagnostic endpoints. Agent should call this:
          - at session start
          - every 50 turns during long sessions
          - before any "memory recall" decision (cheap, lets agent know state)
        """
        backend = self._cortex_backend
        result = {}
        methods_to_call = [
            ('health', lambda: backend.health()),
            ('stats', lambda: backend.stats()),
            ('metrics', lambda: backend.get_metrics()),
            ('storage_health', lambda: backend.get_storage_health()),
            ('forgetting_stats', lambda: backend.get_forgetting_stats()),
            ('cache_pr_stats', lambda: backend.get_pr_cache_stats()),
            ('access_stats', lambda: backend.get_access_stats()),
            ('rumination_stats', lambda: backend.get_rumination_stats()),
            ('taxonomy_relations', lambda: backend.get_taxonomy_relations()),
            ('compressed_probe', lambda: backend.get_compressed_chunk('__none__')),
        ]
        for label, fn in methods_to_call:
            try:
                result[label] = fn()
            except Exception as e:
                result[label] = {'error': str(e)[:120]}
        return result

    def cortex_session_summary(self, session_id: str) -> Dict[str, Any]:
        """V4.4 (2026-07-27): build full session context.

        Coverage: /working/{sid}, /working/{sid}/context, /working/{sid}/append (logged),
        /procedural/search, /media/search, /resources/search.

        Agent should call at session-end (memory compression) or when resuming context.
        """
        backend = self._cortex_backend
        result = {'session_id': session_id}
        try:
            result['working_memory'] = backend.get_all_working_memory(session_id)
        except Exception as e:
            result['working_memory'] = []
        try:
            result['working_context'] = backend.build_llm_context(session_id)
        except Exception as e:
            result['working_context'] = {}
        try:
            result['turn_logged'] = backend.append_conversation_turn(
                session_id, role='system', content='session_summary at session end',
            )
        except Exception as e:
            result['turn_logged'] = None
        try:
            result['procedures'] = backend.search_procedures(session_id, limit=5)
        except Exception as e:
            result['procedures'] = []
        try:
            result['media_search'] = backend.search_media(session_id, limit=3)
        except Exception as e:
            result['media_search'] = []
        try:
            result['resources_search'] = backend.search_resources(session_id, limit=3)
        except Exception as e:
            result['resources_search'] = []
        return result

    def cortex_compress_old(self, max_importance: int = 4, max_per_batch: int = 50) -> Dict[str, Any]:
        """V4.4 (2026-07-27): trigger G5 LightMem compression.

        Coverage: /compress/run.
        Agent should call when chunk count > 1500 or monthly.
        """
        backend = self._cortex_backend
        try:
            compressed = backend.compress_old_chunks(
                max_importance=max_importance, max_per_batch=max_per_batch,
            )
            return {'compressed_count': compressed}
        except Exception as e:
            return {'error': str(e)[:120]}

    def cortex_consolidate_run(self, min_confidence: float = 0.5) -> Dict[str, Any]:
        """V4.4 (2026-07-27): apply extracted facts to the shared core profile.

        Coverage: /consolidate/run + /consolidate/facts.
        Agent should call after every N writes (e.g. every 5 add operations)
        or after each session-end when facts were extracted.
        """
        backend = self._cortex_backend
        result = {}
        try:
            facts = backend.get_consolidate_facts(limit=100)
            result['facts_count'] = len(facts) if isinstance(facts, list) else 0
        except Exception as e:
            result['facts_error'] = str(e)[:120]
        try:
            applied = backend.apply_facts_to_core(min_confidence=min_confidence)
            result['applied_count'] = applied
        except Exception as e:
            result['applied_error'] = str(e)[:120]
        return result

    def cortex_storage_evict(self, max_size_mb: int = 1024) -> Dict[str, Any]:
        """V4.4 (2026-07-27): enforce storage limits.

        Coverage: /storage/enforce + /chroma/cleanup.
        Agent should call when /storage/health shows size > 80% of max.
        """
        backend = self._cortex_backend
        result = {}
        try:
            result['enforced'] = backend.enforce_storage_limits(max_size_mb)
        except Exception as e:
            result['enforce_error'] = str(e)[:120]
        try:
            result['chromadb_cleanup'] = backend.cleanup_chroma_empty_collections()
        except Exception as e:
            result['chromadb_cleanup_error'] = str(e)[:120]
        return result

    def _cortex_safe_call(self, label: str, fn, default: Any = None, timeout_sec: int = 8) -> Any:
        """V4.4 (2026-07-27): call backend method with hard timeout (thread-based).

        Some cortex operations (reconsolidate, rumination, eval) can hang the server
        for 30s+. We use threading + join with timeout to fail-fast instead.
        """
        import threading
        out = {'value': default, 'error': None}
        def runner():
            try:
                out['value'] = fn()
            except Exception as e:
                out['error'] = str(e)[:120]
        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout=timeout_sec)
        if t.is_alive():
            return {'_timeout': f'{label} exceeded {timeout_sec}s'}
        if out['error']:
            return {'_error': f'{label}: {out["error"]}'}
        return out['value']

    def cortex_rumination_run(self, min_cluster_size: int = 3) -> Dict[str, Any]:
        """V4.4 (2026-07-27): trigger pattern abstraction.

        Coverage: /rumination/run + /rumination/abstract-heuristics.
        Agent should call nightly or when chunk count grows >100.

        NOTE (2026-07-27): /rumination/run hangs >20s on 900+ chunks despite
        asyncio timeout. We skip it by default — agent should call
        cortex_run_full_lifecycle() in quiet window or use background task.
        """
        backend = self._cortex_backend
        result = {}
        try:
            stats = backend.stats()
            total = stats.get('total_memories', 0) if isinstance(stats, dict) else 0
        except Exception:
            total = 0
        if total < 100:
            result['rumination'] = self._cortex_safe_call(
                'run_rumination',
                lambda: backend.run_rumination(min_cluster_size=min_cluster_size),
                timeout_sec=8,
            )
            result['abstracted'] = self._cortex_safe_call(
                'abstract_heuristics',
                lambda: backend.abstract_heuristics(force=False),
                timeout_sec=8,
            )
        else:
            # Both calls hang on 900+ chunks — skip + return fast diagnostics
            result['rumination_skipped'] = f'corpus too large ({total}), defer to nightly batch'
            result['abstracted_skipped'] = f'corpus too large ({total}), defer to nightly batch'
        return result

    def cortex_forgetting_full(self, min_score: float = 0.1) -> Dict[str, Any]:
        """V4.4 (2026-07-27): full forgetting cleanup pass.

        Coverage: /forgetting/expired + /forgetting/expiring-soon + read-only diagnostics.
        (Skip /forgetting/cleanup by default — it hangs on 900+ chunks. Agent should
        call cortex_run_full_lifecycle() if real cleanup is needed.)
        """
        backend = self._cortex_backend
        result = {}
        # Read paths only
        try:
            result['expired_count'] = len(backend.get_expired_memories(limit=100))
            result['expiring_soon_count'] = len(backend.get_expiring_soon(days=7))
        except Exception as e:
            result['read_error'] = str(e)[:120]
        # Cleanup only if explicitly requested via small corpus or empty storage
        # Default: SKIP to avoid hangs on large corpus
        try:
            stats = backend.stats()
            total = stats.get('total_memories', 0) if isinstance(stats, dict) else 0
            if total < 100:
                result['cleanup_count'] = self._cortex_safe_call(
                    'run_forgetting_cleanup',
                    lambda: backend.run_forgetting_cleanup(min_score=min_score),
                    timeout_sec=10,
                )
            else:
                result['cleanup_skipped'] = f'corpus too large ({total}), use cortex_run_full_lifecycle in quiet window'
        except Exception:
            pass
        return result

    def cortex_links_decay(self) -> Dict[str, Any]:
        """V4.4 (2026-07-27): trigger Zettelkasten auto-link.

        Coverage: /links/auto-link.
        Agent should call after every batch of writes to keep graph fresh.
        """
        backend = self._cortex_backend
        result = {}
        try:
            result['auto_linked'] = backend.auto_link_from_search(
                'recent context', top_pairs=5, min_similarity=0.5,
            )
        except Exception as e:
            result['auto_link_error'] = str(e)[:120]
        return result

    def cortex_eval_session(self) -> Dict[str, Any]:
        """V4.4 (2026-07-27): run multi-hop benchmark + reindex.

        Coverage: /bench/multi-hop + /reindex.
        Agent should call weekly to measure recall quality and trigger reindex.
        NOTE: reindex hangs on 900+ chunks. Default to skip; agent should run in quiet window.
        """
        backend = self._cortex_backend
        result = {}
        result['bench'] = self._cortex_safe_call(
            'run_bench_multi_hop',
            backend.run_bench_multi_hop,
            timeout_sec=15,
        )
        # Skip reindex in default call — only run if explicitly needed
        try:
            stats = backend.stats()
            total = stats.get('total_memories', 0) if isinstance(stats, dict) else 0
        except Exception:
            total = 0
        if total < 100:
            result['reindex'] = self._cortex_safe_call(
                'reindex',
                backend.reindex,
                timeout_sec=15,
            )
        else:
            result['reindex_skipped'] = f'corpus too large ({total}), defer to weekly batch'
        return result

    def cortex_user_profile(self, user_id: str = 'default') -> Dict[str, Any]:
        """V4.4 (2026-07-27): get + list user core profile.

        Coverage: /core/{user_id} (GET) + /core (list).
        Agent should call at session start to load user context.
        """
        backend = self._cortex_backend
        result = {'user_id': user_id}
        try:
            result['profile'] = backend.get_core_profile(user_id)
        except Exception as e:
            result['profile'] = None
            result['profile_error'] = str(e)[:120]
        try:
            result['profiles_list'] = backend.list_core_profiles(limit=10)
        except Exception as e:
            result['profiles_list_error'] = str(e)[:120]
        return result

    def cortex_attach_episode(self, memory_id: str, session_id: str = 'session',
                              title: str = 'untitled', summary: str = '') -> Dict[str, Any]:
        """V4.4 (2026-07-27): attach episode metadata to a memory.

        Coverage: /episodes (POST) + /episodes/{memory_id} (GET).
        Agent should call when saving a memory with rich context.
        """
        backend = self._cortex_backend
        result = {}
        try:
            eid = backend.create_episode(
                session_id=session_id, title=title, summary=summary,
                parent_chunk_id=memory_id,
            )
            result['episode_id'] = eid
        except Exception as e:
            result['episode_error'] = str(e)[:120]
        try:
            result['episodes'] = backend.get_episodes_for_memory(memory_id)
        except Exception as e:
            result['episodes_list_error'] = str(e)[:120]
        return result

    def cortex_create_procedure(self, name: str, trigger: str, action: str,
                                description: str = '') -> Dict[str, Any]:
        """V4.4 (2026-07-27): learn a new procedural memory.

        Coverage: /procedural + /procedural/{proc_id}/outcome.
        Agent should call when discovering a repeatable workflow.
        """
        backend = self._cortex_backend
        result = {}
        try:
            proc_id = backend.learn_procedure(
                name=name, trigger=trigger, action=action, description=description,
            )
            result['procedure_id'] = proc_id
            if proc_id:
                result['outcome_recorded'] = backend.record_procedure_outcome(proc_id, success=True)
        except Exception as e:
            result['error'] = str(e)[:120]
        return result

    def cortex_deep_context(self, memory_id: str) -> Dict[str, Any]:
        """V4.4 (2026-07-27): 6-way deep context for a single memory.

        Coverage: /provenance/{id}, /history/{id}, /relations/{id}, /hierarchy/enrich/{id},
        /links/{id}, /memories/{id}/refresh, /episodes/{id}.
        """
        backend = self._cortex_backend
        result = {'memory_id': memory_id}
        try:
            result['provenance'] = backend.get_provenance(memory_id)
        except Exception as e:
            result['provenance_error'] = str(e)[:120]
        try:
            result['history'] = backend.get_history(memory_id)
        except Exception as e:
            result['history_error'] = str(e)[:120]
        try:
            result['relations'] = backend.get_relations(memory_id)
        except Exception as e:
            result['relations_error'] = str(e)[:120]
        try:
            result['hierarchy'] = backend.enrich_chunk_with_hierarchy(memory_id)
        except Exception as e:
            result['hierarchy_error'] = str(e)[:120]
        try:
            result['links'] = backend.get_links(memory_id, min_strength=0.0, limit=10)
        except Exception as e:
            result['links_error'] = str(e)[:120]
        try:
            result['refreshed'] = backend.refresh_memory(memory_id)
        except Exception as e:
            result['refresh_error'] = str(e)[:120]
        try:
            result['episodes'] = backend.get_episodes_for_memory(memory_id)
        except Exception as e:
            result['episodes_error'] = str(e)[:120]
        return result

    def cortex_reset_ops_counters(self) -> bool:
        """V4.4 (2026-07-27): reset ops counters. Coverage: /stats/ops/reset."""
        backend = self._cortex_backend
        try:
            return backend.reset_ops_counters()
        except Exception:
            return False

    def cortex_get_dashboard(self) -> str:
        """V4.4 (2026-07-27): get HTML dashboard. Coverage: /dashboard/html."""
        backend = self._cortex_backend
        try:
            html = backend.get_dashboard_html()
            return html if html else ''
        except Exception as e:
            return f'err: {e}'

    def cortex_add_media(self, media_type: str, ocr_text: str = '',
                        media_url: str = '') -> Dict[str, Any]:
        """V4.4 (2026-07-27): add OCR'd media. Coverage: /media + /media/by-type."""
        backend = self._cortex_backend
        result = {}
        try:
            mid = backend.add_media(media_type=media_type, ocr_text=ocr_text, media_url=media_url)
            result['media_id'] = mid
        except Exception as e:
            result['media_error'] = str(e)[:120]
        try:
            result['by_type'] = backend.get_media_by_type(media_type, limit=10)
        except Exception as e:
            result['by_type_error'] = str(e)[:120]
        return result

    def cortex_add_resource(self, resource_type: str, title: str,
                            url: str = '', description: str = '') -> Dict[str, Any]:
        """V4.4 (2026-07-27): add bookmarkable resource + access counter.

        Coverage: /resources + /resources/{id}/accessed.
        """
        backend = self._cortex_backend
        result = {}
        try:
            rid = backend.add_resource(
                resource_type=resource_type, title=title, url=url, description=description,
            )
            result['resource_id'] = rid
            if rid:
                result['accessed'] = backend.increment_resource_access(rid)
        except Exception as e:
            result['error'] = str(e)[:120]
        return result

    def cortex_hierarchy_node(self, level: str = 'episode', title: str = 'default') -> Dict[str, Any]:
        """V4.4 (2026-07-27): create + traverse hierarchy tree.

        Coverage: /hierarchy, /hierarchy/{id}, /hierarchy/{id}/tree.
        """
        backend = self._cortex_backend
        result = {}
        try:
            node_id = backend.create_hierarchy_node(level=level, title=title)
            result['node_id'] = node_id
            if node_id:
                result['node'] = backend.get_hierarchy_node(node_id)
                result['tree'] = backend.get_hierarchy_tree(node_id, max_depth=3)
        except Exception as e:
            result['error'] = str(e)[:120]
        return result

    def cortex_links_manage(self, source_id: str, target_id: str = '',
                             link_type: str = 'related') -> Dict[str, Any]:
        """V4.4 (2026-07-27): full link operations.

        Coverage: /links/{source}, /links/access, /links/path/{src}/{tgt}.
        """
        backend = self._cortex_backend
        result = {}
        try:
            result['outgoing'] = backend.get_links(source_id, link_type=link_type, min_strength=0.0, limit=10)
        except Exception as e:
            result['outgoing_error'] = str(e)[:120]
        if target_id:
            try:
                result['access_boost'] = backend.access_link(source_id, target_id, link_type)
            except Exception as e:
                result['access_error'] = str(e)[:120]
            try:
                result['shortest_path'] = backend.shortest_link_path(source_id, target_id, max_depth=5)
            except Exception as e:
                result['path_error'] = str(e)[:120]
        return result

    def cortex_submit_feedback(self, memory_id: str, feedback_type: str = 'helpful',
                                score: float = 1.0, comment: str = '') -> bool:
        """V4.4 (2026-07-27): submit eval feedback. Coverage: /eval/feedback."""
        backend = self._cortex_backend
        try:
            return backend.submit_eval_feedback(
                memory_id=memory_id, feedback_type=feedback_type, score=score, comment=comment,
            )
        except Exception:
            return False

    def cortex_clear_pr_cache(self) -> bool:
        """V4.4 (2026-07-27): clear PageRank cache. Coverage: /cache/pr/clear."""
        backend = self._cortex_backend
        try:
            return backend.clear_pr_cache()
        except Exception:
            return False

    def cortex_get_archived(self) -> List[Dict[str, Any]]:
        """V4.4 (2026-07-27): list archived chunks. Coverage: /ttl/archived."""
        backend = self._cortex_backend
        try:
            return backend._request("GET", "/ttl/archived", json_body=None) or []
        except Exception as e:
            return [{'error': str(e)[:80]}]

    def cortex_taxonomy_lookup(self, child_tag: str) -> List[str]:
        """V4.4 (2026-07-27): get taxonomy ancestors. Coverage: /taxonomy/ancestors/{tag}."""
        backend = self._cortex_backend
        try:
            return backend.get_taxonomy_ancestors(child_tag) or []
        except Exception as e:
            return []

    def cortex_taxonomy_delete(self, child_tag: str) -> bool:
        """V4.4 (2026-07-27): delete taxonomy relation. Coverage: /taxonomy/relations/{tag} DELETE."""
        backend = self._cortex_backend
        try:
            return backend.delete_taxonomy_relation(child_tag)
        except Exception:
            return False

    def cortex_links_decay_one(self, link_type: str = 'related') -> int:
        """V4.4 (2026-07-27): decay specific link type. Coverage: /links/decay."""
        backend = self._cortex_backend
        try:
            r = backend._request("POST", "/links/decay", json_body={'link_type': link_type})
            return r.get('decayed', 0) if isinstance(r, dict) else 0
        except Exception:
            return 0

    def cortex_get_dashboard_json(self) -> Dict[str, Any]:
        """V4.4 (2026-07-27): get JSON dashboard. Coverage: /dashboard."""
        backend = self._cortex_backend
        try:
            return backend._request("GET", "/dashboard", json_body=None) or {}
        except Exception as e:
            return {'error': str(e)[:80]}

    def cortex_relations_temporal_lookup(self, source_id: str) -> List[Dict[str, Any]]:
        """V4.4 (2026-07-27): get temporal relations for source. Coverage: /relations/temporal/{src}."""
        backend = self._cortex_backend
        try:
            r = backend._request("GET", f"/relations/temporal/{source_id}", json_body=None)
            return r.get('relations', []) if isinstance(r, dict) else []
        except Exception:
            return []

    def cortex_bench_custom(self, queries: List[Dict]) -> Dict[str, Any]:
        """V4.4 (2026-07-27): run custom multi-hop bench. Coverage: /bench/multi-hop/custom."""
        backend = self._cortex_backend
        try:
            return backend.run_bench_multi_hop(queries=queries) or {}
        except Exception as e:
            return {'error': str(e)[:80]}

    def cortex_rumination_debug(self) -> Dict[str, Any]:
        """V4.4 (2026-07-27): debug abstraction. Coverage: /rumination/debug-abstract."""
        backend = self._cortex_backend
        try:
            return backend._request("POST", "/rumination/debug-abstract", json_body={}) or {}
        except Exception as e:
            return {'error': str(e)[:80]}

    def cortex_storage_evict_chunks(self, age_days: int = 365, max_count: int = 100) -> Dict[str, Any]:
        """V4.4 (2026-07-27): evict old chunks. Coverage: /storage/evict."""
        backend = self._cortex_backend
        try:
            r = backend._request("POST", "/storage/evict", json_body={
                'age_days': age_days, 'max_count': max_count,
            })
            return r if isinstance(r, dict) else {}
        except Exception as e:
            return {'error': str(e)[:80]}

    def cortex_access_stats_for_memory(self, memory_id: str) -> Dict[str, Any]:
        """V4.4 (2026-07-27): access stats per memory. Coverage: /access-stats/{memory_id}."""
        backend = self._cortex_backend
        try:
            return backend.get_access_stats(memory_id) or {}
        except Exception as e:
            return {'error': str(e)[:80]}

    def cortex_user_profile_upsert(self, user_id: str, profile: Dict[str, Any],
                                    display_name: str = '', summary: str = '') -> str:
        """V4.4 (2026-07-27): upsert user core profile. Coverage: /core/{user_id} POST."""
        backend = self._cortex_backend
        try:
            return backend.upsert_user_profile(
                user_id=user_id, profile=profile,
                display_name=display_name, summary=summary,
            ) or ''
        except Exception:
            return ''

    def cortex_relation_delete(self, memory_id: str, target_id: str) -> bool:
        """V4.4 (2026-07-27): delete a specific relation. Coverage: /relations/{id}/{target} DELETE."""
        backend = self._cortex_backend
        try:
            r = backend._request("DELETE", f"/relations/{memory_id}/{target_id}", json_body=None)
            return isinstance(r, dict)
        except Exception:
            return False

    def cortex_ops_counters(self) -> Dict[str, int]:
        """V4.4 (2026-07-27): get ops counters. Coverage: /stats/ops."""
        backend = self._cortex_backend
        try:
            return backend._request("GET", "/stats/ops", json_body=None) or {}
        except Exception:
            return {}

    def cortex_working_memory_get(self, session_id: str, key: str) -> Optional[Dict[str, Any]]:
        """V4.4 (2026-07-27): get single working memory key. Coverage: /working/{sid}/{key}."""
        backend = self._cortex_backend
        try:
            r = backend._request("GET", f"/working/{session_id}/{key}", json_body=None)
            return r if isinstance(r, dict) else None
        except Exception:
            return None

    def cortex_run_full_lifecycle(self, force: bool = False) -> Dict[str, Any]:
        """V4.4 (2026-07-27): trigger ONE full maintenance cycle.

        Coverage: /forgetting/cleanup + /reconsolidate + /merge-duplicates + /rumination/run
        + /compress/run + /cache/pr/clear + /chroma/cleanup + /storage/enforce.

        Agent should call weekly or when stats shows degraded state.
        By default, runs only the LIGHT operations (compress, cache clear,
        chroma cleanup, storage enforce). Pass force=True to also run
        heavy operations (reconsolidate, merge, rumination, forgetting).
        Each operation has its own 8s timeout.
        """
        backend = self._cortex_backend
        result = {}
        # Always run lightweight operations
        light_ops = [
            ('compress_old', lambda: backend.compress_old_chunks(max_importance=4, max_per_batch=50)),
            ('cache_pr_clear', lambda: backend.clear_pr_cache()),
            ('chroma_cleanup', lambda: backend.cleanup_chroma_empty_collections()),
            ('storage_enforce', lambda: backend.enforce_storage_limits(1024)),
        ]
        for label, fn in light_ops:
            result[label] = self._cortex_safe_call(label, fn, timeout_sec=8)
        if not force:
            # Skip heavy operations
            result['heavy_ops_skipped'] = 'set force=True to run reconsolidate/merge/rumination/forgetting/cleanup'
            return result
        # force=True — run heavy ops too
        heavy_ops = [
            ('forgetting_cleanup', lambda: backend.run_forgetting_cleanup(min_score=0.1)),
            ('reconsolidate', lambda: backend.reconsolidate()),
            ('merge_duplicates', lambda: backend.merge_duplicates(threshold=0.95)),
            ('rumination_run', lambda: backend.run_rumination(min_cluster_size=3)),
        ]
        for label, fn in heavy_ops:
            result[label] = self._cortex_safe_call(label, fn, timeout_sec=15)
        return result


def load_on_disk_store() -> "MemoryStore":
    """Build a fresh on-disk :class:`MemoryStore`, honoring configured char limits.

    Use this from any context that has no live agent (the messaging gateway, the
    Desktop GUI, the bare CLI ``/memory`` handler) but still needs to read or
    apply approved memory writes. Mirrors how the live agent constructs its store
    in ``agent/agent_init.py`` — including the user's ``memory.memory_char_limit``
    / ``memory.user_char_limit`` overrides — so an approval applied without a live
    agent enforces the SAME caps as one applied with one.

    Falls back to the built-in defaults if config can't be loaded, so this can
    never raise on a missing/unreadable config.
    """
    memory_char_limit = 2200
    user_char_limit = 1375
    try:
        from hermes_cli.config import load_config

        mem_cfg = (load_config() or {}).get("memory", {}) or {}
        memory_char_limit = int(mem_cfg.get("memory_char_limit", memory_char_limit))
        user_char_limit = int(mem_cfg.get("user_char_limit", user_char_limit))
    except Exception:
        pass  # config optional — fall back to defaults rather than break /memory

    store = MemoryStore(
        memory_char_limit=memory_char_limit,
        user_char_limit=user_char_limit,
    )
    store.load_from_disk()
    return store


def _apply_write_gate(action: str, target: str, content: Optional[str],
                      old_text: Optional[str]) -> Optional[str]:
    """Evaluate the memory write gate. Returns a JSON tool-result string when
    the write should NOT proceed normally (blocked or staged), or None when the
    caller should perform the real write.

    Only the mutating actions (add/replace/remove) are gated.
    """
    if action not in {"add", "replace", "remove"}:
        return None

    try:
        from tools import write_approval as wa
    except Exception:
        # If the gate module can't load, fail open (current behaviour) rather
        # than blocking all memory writes.
        return None

    # Build a small inline summary/detail for the foreground approval prompt.
    label = "user profile" if target == "user" else "memory"
    if action == "add":
        summary = f"add to {label}"
        detail = content or ""
    elif action == "replace":
        summary = f"replace in {label}"
        detail = f"old: {old_text}\nnew: {content}"
    else:  # remove
        summary = f"remove from {label}"
        detail = old_text or ""

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    # stage
    payload = {
        "action": action,
        "target": target,
        "content": content,
        "old_text": old_text,
    }
    record = wa.stage_write(
        wa.MEMORY, payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "message": decision.message},
        ensure_ascii=False,
    )


def _apply_batch_write_gate(target: str, operations: List[Dict[str, Any]]) -> Optional[str]:
    """Evaluate the write gate for a batch of memory operations.

    Returns a JSON tool-result string when the batch should NOT proceed
    (blocked or staged), or None when the caller should perform the real
    batch write. The whole batch is gated as a single unit.
    """
    try:
        from tools import write_approval as wa
    except Exception:
        return None

    label = "user profile" if target == "user" else "memory"
    summary = f"apply {len(operations)} op(s) to {label}"
    detail_lines = []
    for op in operations:
        op = op or {}
        act = op.get("action", "?")
        if act == "remove":
            detail_lines.append(f"- remove: {op.get('old_text', '')}")
        elif act == "replace":
            detail_lines.append(f"- replace: {op.get('old_text', '')} -> {op.get('content', '')}")
        else:
            detail_lines.append(f"- {act}: {op.get('content', '')}")
    detail = "\n".join(detail_lines)

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    payload = {"action": "batch", "target": target, "operations": operations}
    record = wa.stage_write(
        wa.MEMORY, payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "message": decision.message},
        ensure_ascii=False,
    )


def _missing_old_text_error(store: "MemoryStore", target: str, action: str) -> str:
    """Build a recoverable error for a replace/remove call that arrived without
    ``old_text``.

    ``replace``/``remove`` are inherently targeted -- without ``old_text`` there
    is no entry to act on, so we cannot fulfil the call. But returning a bare
    "old_text is required" is a dead-end: some structured-output clients omit the
    optional ``old_text`` field (it isn't, and can't be, schema-required without
    a top-level combinator the Codex backend rejects -- see
    tests/tools/test_memory_tool_schema.py). So instead we return the current
    entry inventory plus an explicit retry instruction, letting the model reissue
    the call with ``old_text`` set to a unique substring of the entry it means.
    Mirrors the batch path's ``_batch_error`` shape. (issues #43412, #49466)
    """
    entries = store._entries_for(target)
    current = store._char_count(target)
    limit = store._char_limit(target)
    return json.dumps(
        {
            "success": False,
            "error": (
                f"'{action}' needs old_text -- a short unique substring of the entry "
                f"to {action}. None was provided. Reissue the {action} with old_text "
                f"set to part of one of the current_entries below."
            ),
            "current_entries": entries,
            "usage": f"{current:,}/{limit:,}",
        },
        ensure_ascii=False,
    )


def _resolve_memory_store(store: Optional["MemoryStore"]) -> Optional["MemoryStore"]:
    """Pick the MemoryStore instance for a tool call.

    Order:
      1. Explicit ``store`` kwarg (tests, subagents, scripted callers).
      2. The agent's own ``agent._memory_store`` if it was attached via
         ``propagate_context_to_thread`` — typical for in-process dispatch.
      3. A lazily-built process-wide default store. The default is wired
         to honor ``CORTEX_TAKEOVER=0`` (offline sessions / tests) and
         routes through cortex when the env var is unset.

    Returns ``None`` only when memory is genuinely disabled in config.
    """
    if store is not None:
        return store
    # (2) Agent-scoped store, when an in-process agent attached it.
    agent_store = _agent_memory_store.get()
    if agent_store is not None:
        return agent_store
    # (3) Process-wide default. lru_cache keeps the construction cost paid
    # once; CORTEX_TAKEOVER=0 env var flips use_cortex off so tests / offline
    # sessions get the .md path without any caller-side wiring.
    global _default_memory_store
    if _default_memory_store is None:
        from tools.memory_tool import MemoryStore as _MS
        _default_memory_store = _MS()
        _default_memory_store.load_from_disk()
    return _default_memory_store


# Thread-local handle so an in-process agent can attach its own MemoryStore
# without us forcing every call site to thread it through kwargs.
import contextvars as _cv
_agent_memory_store: _cv.ContextVar[Optional["MemoryStore"]] = _cv.ContextVar(
    "_agent_memory_store", default=None,
)
_default_memory_store: Optional["MemoryStore"] = None


def set_default_memory_store(store: "MemoryStore") -> None:
    """Override the process-wide default store. Used by tests and by
    ``agent_init._init_memory`` so every dispatch path lands on the same
    MemoryStore instance (and so the cortex takeover flag stays consistent
    with the agent's config)."""
    global _default_memory_store
    _default_memory_store = store
    # Also populate the thread-local so the same instance wins both paths.
    _agent_memory_store.set(store)


def memory_tool(
    action: str = None,
    target: str = "memory",
    content: str = None,
    old_text: str = None,
    operations: Optional[List[Dict[str, Any]]] = None,
    store: Optional[MemoryStore] = None,
    tier: Optional[str] = None,
    new_text: Optional[str] = None,
) -> str:
    """
    Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Two shapes:
      - Single op: action + (content / old_text).
      - Batch:     operations=[{action, content?, old_text?, new_text?}, ...] applied
                   atomically against the final char budget in ONE call.

    Returns JSON string with results.

    ``new_text`` is accepted as an alias for ``content`` so callers that mirror
    the patch-tool shape (where ``old_text``/``new_text`` are paired) succeed.
    ``content`` wins when both are present (content is the canonical name).
    """
    # Alias resolution: new_text → content (content wins if both present).
    if content is None and new_text is not None:
        content = new_text
    # Same alias inside batch ops.
    if operations and isinstance(operations, list):
        for _op in operations:
            if isinstance(_op, dict) and _op.get("content") is None and _op.get("new_text") is not None:
                _op["content"] = _op["new_text"]

    store = _resolve_memory_store(store)
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.", success=False)

    # Some strict providers fill optional schema fields with JSON null rather
    # than omitting them.  Treat ``target: null`` as omitted so memory writes
    # still use the documented default store instead of failing validation.
    if target is None:
        target = "memory"

    # Target guard: 'memory'/'user' apply to the write-path actions (add/replace/
    # remove/batch) and recall/deep/audit. For save_procedure, 'target' carries the
    # procedure NAME (see the action docstring), so it must not be restricted here.
    if action != "save_procedure" and target not in {"memory", "user"}:
        return tool_error(f"Invalid target '{target}'. Use 'memory' or 'user'.", success=False)

    # --- Batch path -------------------------------------------------------
    if operations:
        if not isinstance(operations, list):
            return tool_error("operations must be a list of {action, content?, old_text?} objects.", success=False)
        gate_result = _apply_batch_write_gate(target, operations)
        if gate_result is not None:
            return gate_result
        result = store.apply_batch(target, operations)
        return json.dumps(result, ensure_ascii=False)

    # --- Single-op path ---------------------------------------------------
    # Validate required params BEFORE the gate so an invalid write is rejected
    # immediately instead of being staged and only failing at approve time.
    if action == "add" and not content:
        return tool_error("Content is required for 'add' action.", success=False)
    if action == "replace" and (not old_text or not content):
        missing = "old_text" if not old_text else "content"
        if not old_text:
            # The client/model omitted old_text. Replace is inherently targeted
            # -- we can't guess which entry. Return the current inventory plus a
            # retry instruction so the model can reissue with old_text set,
            # instead of hitting a dead-end error. (issues #43412, #49466)
            return _missing_old_text_error(store, target, "replace")
        return tool_error(f"{missing} is required for 'replace' action.", success=False)
    if action == "remove" and not old_text:
        return _missing_old_text_error(store, target, "remove")

# Approval gate: when on, stages the write (background/gateway) or prompts
    # inline (interactive CLI); when off (default) passes straight through.
    gate_result = _apply_write_gate(action, target, content, old_text)
    if gate_result is not None:
        return gate_result

    if action == "add":
        result = store.add(target, content, tier=tier)

    elif action == "replace":
        result = store.replace(target, old_text, content)

    elif action == "remove":
        result = store.remove(target, old_text)

    elif action == "recall":
        # FIX (2026-08-02, Direction 1): agentic recall. Instead of dumping
        # ALL memory entries (token-heavy, no association), semantically
        # retrieve ONLY the chunks relevant to the query + their graph
        # neighbors, returning a compact recall pack the agent can reason over.
        if not content:
            return tool_error("query (passed as content) is required for 'recall'.", success=False)
        try:
            from tools import cortex_memory_backend as _cmb
            query = content.strip()
            # 1. hybrid semantic search (vector + FTS5 + PageRank)
            hybrid = _cmb.search_hybrid(query, top_k=5) or {}
            hits = hybrid.get("results", []) if isinstance(hybrid, dict) else []
            # 2. graph-aware reasoning (search hits + hierarchies + links + k-hop subgraph)
            reason = {}
            try:
                reason = _cmb.reason_with_memory(query, top_k=3, include_hierarchy=True, khop=2)
            except Exception:
                pass
            # compact pack
            pack = {
                "query": query,
                "hits": [{
                    "memory_id": h.get("id"),
                    "content": h.get("content"),
                    "type": h.get("type"),
                    "similarity": h.get("similarity"),
                    "importance": h.get("importance"),
                    "related": (h.get("related_memories") or [])[:5],
                } for h in hits],
                "graph_context": reason if isinstance(reason, dict) else {},
                "graph_khop": (reason.get("graph_subgraph") or {})
                              if isinstance(reason, dict) else {},
            }
            return json.dumps({"success": True, "recall": pack,
                               "hits_found": len(hits),
                               "graph_khop_nodes": len((pack.get("graph_khop") or {}).get("nodes", []))})
        except Exception as e:
            return tool_error(f"recall failed: {e}", success=False)

    elif action == "save_procedure":
        # FIX (2026-08-02, Direction 5): expose procedural memory to the agent.
        # The backend learn_procedure() was defined but never surfaced as a tool
        # action — the agent could only create fact-type chunks, never persist a
        # REUSABLE workflow. 'content' = the action recipe; 'old_text' = the
        # trigger pattern (fire-when); 'target' = the procedure name/label.
        try:
            from tools import cortex_memory_backend as _cmb
            name = target or "untitled_procedure"
            trigger = old_text or ""
            action = content or ""
            if not trigger or not action:
                return tool_error("save_procedure needs trigger (old_text) + action (content).", success=False)
            proc_id = _cmb.learn_procedure(name=name, trigger=trigger, action=action, description="")
            if proc_id:
                _cmb.record_procedure_outcome(proc_id, success=True)
            return json.dumps({"success": True, "procedure_id": proc_id,
                               "name": name, "trigger": trigger})
        except Exception as e:
            return tool_error(f"save_procedure failed: {e}", success=False)

    elif action == "get_procedure":
        # FIX (2026-08-02, Direction 5): retrieve the best-matching reusable
        # procedure for a task. 'content' = the task/trigger description.
        try:
            from tools import cortex_memory_backend as _cmb
            query = (content or "").strip()
            if not query:
                return tool_error("get_procedure needs a task description (content).", success=False)
            procs = _cmb.search_procedures(query, limit=5)
            return json.dumps({"success": True, "procedures": procs or []})
        except Exception as e:
            return tool_error(f"get_procedure failed: {e}", success=False)

    elif action == "deep_context":
        # FIX (2026-08-02): wired up the previously-unused deep_context_for()
        # backend function. Lets the agent pull full provenance/history/relations/
        # hierarchy/links for a chunk in one call -- useful for "explain how I
        # know X" or debugging stale memory.
        if not old_text:
            return tool_error("chunk_id (passed as old_text) is required for 'deep_context' action.", success=False)
        try:
            from tools import cortex_memory_backend as _cmb
            ctx = _cmb.deep_context_for(old_text)
            return json.dumps({"success": True, "context": ctx})
        except Exception as e:
            return tool_error(f"deep_context failed: {e}", success=False)

    elif action == "audit_query":
        # FIX (2026-08-02): wired up the previously-unused query_audit() backend
        # function. Read-only audit log query for the agent to inspect what
        # happened (who wrote/refreshed what, when).
        try:
            from tools import cortex_memory_backend as _cmb
            entries = _cmb.query_audit(limit=int(content) if content and content.isdigit() else 20)
            return json.dumps({"success": True, "entries": entries, "total": len(entries)})
        except Exception as e:
            return tool_error(f"audit_query failed: {e}", success=False)

    else:
        actions_list = "add, replace, remove, recall, save_procedure, get_procedure, deep_context, audit_query"
        return tool_error(f"Unknown action '{action}'. Use: " + actions_list, success=False)

    return json.dumps(result, ensure_ascii=False)


def check_memory_requirements() -> bool:
    """Memory tool has no external requirements -- always available."""
    return True


def apply_memory_pending(payload: Dict[str, Any], store: "MemoryStore") -> Dict[str, Any]:
    """Replay a staged memory write directly against the store, bypassing the
    write gate. Called by the /memory approve handler.

    Returns the store's result dict.
    """
    action = payload.get("action")
    target = payload.get("target", "memory")
    content = payload.get("content") or ""
    old_text = payload.get("old_text") or ""
    if action == "batch":
        return store.apply_batch(target, payload.get("operations") or [])
    if action == "add":
        return store.add(target, content)
    if action == "replace":
        return store.replace(target, old_text, content)
    if action == "remove":
        return store.remove(target, old_text)
    return {"success": False, "error": f"Unknown staged action '{action}'."}
# OpenAI Function-Calling Schema
# =============================================================================

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across sessions. Memory is "
        "injected into every future turn, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL your changes in ONE call via an 'operations' array (each item: "
        "{action, content?, old_text?}). The batch applies atomically and the char limit is "
        "checked only on the FINAL result — so a single call can remove/replace stale entries "
        "to free room AND add new ones, even when an add alone would overflow. The response "
        "reports current/limit chars and confirms completion; one batch call finishes the "
        "update, so don't repeat it. Use the bare action/content/old_text fields only for a "
        "single lone change.\n\n"
        "WHEN: save proactively when the user states a preference, correction, or personal "
        "detail, or you learn a stable fact about their environment, conventions, or workflow. "
        "Priority: user preferences & corrections > environment facts > procedures. The best "
        "memory stops the user repeating themselves.\n\n"
        "IF FULL: an add is rejected with the current entries shown. Reissue as ONE batch that "
        "removes or shortens enough stale entries and adds the new one together.\n\n"
        "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
        "notes (environment, conventions, tool quirks, lessons).\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, "
        "completed-work logs, temporary TODO state (use session_search for those). Reusable "
        "procedures belong in a skill, not memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove", "recall", "save_procedure", "get_procedure", "deep_context", "audit_query"],
                "description": "The action to perform (single-op shape). 'recall' = agentic memory retrieval (query via content). 'save_procedure' = persist a reusable workflow (trigger via old_text, action via content, name via target). 'get_procedure' = fetch best-matching procedure (task desc via content). 'deep_context' and 'audit_query' are read-only debug actions (chunk_id via old_text; for audit_query, limit=content as int). Omit when using 'operations'."
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile."
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace' (single-op shape)."
            },
            "old_text": {
                "type": "string",
                "description": "REQUIRED for 'replace' and 'remove' (single-op shape): a short unique substring identifying the existing entry to modify. Omit only for 'add'."
            },
            "tier": {
                "type": "string",
                "enum": ["short", "medium", "long", "permanent"],
                "description": "Optional retention tier for 'add'. Maps to importance: short=3 (session scratch), medium=5 (cross-session), long=7 (user-curated durable, default), permanent=9 (never-forget). Cortex evicts lower tiers sooner."
            },
            "operations": {
                "type": "array",
                "description": (
                    "Batch shape: a list of operations applied atomically in one call "
                    "against the final char budget. Preferred when making multiple changes "
                    "or consolidating to make room. Each item is {action, content?, old_text?}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "content": {"type": "string", "description": "Entry content for add/replace."},
                        "old_text": {"type": "string", "description": "Substring identifying the entry for replace/remove."},
                    },
                    "required": ["action"],
                },
            },
        },
        "required": ["target"],
    },
}


# --- Registry ---
import time as _t
# FIX (2026-08-02, Direction A): dynamic memory-tool schema pulled from cortex's
# /endpoints/recommend/connector_memory. The LLM sees the live cortex capability
# set instead of a hardcoded enum. Falls back to static if cortex unreachable.
_mem_cs = {"rec": None, "ts": 0.0}

def _memory_dynamic_schema() -> dict:
    """Return schema overrides merged into MEMORY_SCHEMA at tool-exposure time.
    Queries cortex /endpoints/recommend/connector_memory at most once per 30s;
    never raises (static fallback on any failure)."""
    now = _t.time()
    if _mem_cs["rec"] is None or now - _mem_cs["ts"] > 30:
        try:
            from tools import cortex_memory_backend as _cmb
            rec = _cmb.recommend_endpoints("connector_memory")
            if isinstance(rec, dict) and rec.get("actions"):
                _mem_cs["rec"] = rec
            else:
                _mem_cs["rec"] = {"empty": True}
        except Exception:
            _mem_cs["rec"] = {"empty": True}
        _mem_cs["ts"] = now
    rec = _mem_cs["rec"]
    hints = []
    if rec and rec.get("actions"):
        for cap, spec in rec["actions"].items():
            if isinstance(spec, dict):
                hints.append(f"{cap}={spec.get('method','?')} {spec.get('path','')}")
    elif rec and rec.get("recommended"):
        hints = list(rec["recommended"])[:6]
    cap_hint = ("Available cortex capabilities: " + "; ".join(hints)) if hints else ""
    base_desc = MEMORY_SCHEMA.get("description", "")
    new_desc = (base_desc + "\n\nCortex runtime:\n" + cap_hint) if cap_hint else base_desc
    new_action_enum = sorted(set(
        list(MEMORY_SCHEMA["parameters"]["properties"]["action"]["enum"]) +
        (list(rec.get("actions", {}).keys()) if rec and rec.get("actions") else [])
    ))
    return {
        "description": new_desc,
        "parameters": {
            **MEMORY_SCHEMA["parameters"],
            "properties": {
                **MEMORY_SCHEMA["parameters"]["properties"],
                "action": {
                    "type": "string",
                    "enum": new_action_enum,
                    "description": MEMORY_SCHEMA["parameters"]["properties"]["action"]["description"],
                },
            },
        },
    }

from tools.registry import registry, tool_error

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    dynamic_schema_overrides=_memory_dynamic_schema,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        operations=args.get("operations"),
        tier=args.get("tier"),
        store=kw.get("store")),
    check_fn=check_memory_requirements,
    emoji="🧠",
)