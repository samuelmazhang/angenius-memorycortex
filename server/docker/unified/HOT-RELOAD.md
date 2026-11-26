# Hot-reload workflow (V5.2.2+)

The unified container now uses **host bind mounts** for code, so changes to the
repo can be picked up without rebuilding the docker image.

## What's bind-mounted (read-only)

| Host path | Container path | Purpose |
|-----------|---------------|---------|
| `server/src/` | `/app/src` | cortex Python source |
| `server/docker/unified/entrypoint.sh` | `/usr/local/bin/entrypoint.sh` | PG init + uvicorn startup |
| `server/docker/unified/schema.sql` | `/opt/cortex/schema.sql` | DB schema (applied on restart) |
| `/root/.local/share/cortex-models/` | `/root/.hermes/plugins/memory-cortex/models` | embedding models |

## Workflow

### Update cortex code (api_pg.py, memory_*.py, etc.)

```bash
cd /mnt/e/MyDevelop/AnGenius-Agent/memory-cortex-clean
git pull                                     # get latest code
docker restart memory-cortex                 # 10-sec restart, data preserved
curl -s http://localhost:1880/health         # verify it's running
```

The Python process exits, entrypoint.sh re-runs PG (already up, just healthcheck),
then `python3 -m src.api_pg` re-imports the modules from the bind-mounted `/app/src`.

### Update entrypoint.sh

```bash
# Edit server/docker/unified/entrypoint.sh
git commit -am "..."
docker restart memory-cortex
```

### Update schema.sql

```bash
# Edit server/docker/unified/schema.sql
# Add new CREATE TABLE / ALTER TABLE statements
docker restart memory-cortex
docker exec memory-cortex bash -c \
    'PGPASSWORD=change_me psql -U postgres -d postgres -h localhost -f /opt/cortex/schema.sql'
```

`CREATE TABLE IF NOT EXISTS` makes the script idempotent (safe to re-run).
For non-idempotent changes (`ALTER TABLE`, `UPDATE`), apply manually first.

### Update embedding models

Models are in `/root/.local/share/cortex-models/` on the host. Drop new files in
the right subdirectory, then `docker restart memory-cortex` to pick them up.

## What CANNOT be hot-reloaded

These require `docker build` + restart (or `run_docker_stack_unified.sh`):

- **Python packages** (fastapi, psycopg2, etc.) — baked into image layer
- **PostgreSQL itself** (binary, init scripts)
- **pgvector** extension (loaded at PG startup, image-baked)
- **OS packages** (apt-get install)

## When to rebuild

Rebuild only when you change:
- `server/docker/unified/Dockerfile`
- `server/src/` requirements (new pip package)
- PG major version / pgvector upgrade
- OS-level deps

```bash
cd /mnt/e/MyDevelop/AnGenius-Agent/memory-cortex-clean
docker build -f server/docker/unified/Dockerfile -t memory-cortex-stack:5.2.0 ../..
bash server/scripts/run_docker_stack_unified.sh  # restarts with new image
```

## Data persistence

All container restarts preserve:
- 997 chunks + 1378 relations in PG (named volume `memory-cortex-data`)
- Embedding models (host bind mount)
- All transient state (search caches, etc. lost on restart ~10s)

## Verification commands

```bash
# Confirm bind mounts are active
docker inspect memory-cortex -f '{{json .Mounts}}' | python3 -m json.tool

# Confirm container sees the same files as host
docker exec memory-cortex cat /app/src/api_pg.py | wc -l
wc -l /mnt/e/MyDevelop/AnGenius-Agent/memory-cortex-clean/server/src/api_pg.py
# Should be identical line counts

# E2E test
python3 server/tests/e2e_smoke.py  # should report 48 passed
```
