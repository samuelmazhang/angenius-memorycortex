"""Bridge between P0-phase-3 route declarations and pre-existing source tests.

Before P0 phase 3, every endpoint was declared in ``api_pg.py`` as::

    @app.get("/provenance/audit")
    def provenance_audit(...): ...

A number of regression tests assert on that source text -- both to check
"endpoint exists" and to use ``\\n@app.`` as the boundary marker for the
next handler when slicing a function body out for inspection.

Phase 3 moved the 118 decorators into ``src/routers/<domain>_routes.py`` as::

    router.add_api_route('/provenance/audit', H['provenance_audit'], methods=['GET'])

The handler functions in ``api_pg.py`` did not change at all; only their
declarations moved. This module reconstructs the original shape by inserting
``@app.<verb>("path")`` lines immediately BEFORE each handler's ``def`` in the
api_pg source. That restores both the decorator-text assertions and the exact
``\\n@app.`` function-boundary semantics the slicing tests rely on.

Nothing is executed -- this is pure text reconstruction: AST-parse the router
modules to learn (path, handler_name, verbs), AST-parse api_pg.py to find each
handler's source line, then splice decorator lines in place.
"""
from __future__ import annotations

import ast
import os

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


def _ensure_importable() -> None:
    """Make this module importable both as `tests._router_source_bridge`
    (pytest with tests/__init__.py) and as a top-level module."""
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


_ensure_importable()


def _collect_routes() -> list[tuple[str, str, list[str]]]:
    """Return [(handler_name, path, [verbs])] from routers/*_routes.py."""
    routers_dir = os.path.join(_SRC_DIR, "routers")
    routes: list[tuple[str, str, list[str]]] = []
    if not os.path.isdir(routers_dir):
        return routes
    for name in sorted(os.listdir(routers_dir)):
        if not name.endswith("_routes.py"):
            continue
        path = os.path.join(routers_dir, name)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if (not isinstance(node, ast.Call)
                    or not isinstance(node.func, ast.Attribute)
                    or node.func.attr != "add_api_route"
                    or len(node.args) < 2
                    or not isinstance(node.args[0], ast.Constant)):
                continue
            route_path = str(node.args[0].value)
            # Second arg is H['handler_name'].
            handler = None
            arg2 = node.args[1]
            if isinstance(arg2, ast.Subscript) and isinstance(arg2.slice, ast.Constant):
                handler = str(arg2.slice.value)
            elif isinstance(arg2, ast.Constant):
                handler = str(arg2.value)
            if not handler:
                continue
            methods: list[str] = []
            for kw in node.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [str(e.value).lower() for e in kw.value.elts
                               if isinstance(e, ast.Constant)]
            if methods:
                routes.append((handler, route_path, methods))
    return routes


def api_source_with_routes() -> str:
    """Return api_pg.py source with phase-3 decorators restored in place.

    Each ``@app.<verb>("path")`` is inserted immediately before the matching
    top-level ``def <handler>`` in api_pg.py, reproducing the pre-phase-3
    layout. This keeps:

      * ``assertIn('@app.get("/x")', SOURCE)`` working;
      * declaration-order checks working (insertion follows router module
        order, and within a handler follows the methods list order);
      * ``seg = SOURCE[SOURCE.index('def f('):]; seg[:seg.index('\\n@app.')]``
        stopping at the NEXT handler, because each decorator block sits
        directly before its def and starts with ``\\n@app.``.
    """
    with open(os.path.join(_SRC_DIR, "api_pg.py"), encoding="utf-8") as fh:
        source = fh.read()

    routes = _collect_routes()
    if not routes:
        return source

    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)

    # handler name -> 0-based line index of its `def` (top-level only)
    def_line: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # node.lineno is 1-based; find the `def` line (skip decorators)
            idx = node.lineno - 1
            while idx < len(lines) and not lines[idx].lstrip().startswith(("def ", "async def ")):
                idx += 1
            def_line.setdefault(node.name, idx)

    # handler -> list of decorator lines
    decorators: dict[str, list[str]] = {}
    for handler, route_path, methods in routes:
        if handler not in def_line:
            continue
        bucket = decorators.setdefault(handler, [])
        for verb in methods:
            bucket.append(f'@app.{verb}("{route_path}")\n')

    if not decorators:
        return source

    # Insert decorators before each def, iterating line indices in reverse
    # so earlier insertions don't shift later ones.
    for handler in sorted(decorators, key=lambda h: def_line[h], reverse=True):
        idx = def_line[handler]
        lines[idx:idx] = decorators[handler]

    return "".join(lines)
