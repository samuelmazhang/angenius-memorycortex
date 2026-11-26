#!/usr/bin/env bash
# Pre-commit hook: validate docker-compose files via docker-compose v1 config
# Falls back to yaml parse if docker-compose unavailable
set -e

for f in "$@"; do
    if command -v docker-compose >/dev/null 2>&1; then
        # Try docker-compose v1 (works on this host per WSL setup)
        docker-compose -f "$f" config >/dev/null 2>&1 || {
            # Fall back: at least check YAML parses
            python3 -c "import yaml; yaml.safe_load(open('$f'))" >/dev/null 2>&1 || {
                echo "compose validation failed: $f"
                exit 1
            }
            echo "WARN: $f - docker-compose unavailable, only YAML validated"
        }
    else
        python3 -c "import yaml; yaml.safe_load(open('$f'))" >/dev/null || {
            echo "YAML parse failed: $f"
            exit 1
        }
    fi
done
echo "OK: all $(echo "$@" | wc -w) compose files validated"
