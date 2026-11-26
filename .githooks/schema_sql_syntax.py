#!/usr/bin/env python3
"""Pre-commit hook: validate schema.sql syntax with sqlparse."""
import sys
try:
    import sqlparse
except ImportError:
    print("sqlparse not installed (pip install sqlparse)", file=sys.stderr)
    sys.exit(1)

text = open(sys.argv[1]).read()
stmts = sqlparse.parse(text)
errors = []
for s in stmts:
    if s.get_type() in ('CREATE', 'INSERT', 'ALTER'):
        # Skip empty statements
        body = str(s).strip().rstrip(';').strip()
        if not body or body == s.get_type():
            errors.append(f"empty {s.get_type()} statement")

if errors:
    print("\n".join(errors), file=sys.stderr)
    sys.exit(1)
print(f"OK: {len(stmts)} statements parsed")
