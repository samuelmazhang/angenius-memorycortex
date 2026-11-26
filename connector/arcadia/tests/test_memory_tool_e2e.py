"""End-to-end test for the arcadia ↔ cortex memory_tool (V5.3 agentic recall).

Runs the ACTUAL `tools.memory_tool` (arcadia-side connector) against a live
cortex with all 8 actions, plus the upstream disk read-safety invariants.

WHY: this regression test exists because a wholesale upgrade to the recall
memory_tool silently lost two things — (1) a broken dispatch indentation that
made every non-`remove` action raise `UnboundLocalError: result`, and (2) the
upstream "unreadable file must refuse & preserve" safety invariants. Both were
only caught by running the tool end-to-end.

HOW TO RUN (in the arcadia venv, so `tools.registry`/`tools.memory_tool` and
deps resolve):

    cd /a/Application/Arcadia-Agent/Program/arcadia-agent
    ./venv/Scripts/python.exe -m pytest \
        ./connector/arcadia/tests/test_memory_tool_e2e.py \
        -p no:cacheprovider -q

Env:
  MEMORY_CORTEX_URL  default http://127.0.0.1:1880 (cortex service)
  CORTEX_TAKEOVER    1 (default) → recall uses cortex; set 0 to force disk path
  for the safety tests.

The e2e test monkeypatches `tools.memory_tool.get_memory_dir` to an isolated
temp dir so it never writes to your real arcadia memory.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Make the arcadia-agent package importable if we're run from an arbitrary cwd.
sys.path.insert(0, r"A:\Application\Arcadia-Agent\Program\arcadia-agent")

os.environ.setdefault("MEMORY_CORTEX_URL", "http://127.0.0.1:1880")
os.environ.setdefault("MEMORY_CORTEX_TIMEOUT", "15")
os.environ.setdefault("MEMORY_CORTEX_RETRIES", "2")

import tools.memory_tool as mt          # noqa: E402
from tools.registry import registry      # noqa: E402


@pytest.fixture()
def isolated_store(tmp_path):
    """A MemoryStore bound to an isolated temp dir (never touches real memory)."""
    # get_memory_dir() resolves via get_arcadia_home()/memories — it does NOT read
    # an env var, so we must monkeypatch the module function to isolate.
    mt.get_memory_dir = lambda: tmp_path
    return mt.load_on_disk_store()


@pytest.fixture(scope="module")
def mark():
    import uuid
    return f"e2e-memtool-{uuid.uuid4().hex[:8]}"


# ============================================================================
# 1) Tool registration + schema surface (must not regress)
# ============================================================================
class TestRegistration:
    def test_memory_tool_registered(self):
        t = registry.get_entry("memory")
        assert t is not None and t.name == "memory"

    def test_all_eight_actions_in_schema(self):
        enum = mt.MEMORY_SCHEMA["parameters"]["properties"]["action"]["enum"]
        assert enum == [
            "add", "replace", "remove", "recall",
            "save_procedure", "get_procedure", "deep_context", "audit_query",
        ]

    def test_dynamic_schema_attached(self):
        t = registry.get_entry("memory")
        assert t.dynamic_schema_overrides is not None


# ============================================================================
# 2) Write-path actions against live cortex (takeover on)
# ============================================================================
class TestWriteActions:
    def test_add(self, isolated_store, mark):
        r = mt.memory_tool(action="add", target="memory", content=mark, store=isolated_store)
        assert r.startswith('{"success"') and '"done": true' in r

    def test_recall_hybrid_graph(self, isolated_store):
        r = mt.memory_tool(action="recall", content="arcadia venv fingerprint", store=isolated_store)
        assert r.startswith('{"success"')
        assert '"hits"' in r

    def test_replace(self, isolated_store, mark):
        r = mt.memory_tool(action="replace", target="memory",
                           old_text=mark, content=mark + "-r", store=isolated_store)
        assert r.startswith('{"success"')

    def test_remove(self, isolated_store, mark):
        r = mt.memory_tool(action="remove", target="memory",
                           old_text=mark + "-r", store=isolated_store)
        assert r.startswith('{"success"')

    def test_batch(self, isolated_store, mark):
        r = mt.memory_tool(operations=[
            {"action": "add", "target": "memory", "content": mark + "-b1"},
            {"action": "add", "target": "memory", "content": mark + "-b2"},
        ], store=isolated_store)
        assert r.startswith('{"success"')

    def test_unknown_action_errors(self, isolated_store):
        r = mt.memory_tool(action="bogus", store=isolated_store)
        assert '"error"' in r and "Unknown action" in r


# ============================================================================
# 3) Procedural memory save/get round-trip
# ============================================================================
class TestProcedural:
    def test_save_and_get_procedure_roundtrip(self, isolated_store, mark):
        name = f"e2e_proc_{mark[-8:]}"
        trigger = f"e2e trigger {mark}"
        # save_procedure uses target as the procedure NAME (guard relaxed for it)
        r = mt.memory_tool(action="save_procedure", target=name,
                           old_text=trigger, content="stepA; stepB", store=isolated_store)
        assert r.startswith('{"success"') and "procedure_id" in r
        r2 = mt.memory_tool(action="get_procedure", content=trigger, store=isolated_store)
        assert r2.startswith('{"success"') and name in r2

    def test_add_target_guard_still_enforced(self, isolated_store):
        # only save_procedure gets the relaxed target; add still requires memory/user
        r = mt.memory_tool(action="add", target="bogus_t", content="x", store=isolated_store)
        assert '"error"' in r and "Invalid target" in r


# ============================================================================
# 4) Read-only / graph actions
# ============================================================================
class TestReadOnlyAndGraph:
    def test_deep_context(self, isolated_store):
        import json
        import urllib.request
        base = os.environ["MEMORY_CORTEX_URL"]
        req = urllib.request.Request(base + "/search",
            data=json.dumps({"query": "arcadia venv", "limit": 1}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        cid = json.loads(urllib.request.urlopen(req, timeout=15).read())["results"][0]["id"]
        r = mt.memory_tool(action="deep_context", old_text=cid, store=isolated_store)
        assert r.startswith('{"success"') and '"context"' in r

    def test_audit_query(self, isolated_store):
        r = mt.memory_tool(action="audit_query", store=isolated_store)
        assert r.startswith('{"success"') and '"entries"' in r


# ============================================================================
# 5) Upstream disk read-safety invariants (native/disk path, takeover=0)
#    These are the 3 that regressed in V5.3.5 and were fixed in V5.3.6.
# ============================================================================
class TestUnreadableFilePreservation:
    def _force_disk(self):
        """Return a store forced to the native disk path (no cortex)."""
        os.environ["CORTEX_TAKEOVER"] = "0"

    def test_add_refuses_and_preserves_on_unreadable(self, tmp_path):
        self._force_disk()
        mt.get_memory_dir = lambda: tmp_path
        store = mt.load_on_disk_store()
        store.add("memory", "good entry")
        mem_path = tmp_path / "MEMORY.md"
        corrupt = b"\xff\xfe invalid utf-8 \x80\x81"
        mem_path.write_bytes(corrupt)
        res = store.add("memory", "new entry")
        assert res["success"] is False and "could not be read" in res.get("error", "")
        assert mem_path.read_bytes() == corrupt  # preserved

    def test_replace_refuses_and_preserves(self, tmp_path):
        self._force_disk()
        mt.get_memory_dir = lambda: tmp_path
        store = mt.load_on_disk_store()
        store.add("memory", "good entry")
        mem_path = tmp_path / "MEMORY.md"
        corrupt = b"\xff\xfe invalid utf-8 \x80\x81"
        mem_path.write_bytes(corrupt)
        res = store.replace("memory", "good", "replacement")
        assert res["success"] is False and "could not be read" in res.get("error", "")
        assert mem_path.read_bytes() == corrupt

    def test_remove_refuses_and_preserves(self, tmp_path):
        self._force_disk()
        mt.get_memory_dir = lambda: tmp_path
        store = mt.load_on_disk_store()
        store.add("memory", "good entry")
        mem_path = tmp_path / "MEMORY.md"
        corrupt = b"\xff\xfe invalid utf-8 \x80\x81"
        mem_path.write_bytes(corrupt)
        res = store.remove("memory", "good")
        assert res["success"] is False and "could not be read" in res.get("error", "")
        assert mem_path.read_bytes() == corrupt

    def test_mutation_reads_file_exactly_once(self, tmp_path, monkeypatch):
        self._force_disk()
        mt.get_memory_dir = lambda: tmp_path
        store = mt.load_on_disk_store()
        store.add("memory", "Only entry.")
        mem_path = tmp_path / "MEMORY.md"

        real = Path.read_text
        counts = {"n": 0}
        def counting(self, *a, **k):
            if self == mem_path:
                counts["n"] += 1
            return real(self, *a, **k)
        monkeypatch.setattr(Path, "read_text", counting)
        result = store.replace("memory", "Only entry", "Replaced entry.")
        assert result["success"] is True
        assert counts["n"] == 1
