"""Regression tests for V5.3 architecture and formal provider contract."""
import importlib.util
import json
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin_package" / "memory_cortex" / "__init__.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("memory_cortex_plugin_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_manifest_uses_current_provider_contract():
    text = (ROOT / "plugin_package/memory_cortex/plugin.yaml").read_text(encoding="utf-8")
    assert "name: memory-cortex" in text
    assert "PostgreSQL/pgvector" in text
    assert "cortex_search" in text
    assert "cortex_health" in text


def test_plugin_uses_configured_remote_url(monkeypatch):
    monkeypatch.setenv("MEMORY_CORTEX_URL", "http://test-cortex:1880")
    module = load_plugin()
    assert module.CORTEX_URL == "http://test-cortex:1880"
    assert module._cortex_url("/health") == "http://test-cortex:1880/health"


def test_plugin_module_has_single_write_guard():
    text = PLUGIN.read_text(encoding="utf-8")
    assert "takeover owns persistence" in text
    assert '"/chunks"' not in text[text.find("def on_memory_write"):]


def test_current_docs_do_not_claim_legacy_storage():
    text = (ROOT / "docs/架构总览.md").read_text(encoding="utf-8")
    assert "PostgreSQL" in text
    assert "pgvector" in text
    assert "SQLite、Chroma" in text
