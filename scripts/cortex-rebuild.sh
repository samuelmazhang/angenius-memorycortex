#!/bin/bash
# cortex-rebuild.sh
# Rebuild the memory-cortex-stack image and re-run the deploy pipeline.
# Use this when Dockerfile changes (new apt packages, new pip packages,
# python version bump). For source-only changes, prefer cortex-deploy.sh
# which is a hot update (no image rebuild).
#
# Image tag policy (2026-09-01): the image is tagged :latest -- the
# source code version (in api_pg.py) updates via git pull + container
# restart independently. Image and code versions are decoupled.
#
# Pipeline (6 steps):
#   1. git pull --ff-only (we need the latest Dockerfile)
#   2. docker build -t memory-cortex-stack:latest .
#   3. tag the old image as :previous (rollback safety)
#   4. docker rm -f memory-cortex
#   5. docker run -d ... (same mounts/env as cortex-deploy.sh describes)
#   6. wait healthy + smoke test
#
# Pass "rollback" to revert to the previous image instead of building.
set -e

WORK=/opt/cortex-deploy/work
CONTAINER=memory-cortex
IMAGE=memory-cortex-stack
NEW_TAG=latest
OLD_TAG=5.3.9

echo "=== [1/6] git pull ==="
cd "$WORK"
git pull --ff-only origin main 2>&1 | tail -3

if [ "$1" = "rollback" ]; then
  echo "=== ROLLBACK: restarting $IMAGE:$OLD_TAG ==="
  sudo docker rm -f "$CONTAINER" 2>/dev/null || true
  sudo docker run -d --name "$CONTAINER" --restart unless-stopped \
    -p 1880:1880 \
    -v memory-cortex-data:/var/lib/postgresql/data \
    -v "$WORK/server/src":/app/src:ro \
    -v "$WORK/server/docker/unified/schema.sql":/opt/cortex/schema.sql:ro \
    -v "$WORK/server/docker/unified/entrypoint.sh":/usr/local/bin/entrypoint.sh:ro \
    -v /root/.local/share/cortex-models:/root/.hermes/plugins/memory-cortex/models:ro \
    "$IMAGE:$OLD_TAG" 2>&1 | tail -3
  exit 0
fi

echo "=== [2/6] docker build $IMAGE:$NEW_TAG (venv-baked deps) ==="
cd "$WORK"
# Build context = repo root so COPY server/docker/unified/entrypoint.sh
# resolves (server/src and VERSION are bind-mounted at runtime, not
# baked into the image). --cache-from=5.3.9 reuses the apt layer so
# the postgres-16-pgvector install is cached. BuildKit cache mount for
# /root/.cache/pip keeps the downloaded wheels across rebuilds.
#
# v6.5.0 architecture: the venv at /opt/cortex-venv is BAKED into the
# image during this build (all business pip packages are installed
# at build time, not at container start). Adding a new dep = edit
# this Dockerfile and rebuild; no host-side install needed.
sudo docker build --cache-from "$IMAGE:$OLD_TAG" \
    -f server/docker/unified/Dockerfile \
    -t "$IMAGE:$NEW_TAG" . 2>&1 | tail -20

echo
echo "=== [3/6] tag current $IMAGE:$OLD_TAG as :previous (rollback safety) ==="
if sudo docker image inspect "$IMAGE:previous" >/dev/null 2>&1; then
  sudo docker rmi "$IMAGE:previous" 2>/dev/null || true
fi
if sudo docker image inspect "$IMAGE:$OLD_TAG" >/dev/null 2>&1; then
  sudo docker tag "$IMAGE:$OLD_TAG" "$IMAGE:previous"
  echo "  $IMAGE:previous <- $IMAGE:$OLD_TAG (rollback safety net)"
fi

echo
echo "=== [4/6] remove old container (preserve data volume) ==="
sudo docker rm -f "$CONTAINER" 2>/dev/null || true
sleep 1

echo
echo "=== [5/6] start new container with $IMAGE:$NEW_TAG ==="
sudo docker run -d --name "$CONTAINER" --restart unless-stopped \
  -p 1880:1880 \
  -v memory-cortex-data:/var/lib/postgresql/data \
  -v "$WORK/server/src":/app/src:ro \
  -v "$WORK/server/docker/unified/schema.sql":/opt/cortex/schema.sql:ro \
  -v "$WORK/server/docker/unified/entrypoint.sh":/usr/local/bin/entrypoint.sh:ro \
  -v /root/.local/share/cortex-models:/root/.hermes/plugins/memory-cortex/models:ro \
  "$IMAGE:$NEW_TAG"

echo
echo "=== [6/6] wait healthy + smoke test ==="
for i in $(seq 1 90); do
  sleep 1
  if curl -sf -m 3 http://127.0.0.1:1880/health >/dev/null 2>&1; then
    echo "  healthy at iter ${i}s"
    break
  fi
  if [ "$i" = "90" ]; then echo "  TIMEOUT"; exit 1; fi
done
echo "  /health: $(curl -s http://127.0.0.1:1880/health)"
echo "  /admin/maintenance/stats:"
curl -s http://127.0.0.1:1880/admin/maintenance/stats | python3 -m json.tool 2>/dev/null | head -15 || \
  curl -s http://127.0.0.1:1880/admin/maintenance/stats | head -c 400
echo
echo
echo "=== summary ==="
echo "  built image: $IMAGE:$NEW_TAG"
echo "  rollback target: $IMAGE:previous  (run '$0 rollback' to revert)"