#!/usr/bin/env python3
"""Pre-commit hook: basic Dockerfile AST check."""
import sys

content = open(sys.argv[1]).read()
# Required: FROM always present (multi-stage may have multiple)
if not any(l.strip().startswith('FROM') for l in content.split('\n')):
    print("Missing FROM directive", file=sys.stderr)
    sys.exit(1)

# Recommend: RUN, COPY, ENTRYPOINT or CMD
recommended = ['RUN', 'ENTRYPOINT']
missing = [k for k in recommended if not any(l.strip().startswith(k) for l in content.split('\n'))]
if missing:
    print(f"Recommended directives missing: {missing}", file=sys.stderr)
    # Don't fail, just warn
    print(f"WARNING: missing recommended directives: {missing}", file=sys.stderr)

print(f"OK: {len(content.split(chr(10)))} lines parsed")
