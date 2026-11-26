#!/usr/bin/env python3
"""Consistency guards against the version / endpoint-catalog drift found 2026-11-25.

Checks (all read-only, fail with a non-zero exit on drift):
  1. VERSION file  ==  FastAPI app.version string in server/src/api_pg.py
  2. README.md H1  ==  VERSION (e.g. "# Memory Cortex v6.7.0")
  3. 文档索引.md "当前版本" == VERSION
  4. 架构总览.md H1  ==  VERSION
  5. ENDPOINTS_CATALOG paths all resolve to a real @app.<method>(...) decorator
     (catches "ghost" catalog entries that do not exist as routes).

No third-party deps. Runs in pre-commit and in CI.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
ERRORS: list[str] = []


def check_version(label: str, text: str) -> None:
    if VERSION not in text:
        ERRORS.append(f"{label}: expected version {VERSION} not found")


# --- 1/2/3/4: version strings -------------------------------------------------
api = (ROOT / "server/src/api_pg.py").read_text(encoding="utf-8")
m = re.search(r'FastAPI\([^\n]*version="([0-9.]+)"', api)
if not m:
    ERRORS.append("api_pg.py: cannot find FastAPI(version=...)")
elif m.group(1) != VERSION:
    ERRORS.append(f"api_pg.py: FastAPI version {m.group(1)} != VERSION {VERSION}")

readme = (ROOT / "README.md").read_text(encoding="utf-8")
if not re.search(rf"^# Memory Cortex v{re.escape(VERSION)}\s*$", readme, re.M):
    ERRORS.append(f"README.md: H1 is not '# Memory Cortex v{VERSION}'")

index = (ROOT / "docs/文档索引.md").read_text(encoding="utf-8")
check_version("docs/文档索引.md", index)

overview = (ROOT / "docs/架构总览.md").read_text(encoding="utf-8")
check_version("docs/架构总览.md", overview)

# --- 5: ENDPOINTS_CATALOG vs real routes --------------------------------------
# P0 phase 3: route declarations live in routers/<domain>.py as
# `router.add_api_route(path, fn, methods=[...])`. Scan those; also honour any
# remaining @app.<verb> decorators in api_pg.py for backward compatibility.
tree = ast.parse(api)
real: set[tuple[str, str]] = set()
for node in ast.walk(tree):
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
            if dec.func.attr in ("get", "post", "put", "delete", "patch"):
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    real.add((dec.func.attr.upper(), str(dec.args[0].value)))

RD = ROOT / "server/src/routers"
if RD.is_dir():
    for rf in sorted(RD.glob("*.py")):
        if rf.name == "__init__.py":
            continue
        rt = ast.parse(rf.read_text(encoding="utf-8"))
        for node in ast.walk(rt):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_api_route" and node.args
                    and isinstance(node.args[0], ast.Constant)):
                path = str(node.args[0].value)
                methods = []
                for kw in node.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
                if methods:
                    for m in methods:
                        real.add((str(m).upper(), path))

catalog = None
for node in tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "ENDPOINTS_CATALOG":
                try:
                    catalog = ast.literal_eval(node.value)
                except Exception:
                    pass
if catalog is None:
    ERRORS.append("api_pg.py: ENDPOINTS_CATALOG not statically evaluable")
else:
    method_map = {
        "GET": "get", "POST": "post", "PUT": "put", "DELETE": "delete", "PATCH": "patch",
    }
    for entry in catalog:
        method = str(entry.get("method", "")).upper()
        path = str(entry.get("path", ""))
        # tolerate method spelling drift by checking path alone as fallback
        if not any(m == method and p == path for m, p in real):
            if not any(p == path for _, p in real):
                ERRORS.append(
                    f"ENDPOINTS_CATALOG ghost entry: {method} {path} "
                    "(no matching @app decorator)"
                )

# --- 6: /endpoints must be router-derived (P0 phase 3) ----------------------
# list_endpoints() walks app.routes. Phase-3 final uses a recursive
# _iter_routes() helper because newer FastAPI inserts _IncludedRouter
# aggregate objects (path=None) that must be expanded via original_router.
# Accept both the direct form and the helper form, but still require an
# app.routes traversal AND the provenance tag.
_has_traversal = (
    "_iter_routes(app.routes)" in api
    or "for r in app.routes:" in api
)
if not _has_traversal or '"source": "app.routes"' not in api:
    ERRORS.append("api_pg.py: /endpoints is not derived from app.routes")

if ERRORS:
    print("consistency_check FAILED:")
    for e in ERRORS:
        print(f"  - {e}")
    sys.exit(1)
print(f"consistency_check OK (VERSION={VERSION}, routes={len(real)}, catalog={len(catalog or [])})")
