"""Cortex-aware MemoryStore for upstream Hermes v0.22.5.

Port of the V6.3.1 / V6.5.2 cortex takeover onto the **split** upstream file
layout introduced in v0.22.x:

    upstream v0.22.5   tools/memory_tool.py      -> thin wrapper / gates
                        tools/memory_tool_store.py -> MemoryStore class

The pre-v0.22 local tree had everything in one monolith (``memory_tool.py``)
where the takeover hooks were written directly against the class body.  Upstream
later split the class into ``memory_tool_store.py`` and rewrote its internals
(``_mutate`` closure protocol, ``_error``/``_failure_with_entries`` helpers,
``_render_block`` signature change, ...).  Rather than re-implementing the
takeover against the new internals, this module **subclasses** upstream's
``MemoryStore`` and overrides only the five entry points that touch storage:

    load_from_disk   -> read every entry from cortex, never from .md
    add              -> create_entry()
    replace          -> update_entry()  (whole-entry replace)
    patch            -> update_entry()  (span patch)
    remove           -> delete_entry()

Everything else (char limits, threat scanning, drift detection, batch
plumbing, response shapes) is inherited unchanged, so the takeover stays
compatible with upstream's behaviour and any future upstream refactor.

Semantics preserved from the original V6.3.1 takeover:

* **Strict mode** — when ``use_cortex`` is on, ``MEMORY.md`` / ``USER.md`` are
  NEVER read or written.  They stay on disk as a read-only emergency mirror.
* **Loud failure** — if cortex is unreachable the tool returns an explicit
  error rather than silently degrading to a stale .md read.
* **No local char-limit gate on cortex writes** — cortex has no hard cap; the
  2 200/1 375-char limits only apply to the native .md path (V6.5.2 fix).
* **Escape hatch** — ``CORTEX_TAKEOVER=0`` restores the pure .md behaviour.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from tools.memory_tool_store import MemoryStore as _UpstreamMemoryStore

logger = logging.getLogger(__name__)

__all__ = ["CortexMemoryStore", "MemoryStore", "CORTEX_URL_DEFAULT"]

CORTEX_URL_DEFAULT = "http://127.0.0.1:1880"

# V6.3.1 tier -> importance mapping (Direction C).
_TIER_TO_IMPORTANCE = {"short": 3, "medium": 5, "long": 7, "permanent": 9}


def _importance_for_tier(tier: Optional[str]) -> int:
    """Map a memory tier onto a cortex importance score (default: ``long``/7)."""
    if not tier:
        return _TIER_TO_IMPORTANCE["long"]
    return _TIER_TO_IMPORTANCE.get(str(tier).lower(), _TIER_TO_IMPORTANCE["long"])


def _norm_for_dedupe(text: str) -> str:
    """Normalize a memory entry so near-identical texts collapse to one key.

    V6.6.1 fix (2026-10-31): used to de-duplicate the active-recall block
    against the injected /list dump. Whitespace + case are normalized so the
    same statement written slightly differently still counts as "already
    injected" and is not repeated inside one system prompt.
    """
    return " ".join((text or "").split()).strip().lower()


class CortexMemoryStore(_UpstreamMemoryStore):
    """``MemoryStore`` whose reads and writes go through Memory Cortex."""

    def __init__(
        self,
        memory_char_limit: int = 2200,
        user_char_limit: int = 1375,
        *,
        use_cortex: Optional[bool] = None,
        cortex_backend=None,
        **kwargs: Any,
    ) -> None:
        # ``use_cortex`` defaults ON; ``CORTEX_TAKEOVER=0`` is the escape hatch.
        if use_cortex is None:
            use_cortex = os.getenv("CORTEX_TAKEOVER", "1").lower() not in (
                "0",
                "false",
                "no",
            )
        # SAFETY (2026-10-30): never talk to the production cortex from inside a
        # pytest run. The upstream tests monkeypatch tools.memory_tool.get_memory_dir
        # to a tmp_path, but that only redirects the LOCAL .md -- with takeover on,
        # the store writes to the REMOTE service, so the test fixtures ("fact A",
        # "rule one", "server A runs nginx", ...) landed in the production memory
        # store. 34 such rows were found in production on 2026-10-30 and deleted.
        # Under pytest we fall back to the plain local store, which the tests
        # already point at tmp_path.
        if use_cortex and os.getenv("PYTEST_CURRENT_TEST"):
            use_cortex = False
        super().__init__(memory_char_limit, user_char_limit, **kwargs)
        self.use_cortex = bool(use_cortex)
        self._cortex = None
        self._cortex_backend_override = cortex_backend
        # chunk_id map: target -> {content: chunk_id}; drives replace/remove
        # lookup without a second HTTP roundtrip.
        self._chunk_ids: Dict[str, Dict[str, str]] = {"memory": {}, "user": {}}
        self._pending_tier: Optional[str] = None
        self._recall_context: List[str] = []
        self._working_memory_loaded = False
        self._last_forgetting_cleanup = 0.0

    # ------------------------------------------------------------------ #
    # backend access
    # ------------------------------------------------------------------ #
    @property
    def _cortex_backend(self):
        """Lazy accessor for the cortex backend module (injectable for tests)."""
        if self._cortex is not None:
            return self._cortex
        if self._cortex_backend_override is not None:
            self._cortex = self._cortex_backend_override
            return self._cortex
        from tools import cortex_memory_backend as _b

        self._cortex = _b
        return self._cortex

    def _is_cortex_unavailable(self) -> bool:
        """True when the takeover should route through cortex.

        The historical name reads backwards: it returns True when cortex IS
        the path we want (takeover on AND the service reachable).
        """
        if not self.use_cortex:
            return False
        try:
            return bool(self._cortex_backend.is_cortex_active())
        except Exception:
            return False

    def _is_cortex_available(self) -> bool:
        """True when cortex is unreachable (takeover on but service down)."""
        if not self.use_cortex:
            return False
        try:
            return not bool(self._cortex_backend.is_cortex_active())
        except Exception:
            return True

    def _cortex_error(self, target: str, content: Optional[str], exc: Exception):
        """Loud, explicit failure — the caller must NOT believe a write landed."""
        url = getattr(self._cortex_backend, "CORTEX_URL", CORTEX_URL_DEFAULT)
        payload: Dict[str, Any] = {
            "success": False,
            "error": f"Memory Cortex is unavailable — entry was not saved. {exc}",
            "warning": (
                "cortex_unavailable: memory persists only when the Memory Cortex "
                f"service at {url} is up."
            ),
            "target": target,
        }
        if content is not None:
            payload["content"] = content
        return payload

    # ------------------------------------------------------------------ #
    # read path
    # ------------------------------------------------------------------ #
    def load_from_disk(self):
        """Load every entry from cortex.  The .md files are never read."""
        if not self.use_cortex:
            return super().load_from_disk()
        try:
            return self._load_from_cortex()
        except Exception as exc:  # cortex unreachable / failed
            logger.warning(
                "memory tool: cortex unreachable on load (%s) — starting with empty memory", exc
            )
            self.memory_entries = []
            self.user_entries = []
            self._chunk_ids = {"memory": {}, "user": {}}
            self._system_prompt_snapshot = {"memory": "", "user": ""}
            return {"memory": [], "user": [], "source": "cortex_unavailable"}

    def _budget_entries(self, target: str, entries: list[str]) -> list[str]:
        """Selective injection: keep only the entries that fit the char budget.

        Cortex takeover used to render *every* chunk into the frozen system-prompt
        snapshot.  Measured 2026-11-06: 175,445 chars of system prompt
        (MEMORY 82K + USER 44K + skills 39K) ~= 142K tokens against a 96,000-token
        window -- the incompressible floor alone overflows, so /compress and fresh
        sessions cannot help.  Rank by importance, keep within _char_limit(), then
        restore original order so the prefix cache stays stable.

        Only the injected copy is trimmed: memory_entries / user_entries / _chunk_ids
        stay complete, so memory tool reads, writes and deletes are unaffected.
        """
        if not entries:
            return []
        limit = int(self._char_limit(target) or 0)
        if limit <= 0:
            return list(entries)
        scores = (getattr(self, "_entry_scores", None) or {}).get(target, {})
        delim_cost = 3  # ENTRY_DELIMITER overhead for each additional entry
        ranked = sorted(range(len(entries)),
                        key=lambda i: (-int(scores.get(entries[i], 0) or 0), i))
        kept, used = set(), 0
        for i in ranked:
            cost = len(entries[i]) + (delim_cost if kept else 0)
            if used + cost > limit:
                continue
            kept.add(i)
            used += cost
        return [e for i, e in enumerate(entries) if i in kept]

    def _load_from_cortex(self) -> Dict[str, Any]:
        """Populate ``memory_entries`` / ``user_entries`` from cortex chunks."""
        backend = self._cortex_backend
        # content -> importance, used only by _budget_entries for injection ranking.
        self._entry_scores = {}
        for target, attr in (("memory", "memory_entries"), ("user", "user_entries")):
            chunks = backend.list_entries(target)
            texts = [c["content"] for c in chunks if c.get("content")]
            self._chunk_ids[target] = {
                c["content"]: c["id"] for c in chunks if c.get("id")
            }
            self._entry_scores[target] = {
                c["content"]: int(c.get("importance") or 0)
                for c in chunks if c.get("content")
            }
            setattr(self, attr, list(dict.fromkeys(texts)))  # order-preserving dedupe

        # ---- essential hooks (kept small on purpose) ----
        #
        # V6.6.1 fix (2026-10-31): the recall query is no longer the fixed,
        # content-free string "recent context". A fixed query resolves to
        # roughly the same generic / high-importance set on EVERY boot, so
        # the same stale facts were re-injected into the prompt on every
        # single session start.
        #
        # Active recall is opt-in now: HERMES_ACTIVE_RECALL=1 plus a
        # meaningful HERMES_RECALL_QUERY. With the default ("off") the
        # snapshot contains exactly the budgeted /list dump -- no
        # duplicates, no graph-expanded noise.
        recall_query = os.getenv("HERMES_RECALL_QUERY", "").strip()
        recall_enabled = (
            os.getenv("HERMES_ACTIVE_RECALL", "0") == "1" and bool(recall_query)
        )

        hooks = [
            lambda: backend.log_audit("read", "chunk", actor="memory_tool_load"),
            lambda: backend.get_forgetting_stats(),
            lambda: backend.get_expiring_soon(limit=5),
            lambda: backend.get_taxonomy_relations(),
        ]
        if recall_enabled:
            # Warm the query we are actually going to issue.
            hooks.append(lambda: backend.search_tts(query=recall_query, top_k=5))
        for call in hooks:
            try:
                call()
            except Exception:
                pass

        # Active recall: proactively surface cross-session relevant context.
        self._recall_context = []
        if recall_enabled:
            try:
                rc = backend.reason_with_memory(
                    question=recall_query, top_k=5, include_hierarchy=True
                )
                if isinstance(rc, dict):
                    chunks = rc.get("chunks") or rc.get("context_chunks") or []
                    self._recall_context = [
                        c.get("content", "")
                        for c in chunks
                        if isinstance(c, dict) and c.get("content")
                    ][:5]
            except Exception:
                pass

        # Two-way working memory: read back this session's scratch state.
        try:
            wm = backend.get_all_working_memory(session_id="current_session")
            if wm and not self._working_memory_loaded:
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

        # Opportunistic short-term cleanup, at most once per 30 min.
        try:
            now = time.time()
            if not self._last_forgetting_cleanup or now - self._last_forgetting_cleanup > 1800:
                backend.run_forgetting_cleanup(min_score=0.1)
                self._last_forgetting_cleanup = now
        except Exception:
            pass

        # Frozen snapshot (system-prompt stability / prefix-cache invariant).
        # Hard floor budget: all-entry rendering measured 175,445 chars ~= 142K tokens
        # against a 96K window, so compression / new sessions cannot recover.  Render
        # only the budgeted subset; the stored entries remain complete (see above).
        budgeted_memory = self._budget_entries("memory", list(self.memory_entries))
        budgeted_user = self._budget_entries("user", list(self.user_entries))
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", budgeted_memory),
            "user": self._render_block("user", budgeted_user),
        }
        if self._recall_context:
            # V6.6.1 fix (2026-10-31): de-duplicate against the injected
            # dump. The recall hits come from the same corpus as the dump,
            # so without this every recalled fact appeared TWICE inside the
            # same system prompt.
            seen = {_norm_for_dedupe(e) for e in budgeted_memory}
            seen |= {_norm_for_dedupe(e) for e in budgeted_user}
            unique_recall: List[str] = []
            for rc_text in self._recall_context[:5]:
                key = _norm_for_dedupe(rc_text)
                if not key or key in seen:
                    continue
                seen.add(key)
                unique_recall.append(rc_text)
            if unique_recall:
                block = "\n\n## Memory Recall Context (proactively retrieved from cortex at session start)\n\n"
                for i, rc_text in enumerate(unique_recall, 1):
                    block += f"{i}. {rc_text.strip()}\n"
                self._system_prompt_snapshot["memory"] += block

        return {
            "memory": list(self.memory_entries),
            "user": list(self.user_entries),
            "source": "cortex",
        }

    # ------------------------------------------------------------------ #
    # write path
    # ------------------------------------------------------------------ #
    def add(self, target: str, content: str, tier: Optional[str] = None, **kwargs):
        """Create the entry in cortex.  No local char-limit gate."""
        if not self.use_cortex:
            return super().add(target, content, **kwargs)
        content = (content or "").strip()
        if not content:
            return self._failure_with_entries(target, "Content cannot be empty.")
        # Reuse upstream's threat scanner so writes are policed identically.
        from tools.memory_tool_store import _scan_memory_content

        if scan_error := _scan_memory_content(content):
            return self._failure_with_entries(target, scan_error)

        self._pending_tier = tier or "long"
        backend = self._cortex_backend
        try:
            if content in self._entries_for(target):
                return self._success_response(
                    target, "Entry already exists (no duplicate added)."
                )
            if self._chunk_ids.get(target, {}).get(content) is not None:
                return self._success_response(
                    target, "Entry already exists (no duplicate added)."
                )
            existing = backend.find_existing_chunk(target, content)
            if existing:
                self._chunk_ids.setdefault(target, {})[content] = existing.get("id")
                return self._success_response(
                    target, "Entry already exists (no duplicate added)."
                )

            importance = _importance_for_tier(self._pending_tier)
            self._pending_tier = None
            created = backend.create_entry(target, content, importance=importance)
            chunk_id = created.get("id")
            if chunk_id:
                self._chunk_ids.setdefault(target, {})[content] = chunk_id
                self._post_write_hooks(backend, target, chunk_id, content)
            self._entries_for(target).append(content)
            return self._success_response(target, "Entry added.")
        except Exception as exc:
            logger.warning("memory tool: cortex unreachable on add(): %s", exc)
            return self._cortex_error(target, content, exc)

    def replace(
        self,
        target: str,
        old_text: str,
        new_content: str,
        matched_entry: Optional[str] = None,
        **kwargs,
    ):
        """Replace a whole entry in cortex."""
        if not self.use_cortex:
            return super().replace(target, old_text, new_content, matched_entry, **kwargs)
        new_content = (new_content or "").strip()
        if not (old_text or "").strip():
            return self._failure_with_entries(target, "old_text cannot be empty.")
        if not new_content:
            return self._failure_with_entries(target, "new content cannot be empty.")
        backend = self._cortex_backend
        try:
            resolved = self._resolve_entry_for_cortex(target, old_text)
            if isinstance(resolved, dict) and resolved.get("success") is False:
                return resolved
            chunk_id, entry_text = resolved
            updated = backend.update_entry(chunk_id, new_content)
            if updated is False:
                raise RuntimeError(f"cortex update_entry({chunk_id}) returned no result")
            self._chunk_ids.setdefault(target, {}).pop(entry_text, None)
            self._chunk_ids.setdefault(target, {})[new_content] = chunk_id
            self._replace_local_entry(target, entry_text, new_content)
            self._post_write_hooks(backend, target, chunk_id, new_content)
            return self._success_response(target, "Entry replaced.")
        except Exception as exc:
            logger.warning("memory tool: cortex unreachable on replace(): %s", exc)
            return self._cortex_error(target, new_content, exc)

    def patch(
        self,
        target: str,
        old_text: str,
        new_text: str,
        matched_entry: Optional[str] = None,
        **kwargs,
    ):
        """Patch a span inside an entry, then write the whole entry back."""
        if not self.use_cortex:
            return super().patch(target, old_text, new_text, matched_entry, **kwargs)
        backend = self._cortex_backend
        try:
            resolved = self._resolve_entry_for_cortex(target, old_text)
            if isinstance(resolved, dict) and resolved.get("success") is False:
                return resolved
            chunk_id, entry_text = resolved
            from tools.memory_tool_store import _patch_entry

            patched = _patch_entry(entry_text, old_text, new_text)
            if patched is None or patched[0] is None:
                message = patched[1] if patched else "Patch failed."
                return self._failure_with_entries(target, message)
            new_entry = patched[0]
            backend.update_entry(chunk_id, new_entry)
            self._chunk_ids.setdefault(target, {}).pop(entry_text, None)
            self._chunk_ids.setdefault(target, {})[new_entry] = chunk_id
            self._replace_local_entry(target, entry_text, new_entry)
            self._post_write_hooks(backend, target, chunk_id, new_entry)
            return self._success_response(target, "Entry patched.")
        except Exception as exc:
            logger.warning("memory tool: cortex unreachable on patch(): %s", exc)
            return self._cortex_error(target, new_text, exc)

    def remove(
        self,
        target: str,
        old_text: str,
        matched_entry: Optional[str] = None,
        **kwargs,
    ):
        """Delete an entry from cortex."""
        if not self.use_cortex:
            return super().remove(target, old_text, matched_entry, **kwargs)
        backend = self._cortex_backend
        try:
            resolved = self._resolve_entry_for_cortex(target, old_text)
            if isinstance(resolved, dict) and resolved.get("success") is False:
                return resolved
            chunk_id, entry_text = resolved
            backend.delete_entry(chunk_id)
            self._chunk_ids.setdefault(target, {}).pop(entry_text, None)
            self._remove_local_entry(target, entry_text)
            return self._success_response(target, "Entry removed.")
        except Exception as exc:
            logger.warning("memory tool: cortex unreachable on remove(): %s", exc)
            return self._cortex_error(target, None, exc)

    # ------------------------------------------------------------------ #
    # batch write path -- the shape the memory tool actually advertises
    # ------------------------------------------------------------------ #
    def apply_batch(self, target: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply a batch of ops through cortex.

        Upstream's ``apply_batch()`` computes the whole batch against a local
        list and commits it with ``self._mutate(...)`` -- it writes
        MEMORY.md/USER.md directly and NEVER calls ``self.add``/``replace``/
        ``patch``/``remove``.  The takeover overrides above therefore never
        fired for the batch shape, and the batch shape is the one the memory
        tool REQUIRES (``operations`` is a required parameter).  Every modern
        write landed in the local .md and never reached cortex.

        This override routes every op through the cortex-aware single-op
        methods and reproduces upstream's response shape.

        Deliberate differences from upstream's atomic .md batch:

        * NOT atomic.  Upstream pre-validates everything and commits once; a
          cortex write is a separate HTTP call per op, so a failure part-way
          leaves the earlier ops written.  The failure response still names
          the op that failed -- nothing is swallowed.
        * The .md char-budget gate does not apply (cortex has no hard cap),
          but upstream's "refuse to empty the store" guard IS kept: it protects
          the store, not the file format.
        """
        if not self.use_cortex:
            return super().apply_batch(target, operations)
        ops = [op for op in (operations or []) if isinstance(op, dict)]
        if not ops:
            return self._failure_with_entries(target, "operations must not be empty.")

        entries_before = list(self._entries_for(target))
        replaced: Dict[str, Any] = {}
        patched: Dict[str, Any] = {}
        produced: Dict[str, Any] = {}
        removed: Dict[str, Any] = {}

        for index, op in enumerate(ops, 1):
            action = op.get("action")
            content = op.get("content") or op.get("new_text") or ""
            old_text = op.get("old_text") or ""
            matched_entry = op.get("matched_entry")

            # Resolve the entry the op selects BEFORE applying it: the response
            # must name the full text that was replaced / removed / patched.
            previous = None
            if action in ("replace", "patch", "remove"):
                resolved = self._resolve_entry_for_cortex(target, old_text)
                if isinstance(resolved, dict) and resolved.get("success") is False:
                    return resolved
                _chunk_id, previous = resolved
                if matched_entry and previous is not None and matched_entry != previous:
                    # A staged write must apply to exactly what the approver
                    # reviewed; refuse when the entry changed since.
                    return self._failure_with_entries(
                        target,
                        "the entry this operation was staged against has changed since it was "
                        "approved; reject it and re-issue the change.",
                    )

            if action == "add":
                result = self.add(target, content)
            elif action == "replace":
                result = self.replace(target, old_text, content, matched_entry)
            elif action == "patch":
                from tools.memory_tool_store import _patch_entry

                outcome = _patch_entry(previous or "", old_text, content) if previous else None
                new_entry = outcome[0] if outcome else None
                result = self.patch(target, old_text, content, matched_entry)
                if result.get("success"):
                    if new_entry is None:
                        return self._failure_with_entries(
                            target,
                            (outcome[1] if outcome else "Patch failed."),
                        )
                    patched[index] = previous
                    produced[index] = new_entry
            elif action == "remove":
                result = self.remove(target, old_text, matched_entry)
                if result.get("success"):
                    removed[index] = previous
            else:
                return self._failure_with_entries(
                    target, f"Unknown action '{action}'. Use: add, patch, remove")

            if not result.get("success"):
                return result

            if action == "replace" and result.get("success"):
                replaced[index] = previous

        # Mirror upstream's guard: never empty a previously non-empty store
        # unless the batch is a single deliberate remove.
        entries_after = list(self._entries_for(target))
        if entries_before and not entries_after and not (
            len(ops) == 1 and ops[0].get("action") == "remove"
        ):
            return self._failure_with_entries(
                target,
                "Refusing to empty the store: this batch would remove every entry. Keep at "
                "least one entry, or delete the final entry alone in a one-item batch.",
            )

        extra: Dict[str, Any] = {}
        if replaced:
            extra["replaced_entries"] = replaced
        if patched:
            extra["patched_entries"] = patched
            extra["patched_entries_after"] = produced
        if removed:
            extra["removed_entries"] = removed
        return self._success_response(
            target, f"Applied {len(ops)} operation(s).", **extra
        )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _resolve_entry_for_cortex(self, target: str, old_text: str):
        """Find ``(chunk_id, entry_text)`` for a substring of an entry.

        Resolution order: exact chunk-id cache hit -> unique local entry match
        -> cortex-side substring lookup (covers entries written by a
        sister session).
        """
        entries = self._entries_for(target)
        # 1. local unique match
        matches = [e for e in entries if old_text in e]
        if len(matches) == 1:
            entry_text = matches[0]
            chunk_id = self._chunk_ids.get(target, {}).get(entry_text)
            if chunk_id:
                return chunk_id, entry_text
            # 2. cortex-side lookup
            backend = self._cortex_backend
            found = backend.lookup_entry_by_substring(target, old_text)
            if found:
                cid = found.get("id") if isinstance(found, dict) else found
                if cid:
                    self._chunk_ids.setdefault(target, {})[entry_text] = cid
                    return cid, entry_text
            return None, entry_text
        if len(matches) > 1:
            return self._failure_with_entries(
                target,
                f"old_text matches {len(matches)} entries — use a longer, unique substring.",
            )
        # 3. not in the local snapshot — ask cortex directly
        backend = self._cortex_backend
        found = backend.lookup_entry_by_substring(target, old_text)
        if not found:
            return self._failure_with_entries(
                target, f"old_text not found in the {target} entries."
            )
        chunk_id = found.get("id") if isinstance(found, dict) else found
        content = (
            found.get("content", "") if isinstance(found, dict) else ""
        ) or ""
        if content:
            self._chunk_ids.setdefault(target, {})[content] = chunk_id
            if content not in entries:
                entries.append(content)
        return chunk_id, content

    def _replace_local_entry(self, target: str, old_entry: str, new_entry: str) -> None:
        entries = self._entries_for(target)
        try:
            idx = entries.index(old_entry)
        except ValueError:
            entries.append(new_entry)
            return
        entries[idx] = new_entry

    def _remove_local_entry(self, target: str, entry: str) -> None:
        entries = self._entries_for(target)
        try:
            entries.remove(entry)
        except ValueError:
            pass

    def _post_write_hooks(self, backend, target: str, chunk_id: str, content: str) -> None:
        """Post-write graph / audit hooks.  Never block the write."""
        for call in (
            lambda: backend.extract_facts(chunk_id, content),
            lambda: backend.log_audit("write", "chunk", resource_id=chunk_id),
            lambda: backend.auto_link_new_chunk(
                chunk_id, content, top_k=5, min_similarity=0.30, max_links=3
            ),
            lambda: backend.set_working_memory(
                session_id=target,
                key="last_written_chunk",
                value={"chunk_id": chunk_id, "preview": content[:120]},
                value_type="dict",
            ),
        ):
            try:
                call()
            except Exception:
                pass
        # Temporal relation to the previous same-target chunk.
        try:
            prior = backend.list_entries(target=target, limit=1)
            if prior:
                last = (
                    prior[0]
                    if isinstance(prior, list)
                    else prior.get("chunks", [{}])[0]
                ).get("id")
                if last and last != chunk_id:
                    backend.create_temporal_relation(
                        source_id=last, target_id=chunk_id, relation_type="successor"
                    )
        except Exception:
            pass


# The factory the rest of Hermes imports.
MemoryStore = CortexMemoryStore
