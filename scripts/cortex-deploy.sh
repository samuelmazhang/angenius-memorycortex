#!/bin/bash
# cortex-deploy.sh
# Pull latest code from Gitea, restart the running memory-cortex container,
# apply pending SQL migrations, and verify health. Designed for hot updates
# (no image rebuild).
#
# Pipeline (7 steps):
#   1. git pull --ff-only in /opt/cortex-deploy/work
#   2. (no-op) file sync — host /opt/cortex-deploy/work/server/src is bind-
#      mounted RO into the container at /app/src, so a git pull on the host
#      instantly refreshes what the running uvicorn process sees on next start
#   3. optionally apply migrations (unified schema.sql + /server/src/migrations/*.sql)
#   4. restart the container
#   5. wait for /health to return 200
#   6. smoke test the new /admin/maintenance endpoints
#   7. summary (HEAD, deploy ts, /health, backup count)
#
# Usage:
#   cortex-deploy.sh                -- hot deploy, no migrations
#   cortex-deploy.sh schema         -- unified schema.sql only (full reset)
#   cortex-deploy.sh schema-all     -- unified + all incremental migrations
#
# Env:
#   CORTEX_DEPLOY_SKIP_MIGRATIONS=1  -- skip step 3 entirely
set -e

WORK=/opt/cortex-deploy/work
CONTAINER=memory-cortex
GITEA_URL=http://<user>:<token>@127.0.0.1:16080/<user>/Memory-Cortex.git
DEPLOY_TS=$(date +%Y%m%d_%H%M%S)
SOURCE_BIND=/opt/cortex-deploy/work/server/src
APP_DIR=/app/src

if [ ! -d "$WORK/.git" ]; then
  echo "FATAL: $WORK is not a git repo. Bootstrap first:"
  echo "  cd /opt/cortex-deploy && git clone $GITEA_URL work"
  exit 1
fi

echo "=== [1/9] git pull ==="
cd "$WORK"
git pull --ff-only origin main 2>&1 | tail -5
HEAD=$(git rev-parse --short HEAD)
HEAD_SUBJ=$(git log -1 --format='%s' HEAD)
echo "  HEAD now: $HEAD  ($HEAD_SUBJ)"

echo
echo "=== [2/9] verify bind-mounted source is up to date ==="
HOST_API_SIZE=$(stat -c %s "$SOURCE_BIND/api_pg.py")
CONTAINER_API_SIZE=$(sudo docker exec "$CONTAINER" stat -c %s "$APP_DIR/api_pg.py")
if [ "$HOST_API_SIZE" != "$CONTAINER_API_SIZE" ]; then
  echo "  WARN: bind mount mismatch (host=$HOST_API_SIZE container=$CONTAINER_API_SIZE); restart will fix"
fi
echo "  host     $SOURCE_BIND/api_pg.py  -> $HOST_API_SIZE bytes"
echo "  container $APP_DIR/api_pg.py      -> $CONTAINER_API_SIZE bytes (same inode via bind mount)"

echo
echo "=== [3/9] optionally apply SQL migrations ==="
if [ "${CORTEX_DEPLOY_SKIP_MIGRATIONS:-0}" = "1" ]; then
  echo "  skipped (CORTEX_DEPLOY_SKIP_MIGRATIONS=1)"
elif [ "$1" = "schema" ] || [ "$1" = "schema-all" ]; then
  echo "  applying unified schema.sql..."
  sudo docker cp "$WORK/server/docker/unified/schema.sql" "$CONTAINER:/tmp/schema.sql"
  sudo docker exec "$CONTAINER" psql -U postgres -d postgres -f /tmp/schema.sql 2>&1 | tail -5
  sudo docker exec "$CONTAINER" rm /tmp/schema.sql
fi
if [ "$1" = "schema-all" ]; then
  echo "  applying incremental migrations (filename-sorted)..."
  for m in $(ls -1 "$WORK/server/src/migrations/"*.sql | sort); do
    bn=$(basename "$m")
    echo "    -> $bn"
    sudo docker cp "$m" "$CONTAINER:/tmp/$bn"
    sudo docker exec "$CONTAINER" psql -U postgres -d postgres -f /tmp/$bn 2>&1 | tail -3
    sudo docker exec "$CONTAINER" rm "/tmp/$bn"
  done
fi
if [ "$1" != "schema" ] && [ "$1" != "schema-all" ]; then
  echo "  skipped (pass 'schema' or 'schema-all' to apply)"
