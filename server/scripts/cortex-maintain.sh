#!/usr/bin/env bash
# scripts/cortex-maintain.sh
# Bring up the redundant memory-cortex API instance on :1881 with its OWN
# bundled PostgreSQL.
#
# Why this script exists: production `memory-cortex` on :1880 already uses
# the bundled PG (memory-cortex-data volume). For zero-downtime maintenance
# windows (image upgrade, schema migration rehearsal, scheduler bug fixes)
# we want a second instance with a SEPARATE PG volume so an experiment on
# :1881 cannot corrupt :1880's data.
#
# Setup:
#   - Volume "cortex-maintain-pgdata" holds the bundled PG's data dir
#   - Image's own entrypoint.sh starts PG, sets user pwd, installs pgvector,
#     and execs python3 -m src.api_pg
#   - scripts/cortex-apply-schema.sh applies schema.sql + migrations/*.sql
#     because api_pg does not auto-bootstrap either
#
# Earlier attempts (2026-09-02 / 09-05) failed because:
#   - First:  --entrypoint bash ran python3 directly, bypassing the Postgres
#             startup in entrypoint.sh -- crash loop on "Connection refused"
#             at 127.0.0.1:5432.
#   - Second: pointed PG_HOST at the quantplatform-postgres container
#             (uses a `quantplatform` user, not `postgres') -- auth fail.
#   - Third:  --entrypoint correctly used entrypoint.sh, but bind-mounted a
#             fresh empty volume -- schema was not created on first boot
#             because api_pg doesn't auto-bootstrap schema.sql. Furthermore,
#             memory_maintenance_queue (added in migration
#             2026_09_01_memory_maintenance_queue.sql) was never applied to
#             the standalone schema bootstrap, so /admin/maintenance/stats
#             returned "relation does not exist" forever.
#   - Fourth (this script, 2026-09-07): use the image's native ENTRYPOINT,
#             dedicated named volume, and delegate schema bootstrap to
#             cortex-apply-schema.sh which applies schema.sql + every
#             migrations/*.sql in lexical order.

set -euo pipefail

NAME=memory-cortex-maintain
IMAGE=memory-cortex-stack:latest
PORT=1881
PGDATA_VOL=cortex-maintain-pgdata
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 0. Persistent PGDATA volume.
if ! docker volume inspect "$PGDATA_VOL" >/dev/null 2>&1; then
    echo "[+] Creating volume $PGDATA_VOL"
    docker volume create "$PGDATA_VOL"
fi

# 1. Remove any prior instance.
if docker ps -a --format '{{.Names}}' | grep -qw "$NAME"; then
    echo "[+] Removing old $NAME"
    docker stop "$NAME" >/dev/null 2>&1 || true
    docker rm "$NAME" >/dev/null 2>&1 || true
fi

# 2. Start the new instance.
echo "[+] Starting $NAME on :$PORT"
docker run -d \
    --name "$NAME" \
    --restart unless-stopped \
    -p "$PORT":1880 \
    -v "$PGDATA_VOL":/var/lib/postgresql/data \
    -v /opt/cortex-deploy/work/server/src:/app/src:ro \
    -v /opt/cortex-deploy/work/server/docker/unified/schema.sql:/opt/cortex/schema.sql:ro \
    -v /opt/cortex-deploy/work/server/docker/unified/entrypoint.sh:/usr/local/bin/entrypoint.sh:ro \
    -v /root/.local/share/cortex-models:/root/.arcadia/plugins/memory-cortex/models:ro \
    -e PG_HOST=localhost \
    -e PG_PORT=5432 \
    -e PG_DB=postgres \
    -e PG_USER=postgres \
    -e PG_PASSWORD=change_me \
    -e EMBEDDING_MODEL_PATH=/root/.arcadia/plugins/memory-cortex/models/nomic-embed-text-v1.5 \
    "$IMAGE"

# 3. Wait for entrypoint.sh to finish PG startup (typically 30s
#    for first boot with initdb).
echo "[+] Waiting 45s for PG boot..."
sleep 45

# 4. Bootstrap schema + every migration. Idempotent.
echo "[+] Applying schema.sql + migrations/*.sql"
bash "${SCRIPT_DIR}/cortex-apply-schema.sh" "$NAME"

echo "[+] Container state:"
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' --filter "name=$NAME"
