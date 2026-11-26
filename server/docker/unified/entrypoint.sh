#!/bin/bash
# Memory Cortex unified container entrypoint.
# Starts PostgreSQL in background, waits for ready, then runs FastAPI cortex.
# Single port (1880) exposed. PG (5432) is internal-only but accessible from
# docker host via -p 5432:5432 mapping.

set -e

export PATH="/usr/lib/postgresql/16/bin:$PATH"

# Use the baked venv at /opt/cortex-venv for all python execution.
# Dockerfile already sets PATH + VIRTUAL_ENV, but we re-export here
# in case the image was built before the venv migration or the
# operator overrides PATH via docker -e.
export PATH="/opt/cortex-venv/bin:${PATH}"
export VIRTUAL_ENV="/opt/cortex-venv"
# Make site-packages importable as top-level (for fastmcp, opentelemetry etc.)
# even if `python` does not resolve to the venv binary.
export PYTHONPATH="/opt/cortex-venv/lib/python3.11/site-packages:${PYTHONPATH:-}"

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
PG_USER="${PG_USER:-postgres}"
PG_DB="${PG_DB:-postgres}"
PG_PASSWORD="${PG_PASSWORD:-change_me}"

echo "[entrypoint] PGDATA=$PGDATA  user=$PG_USER  db=$PG_DB"

# Setup socket dir
mkdir -p /var/run/postgresql
chown postgres:postgres /var/run/postgresql 2>/dev/null || true
chmod 0755 /var/run/postgresql

# Initialize PG data dir if empty (first run only)
if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "[entrypoint] Initializing PG data dir (first run)..."
    chown -R postgres:postgres "$PGDATA" 2>/dev/null || true
    PWFILE=$(mktemp)
    chmod 644 "$PWFILE"
    echo "$PG_PASSWORD" > "$PWFILE"
    gosu postgres initdb -D "$PGDATA" --pwfile="$PWFILE"
    rm -f "$PWFILE"
fi

# Fix ownership in case volume already had PG data
chown -R postgres:postgres "$PGDATA" 2>/dev/null || true

# Add permissive pg_hba entry for docker network
cat >> "$PGDATA/pg_hba.conf" <<'EOF'
# Allow password auth from any IP (for docker port forwarding)
host    all             all             0.0.0.0/0               md5
EOF

# Start postgres in background (listen on all interfaces for docker port mapping)
echo "[entrypoint] Starting PostgreSQL..."
gosu postgres postgres -D "$PGDATA" -c listen_addresses='*' &
PG_PID=$!

# Wait for ready
echo -n "[entrypoint] Waiting for Postgres... "
for i in $(seq 1 30); do
    if pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
        echo "ready (${i}s)"
        break
    fi
    sleep 1
done

if ! pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
    echo "TIMEOUT"
    exit 1
fi

# Configure (now PG is running)
echo "[entrypoint] Setting password for superuser $PG_USER..."
gosu postgres psql -c "ALTER USER $PG_USER WITH PASSWORD '$PG_PASSWORD';" 2>&1 | head -3

# Install pgvector extension
echo "[entrypoint] Installing pgvector extension..."
gosu postgres psql -d postgres -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>&1 | head -3

# Trap signals
trap 'kill -TERM $PG_PID 2>/dev/null; exit 0' TERM INT

# Set default EMBEDDING_MODEL_PATH if not already set by docker run -e.
# Without this, CortexConfig._load() raises RuntimeError on container
# startup and the container enters a Restarting crash loop (pre-existing
# bug fix; see ADR-077 2026-08-23). Operators can override via -e
# EMBEDDING_MODEL_PATH=... at run time.
if [ -z "$EMBEDDING_MODEL_PATH" ] && [ -d /root/.arcadia/plugins/memory-cortex/models/nomic-embed-text-v1.5 ]; then
    export EMBEDDING_MODEL_PATH=/root/.arcadia/plugins/memory-cortex/models/nomic-embed-text-v1.5
    echo "[entrypoint] EMBEDDING_MODEL_PATH set to $EMBEDDING_MODEL_PATH (default)"
fi

# Start cortex API
echo "[entrypoint] Starting cortex API on port 1880..."
cd /app
exec python3 -m src.api_pg
