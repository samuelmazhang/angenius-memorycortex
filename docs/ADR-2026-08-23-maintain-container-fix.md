# ADR-2026-08-23: memory-cortex-maintain container restart-loop fix

## Problem

User reported that the `memory-cortex-maintain` container on
127.0.0.1 was "always paused / restarting". `docker ps -a`
showed it in state `Restarting (1) N seconds ago` indefinitely.

The maintain container is a redundant mirror of the production
`memory-cortex` API server, exposing its HTTP API on `:1881`
while the production one runs on `:1880`. Both run the same
`memory-cortex-stack:latest` image.

## Root cause

The container was originally started on 2026-09-02 with the
following command (from /var/log/auth.log):

```bash
docker run -d --name memory-cortex-maintain \
  --restart unless-stopped -p 1881:1880 \
  -v ...:/app/src:ro ... \
  --entrypoint bash \
  -e PG_HOST=127.0.0.1 \
  ... \
  memory-cortex-stack:latest \
  -c '... exec python3 -m src.api_pg'
```

Two problems:

1. **`--entrypoint bash -c '...'` bypassed the image's own
   `entrypoint.sh`.** The image's entrypoint initializes the
   bundled PostgreSQL data dir (`/var/lib/postgresql/data`),
   starts `postgres -D ... -c listen_addresses='*'`, sets the
   superuser password, and installs the pgvector extension,
   all *before* exec'ing `python3 -m src.api_pg`. By skipping
   it, the api_pg started with no Postgres running at
   `localhost:5432` -- hence the recurring
   "connection refused" traceback.

2. **`PG_HOST=127.0.0.1` was hardcoded to an IP address.**
   Even when the operator tried to fix the connection
   target by pointing it at the external `quantplatform-postgres`
   container by service name (`quantplatform-postgres`), that
   image uses the `quantplatform` user, not `postgres`, so
   `password authentication failed for user "postgres"`.

So the container never worked after the original 2026-09-02
launch; the user's symptom "always paused" was the
`Restarting (1)` loop continuing because of one or the other
failure mode.

## Fix

Deployed `/opt/cortex-deploy/scripts/cortex-maintain.sh` on
the 8.2 host. It is idempotent. The new run uses:

* the image's own ENTRYPOINT (no `--entrypoint bash` override)
* a named volume `cortex-maintain-pgdata` for
  `/var/lib/postgresql/data` so the bundled PG keeps its data
  across restarts
* `PG_HOST=localhost` so the api_pg connects to the bundled
  PG inside the same container, just like the production
  `memory-cortex` container does
* the same bind-mounted source / schema / entrypoint /
  models volumes as production
* after first-boot, manually applies
  `/opt/cortex/schema.sql` via `psql` inside the container
  because the api_pg process does NOT auto-bootstrap the
  schema on first run -- it relies on the schema having
  been created previously (which is the case on production
  because the maintain container starts from a fresh PGDATA)

## State after fix

```
$ docker ps -a --filter "name=memory-cortex"
NAME                   STATUS                    PORTS
memory-cortex-maintain Up About a minute (healthy) 5432/tcp, 0.0.0.0:1881->1880/tcp
memory-cortex          Up 2 days (healthy)         0.0.0.0:1880->1880/tcp, 5432/tcp

$ curl -s http://127.0.0.1:1881/health | jq .
{ "status": "healthy", "version": "6.5.1", "vectors": 0, "chunks": 0 }
```

The maintain container has its own empty PG (the bundled one
inside the container). It does NOT share data with the
production `memory-cortex` container -- that's a deliberate
choice in the maintain container's design (a separate
PG instance for redundancy tests), not in scope here.

## Future work

If maintain is meant to actually carry production data, the
script needs a replication or restore-from-prod step. Out of
scope for this fix.