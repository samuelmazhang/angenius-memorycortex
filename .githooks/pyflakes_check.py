#!/usr/bin/env python3
"""Pre-commit hook: pyflakes on Python files."""
import io
import sys

from pyflakes.api import check
from pyflakes.reporter import Reporter

errors = 0
for f in sys.argv[1:]:
    out = io.StringIO()
    err = io.StringIO()
    reporter = Reporter(out, err)
    try:
        with open(f) as fp:
            source = fp.read()
        warnings = check(source, f, reporter=reporter)
    except Exception as e:
        print(f"  error checking {f}: {e}", file=sys.stderr)
        errors += 1
        continue
    captured = out.getvalue() + err.getvalue()
    if captured.strip():
        print(f"=== {f} ===")
        print(captured)
        errors += 1

if errors:
    print(f"\n{errors} file(s) have pyflakes issues", file=sys.stderr)

sys.exit(1 if errors else 0)