fi

echo
echo "=== [4/9] carry the connector source into the container ==="
# server/tests/test_lifecycle_precision_v6_6_1.py reads
# connector/hermes/memory_tool.py and cortex_memory_backend.py to assert
# on the Hermes-side behaviour. The container only bind-mounts
# server/src, so the repo-relative path does not exist there and the
# test raised FileNotFoundError AT COLLECTION time -- which aborts the
# whole pytest run, not just that file.
#
# Copying the two files in keeps the test meaningful (it asserts on the
# real source) instead of weakening it to skip.
if [ -d "$WORK/connector/hermes" ]; then
  # Two destinations: /app/connector/hermes (the container's own tree) and
  # /connector/hermes (the repo root the container's tests resolve to).
  sudo docker exec "$CONTAINER" mkdir -p /app/connector/hermes /connector/hermes 2>/dev/null || true
  sudo docker cp "$WORK/connector/hermes/." "$CONTAINER:/app/connector/hermes/" 2>/dev/null || true
  sudo docker cp "$WORK/connector/hermes/." "$CONTAINER:/connector/hermes/" 2>/dev/null || true
  echo "  connector/hermes -> /app/connector/hermes and /connector/hermes"
fi

echo
echo "=== [5/9] rebuild the repo-shaped tree the test suite expects ==="
# The tests resolve repo paths as <repo>/server/src, <repo>/server/tests,
# <repo>/server/docker/unified/schema.sql and
# <repo>/plugin_package/memory_cortex/requirements.txt. The container
# flattens that to /app/src, /app/tests and /opt/cortex/schema.sql, so
# ~22 tests failed with FileNotFoundError / ModuleNotFoundError --
# including a MODULE-LEVEL `import graph`, which aborted collection of
# whole files.
#
# Rather than editing two dozen tests, recreate the repo shape at the
# container root with symlinks: one place, and the tests keep asserting
# against the real files.
sudo docker exec "$CONTAINER" sh -c '
  mkdir -p /server/docker/unified /plugin_package/memory_cortex /connector
  ln -sfn /app/src /server/src
  ln -sfn /app/tests /server/tests
  cp -f /opt/cortex/schema.sql /server/docker/unified/schema.sql 2>/dev/null || true
' 2>/dev/null || true
sudo docker cp "$WORK/server/docker/unified/schema.sql" \
  "$CONTAINER:/server/docker/unified/schema.sql" 2>/dev/null || true
if [ -f "$WORK/plugin_package/memory_cortex/requirements.txt" ]; then
  sudo docker cp "$WORK/plugin_package/memory_cortex/requirements.txt" \
    "$CONTAINER:/plugin_package/memory_cortex/requirements.txt" 2>/dev/null || true
fi
echo "  /server/src, /server/tests, /server/docker, /plugin_package, /connector"

echo
echo "=== [6/9] restart container ==="
sudo docker restart "$CONTAINER"

echo
echo "=== [7/9] wait healthy ==="
HEALTHY=false
for i in $(seq 1 60); do
  sleep 1
  if curl -sf -m 3 http://127.0.0.1:1880/health >/dev/null 2>&1; then
    HEALTHY=true
    echo "  healthy at iter ${i}s"
    break
  fi
  if [ "$i" = "60" ]; then echo "  TIMEOUT"; exit 1; fi
done

echo
echo "=== [8/9] smoke test ==="
echo "  /health: $(curl -s http://127.0.0.1:1880/health)"
echo "  /stats:  $(curl -s http://127.0.0.1:1880/stats | head -c 200)"
echo "  /admin/maintenance/stats:"
curl -s http://127.0.0.1:1880/admin/maintenance/stats | python3 -m json.tool 2>/dev/null | head -25 || \
  curl -s http://127.0.0.1:1880/admin/maintenance/stats | head -c 400
echo

echo
echo "=== [9/9] summary ==="
echo "  deployed HEAD:        $HEAD"
echo "  deployed subject:     $HEAD_SUBJ"
echo "  deploy timestamp:     $DEPLOY_TS"
echo "  /health:              $(curl -s http://127.0.0.1:1880/health)"
N=$(sudo docker exec "$CONTAINER" ls -1 "${APP_DIR}/api_pg.py".bak.* 2>/dev/null | wc -l)
echo "  container backups:    $N files"