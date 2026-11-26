#!/usr/bin/env bash
# scripts/cortex-apply-schema.sh
# Apply schema.sql + every migrations/*.sql to a running memory-cortex container.
#
# Why: schema.sql is the "frozen baseline" applied by entrypoint.sh on first
# boot. Anything created AFTER the baseline lives in
# server/src/migrations/<date>_<feature>.sql. Future container bring-ups need
# to apply BOTH to reach the current state. Without this, a fresh container
# (e.g. memory-cortex-maintain spin-up) silently misses any table added post-
# baseline and /admin/maintenance/* endpoints return "relation does not exist".
#
# Idempotent — every CREATE is IF NOT EXISTS, safe to re-run on each restart.
#
# Usage:
#   ./scripts/cortex-apply-schema.sh memory-cortex
#   ./scripts/cortex-apply-schema.sh memory-cortex-maintain
#
# 2026-09-07: created when memory-cortex-maintain came up with 21 tables
# instead of 22 (missing memory_maintenance_queue -> scheduler silently
# never ran).

set -euo pipefail

CONTAINER_NAME="${1:-memory-cortex}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCHEMA_FILE="${REPO_ROOT}/server/docker/unified/schema.sql"
MIGRATIONS_DIR="${REPO_ROOT}/server/src/migrations"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not on PATH" >&2
    exit 1
fi
if ! docker ps --format '{{.Names}}' | grep -qw "${CONTAINER_NAME}"; then
    echo "ERROR: container ${CONTAINER_NAME} not running" >&2
    exit 1
fi
if [ ! -f "${SCHEMA_FILE}" ]; then
    echo "ERROR: schema.sql not found at ${SCHEMA_FILE}" >&2
    exit 1
fi

# Copy files into the container (they sit on a RO bind mount, so we stage
# them under /tmp where the postgres user can read them).
echo "[+] Staging schema.sql into ${CONTAINER_NAME}:/tmp/schema.sql"
docker cp "${SCHEMA_FILE}" "${CONTAINER_NAME}:/tmp/schema.sql"

echo "[+] Applying schema.sql"
docker exec -e PGPASSWORD=change_me "${CONTAINER_NAME}" \
    psql -U postgres -d postgres -h localhost \
    -v ON_ERROR_STOP=1 -f /tmp/schema.sql 2>&1 | tail -5 || {
        echo "ERROR: schema.sql apply failed" >&2
        exit 1
    }

# Apply migrations in lexical date order so dependencies land before dependents.
if [ -d "${MIGRATIONS_DIR}" ]; then
    for mig in "${MIGRATIONS_DIR}"/*.sql; do
        [ -e "${mig}" ] || continue
        name="$(basename "${mig}")"
        echo "[+] Applying migration ${name}"
        docker cp "${mig}" "${CONTAINER_NAME}:/tmp/${name}"
        docker exec -e PGPASSWORD=change_me "${CONTAINER_NAME}" \
            psql -U postgres -d postgres -h localhost \
            -v ON_ERROR_STOP=1 -f "/tmp/${name}" 2>&1 | tail -5 || {
                echo "ERROR: migration ${name} failed" >&2
                exit 1
            }
    done
else
    echo "[!] migrations dir ${MIGRATIONS_DIR} not present, skipping"
fi

echo "[+] Done. Container ${CONTAINER_NAME} schema fully bootstrapped."
