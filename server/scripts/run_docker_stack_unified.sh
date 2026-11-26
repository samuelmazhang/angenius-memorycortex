#!/usr/bin/env bash
# scripts/run_docker_stack_unified.sh
# Bring up the unified memory-cortex container (V5.2.2+).
#
# This script implements "V5.2.2 hot-reload loop":
#   - Bind-mount /app/src from host, so `git pull` in server/src/ + restart = code refresh
#   - Bind-mount /usr/local/bin/entrypoint.sh from host, so entrypoint changes can be tested
#   - Bind-mount /opt/cortex/schema.sql from host, so schema.sql changes take effect on restart
#   - Bind-mount embedding models from /root/.local/share/cortex-models (existing V5.2 setup)
#   - Named volume for PG data (persistence)
#
# When you `git pull` new code:
#   1. cd /path/to/repo && git pull
#   2. docker restart memory-cortex    (10 sec reload, data preserved)
#
# What this does:
#   1. Creates local model dir if needed
#   2. Removes old 2-container artifacts + current unified container
#   3. Starts ONE container `memory-cortex` with all 4 bind mounts
#   4. Waits for /health to return healthy
#   5. Applies schema.sql via docker exec (idempotent — CREATE TABLE IF NOT EXISTS)
#   6. Verifies via /health
#
# After successful run:
#   - cortex API:    http://localhost:1880/health
#   - PG (admin):    psql -h localhost -p 5432 -U postgres -d postgres (pwd: change_me)
#   - All WSL services should be stopped — see takeover steps
#
# Rollback to image-baked config (no host bind):
#   Use the original docker run command (no -v $(pwd)/... mounts) instead.

set -e

cd "$(dirname "$0")/../.."

DOCKERFILE="server/docker/unified/Dockerfile"
IMG_NAME="memory-cortex-stack"
IMG_TAG="5.2.0"
CONTAINER_NAME="memory-cortex"
VOLUME_NAME="memory-cortex-data"
PARENT_MODEL_DIR="/root/.local/share/cortex-models"
LOCAL_MODEL_DIR="$PARENT_MODEL_DIR/paraphrase-multilingual-MiniLM-L12-v2"
SCHEMA_FILE="server/docker/unified/schema.sql"

echo "=== Cortex v5.2.2 unified — Docker bringup with bind mounts ==="

# 1. Local model dir (avoid /mnt/e bind mount perm issues for models)
if [ ! -d "$LOCAL_MODEL_DIR/onnx" ] || [ ! -f "$LOCAL_MODEL_DIR/tokenizer.json" ]; then
    echo "Copying embedding model to $LOCAL_MODEL_DIR (one-time, ~4GB)..."
    mkdir -p "$LOCAL_MODEL_DIR"
    cp -r /root/.arcadia/plugins/memory-cortex/models/paraphrase-multilingual-MiniLM-L12-v2/. \
        "$LOCAL_MODEL_DIR/"
fi

# 2. Remove existing containers (preserving volumes)
echo "Removing any old containers..."
for c in memory-cortex cortex-api cortex-api-test cortex-api-isolated cortex-pg-test cortex-pg-isolated; do
    docker rm -f "$c" 2>/dev/null || true
done

# 3. Run unified container with bind mounts
#    CRITICAL: container runs as root (--user 0:0) so it can read /mnt/e drvfs paths
echo "Starting ${CONTAINER_NAME} container with bind mounts..."
docker run -d \
    --name "$CONTAINER_NAME" \
    --hostname "$CONTAINER_NAME" \
    --user "0:0" \
    --restart unless-stopped \
    -p 1880:1880 \
    -p 5432:5432 \
    -v "$(pwd)/server/src:/app/src:ro" \
    -v "$(pwd)/server/docker/unified/entrypoint.sh:/usr/local/bin/entrypoint.sh:ro" \
    -v "$(pwd)/server/docker/unified/schema.sql:/opt/cortex/schema.sql:ro" \
    -v "${PARENT_MODEL_DIR}:/root/.arcadia/plugins/memory-cortex/models:ro" \
    -v "${VOLUME_NAME}:/var/lib/postgresql/data" \
    --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
    "${IMG_NAME}:${IMG_TAG}"

# 4. Wait for /health to return healthy
echo "Waiting for healthy..."
for i in $(seq 1 60); do
    sleep 2
    if docker exec "$CONTAINER_NAME" python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:1880/health', timeout=3).read()" >/dev/null 2>&1; then
        echo "Healthy at iter ${i}"
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "ERROR: container not healthy after 120s. Logs:"
        docker logs --tail 50 "$CONTAINER_NAME"
        exit 1
    fi
done

# 5. Apply schema (idempotent — safe to re-run)
echo "Applying schema.sql (idempotent)..."
docker exec "$CONTAINER_NAME" bash -c \
    'PGPASSWORD=change_me psql -U postgres -d postgres -h localhost -f /opt/cortex/schema.sql' \
    2>&1 | grep -E "CREATE|NOTICE|ERROR" | tail -20 || true

# 6. Verify
echo ""
echo "=== Verification ==="
HEALTH=$(curl -s http://localhost:1880/health)
echo "Cortex /health:"
echo "$HEALTH" | python3 -m json.tool 2>/dev/null || echo "$HEALTH"

echo ""
echo "=== Hot-reload loop enabled ==="
echo "  To pick up code changes:"
echo "    cd $(pwd)"
echo "    git pull"
echo "    docker restart memory-cortex    # 10 sec, no data loss"
echo ""
echo "Done. Container '${CONTAINER_NAME}' running with ${IMG_NAME}:${IMG_TAG}"
echo "  - API:    http://localhost:1880"
echo "  - PG:     localhost:5432 (postgres / change_me / db=postgres)"
echo "  - Volume: ${VOLUME_NAME} (managed by docker, NOT host bind)"
