"""Guard against annotation-only NameErrors in the server modules.

The lesson (2026-10-31): a new helper in api_pg.py used
``List[Tuple[str, str, str, float]]`` in its signature but ``Tuple``
was not imported. The container runs Python 3.10, where module-level
annotations are EVALUATED at definition time, so the module raised

    NameError: name 'Tuple' is not defined

on import and the container crash-looped on startup.

Neither ``py_compile`` nor the local test suite caught it, because the
local dev interpreter is Python 3.14, where PEP 649 makes annotations
lazy by default — the name is never looked up.

So this test does what the compiler does not: it parses each server
module, collects every name used inside an annotation, and asserts the
name is actually available. It covers the whole ``server/src`` tree,
not just api_pg.py, because the same trap exists everywhere.
"""
from __future__ import annotations

import ast
import builtins
import os
import unittest

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(SERVER_ROOT, "src")

# Names that may appear in an annotation without being imported: they
# are builtins, or they are defined in the same module.
_BUILTIN_OK = set(dir(builtins))

# Typing helpers people reach for in annotations. If one of these shows
# up in an annotation it MUST be imported by the module.
_TYPING_NAMES = {
    "Any", "Callable", "Dict", "FrozenSet", "Generator", "Iterable",
    "Iterator", "List", "Mapping", "Optional", "Sequence", "Set",
    "Tuple", "Union",
}


def _module_names(tree: ast.Module) -> set:
    """Names bound at module level (imports, defs, classes, assignments)."""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Try):
            # try: import X except ImportError: X = None
            for sub in node.body:
                if isinstance(sub, ast.Import):
                    for a in sub.names:
                        names.add(a.asname or a.name.split(".")[0])
                elif isinstance(sub, ast.ImportFrom):
                    for a in sub.names:
                        names.add(a.asname or a.name)
            for handler in node.handlers:
                for sub in handler.body:
                    if isinstance(sub, ast.Assign):
                        for t in sub.targets:
                            if isinstance(t, ast.Name):
                                names.add(t.id)
    return names


def _annotation_names(tree: ast.Module) -> set:
    """Every bare name used inside an annotation anywhere in the module."""
    found = set()

    def visit(node):
        for field, value in ast.iter_fields(node):
            if field == "annotation" and value is not None:
                for sub in ast.walk(value):
                    if isinstance(sub, ast.Name):
                        found.add(sub.id)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        visit(item)
            elif isinstance(value, ast.AST):
                visit(value)

    visit(tree)
    return found


def _server_modules():
    for root, _dirs, files in os.walk(SRC):
        if "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


class TestAnnotationNamesAreBound(unittest.TestCase):
    def test_no_annotation_uses_an_unbound_typing_name(self):
        problems = []
        for path in _server_modules():
            rel = os.path.relpath(path, SERVER_ROOT)
            try:
                with open(path, encoding="utf-8") as f:
                    source = f.read()
                tree = ast.parse(source, filename=path)
            except SyntaxError:
                continue
            bound = _module_names(tree)
            used = _annotation_names(tree)
            for name in sorted(used & _TYPING_NAMES):
                if name not in bound:
                    problems.append(f"{rel}: annotation uses {name!r} "
                                    f"but it is not imported")
        self.assertEqual(problems, [],
                         "a typing name used in an annotation but not "
                         "imported is a NameError at import time on "
                         "Python 3.10 (the container runtime). Local "
                         "Python 3.14 defers annotations (PEP 649), so "
                         "neither py_compile nor the local tests catch it.")

    def test_api_pg_imports_tuple(self):
        path = os.path.join(SRC, "api_pg.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        self.assertIn("Tuple", _module_names(tree))

    def test_the_guard_itself_detects_a_missing_import(self):
        """The guard must actually fail on the pattern it exists to catch."""
        broken = "from typing import List\n\ndef f(x: List[Tuple[str, str]]) -> None:\n    pass\n"
        tree = ast.parse(broken)
        bound = _module_names(tree)
        used = _annotation_names(tree)
        self.assertIn("Tuple", used & _TYPING_NAMES)
        self.assertNotIn("Tuple", bound,
                         "the guard's own logic must flag this case")


if __name__ == "__main__":
    unittest.main()
