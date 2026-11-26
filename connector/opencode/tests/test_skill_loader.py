"""
TDD — connector/opencode/opencode_cortex_loader.py

覆盖:
  - get_cortex_tools() 返回 actions schema list
  - 每个 action 有 name / description / params
  - load_skill_metadata() 解析 SKILL.md frontmatter
  - demo_smoke.py 能 import 6 个 action

loader 目标 ≤ 200 行 (AC).
"""

import os
import sys
import json
import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
_loader_path = os.path.join(_HERE, '..', 'opencode_cortex_loader.py')

_spec = importlib.util.spec_from_file_location('opencode_cortex_loader', _loader_path)
loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loader)


# ============ get_cortex_tools ============

def test_get_cortex_tools_returns_list():
    tools = loader.get_cortex_tools()
    assert isinstance(tools, list)
    assert len(tools) >= 6  # 6 actions


def test_each_tool_has_required_fields():
    required = {'name', 'description', 'params'}
    for t in loader.get_cortex_tools():
        assert required.issubset(t.keys()), f"missing fields in {t.get('name')}"
        assert isinstance(t['name'], str) and t['name']
        assert isinstance(t['description'], str) and t['description']
        assert isinstance(t['params'], dict)


def test_known_actions_present():
    names = {t['name'] for t in loader.get_cortex_tools()}
    expected = {'remember', 'recall', 'forget', 'search', 'audit_query', 'introspect'}
    assert expected.issubset(names), f"missing: {expected - names}"


def test_remember_params_schema():
    tools = {t['name']: t for t in loader.get_cortex_tools()}
    r = tools['remember']
    assert 'content' in r['params']
    assert r['params']['content'].get('required') is True
    assert 'tags' in r['params']
    assert 'importance' in r['params']


# ============ load_skill_metadata ============

def test_load_skill_metadata_returns_dict():
    meta = loader.load_skill_metadata()
    assert isinstance(meta, dict)
    assert meta.get('name') == 'cortex-memory'
    assert 'description' in meta
    assert 'version' in meta


def test_load_skill_metadata_version_matches_connector():
    meta = loader.load_skill_metadata()
    assert meta['version'].startswith('1.')  # V1.x


# ============ build_action_invoker ============

def test_build_invoker_dispatches_remember():
    invoker = loader.build_invoker()
    assert 'remember' in invoker
    # Should be callable, accept kwargs
    fake = {'id': 'x', 'content': 'hi'}
    invoker['remember'].tool = lambda **kw: fake
    assert invoker['remember'](content='hi') == fake


# ============ demo_smoke import ============

def test_demo_smoke_importable():
    demo_path = os.path.join(_HERE, '..', 'demo_smoke.py')
    if os.path.exists(demo_path):
        spec = importlib.util.spec_from_file_location('demo_smoke', demo_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, 'main')


# ============ SKILL.md frontmatter parseable ============

def test_skill_md_has_valid_frontmatter():
    md_path = os.path.join(_HERE, '..', 'SKILL.md')
    assert os.path.exists(md_path), "SKILL.md missing"
    with open(md_path, encoding='utf-8') as f:
        content = f.read()
    assert content.startswith('---'), "no YAML frontmatter"
    parts = content.split('---', 2)
    assert len(parts) >= 3, "malformed frontmatter"
    fm = parts[1].strip()
    # Must contain name, description, version
    assert 'name:' in fm
    assert 'description:' in fm
    assert 'version:' in fm