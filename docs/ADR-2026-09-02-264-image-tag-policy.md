# ADR-264: image tag policy -- decouple from source version

- Status: Accepted
- Date: 2026-09-02
- Author: Arcadia (subagent dispatch) + 天行健 (decision)
- Affects: server/docker/unified/Dockerfile, scripts/cortex-rebuild.sh,
  8.2 host image inventory

## 背景 (Context)

Pre-264, the deployment pipeline conflated two unrelated concepts:

  * **image**: base environment (python:3.11-slim + postgresql-16 +
    pgvector + venv-baked pip packages + entrypoint.sh). Rebuilt only
    when apt/pip/python changes (hours-to-days cadence).

  * **source code**: git-tracked Python files in /app/src (bind-mounted
    from host, hot-updated via git pull + container restart, minutes
    cadence).

Old workflow tried to bump them together (`6.5.0 -> 6.5.1`):
  * source code VERSION bump in api_pg.py
  * source code app.version string
  * Dockerfile LABEL org.opencontainers.image.version
  * rebuild.sh NEW_TAG
  * IMAGE:6.5.X on 8.2 host
  * commit message

This was confusing because:
  * most commits touched source only -- image tag shouldn't change
  * the 5-min rebuild (venv pip install) is heavy for a 1-line source fix
  * the OCI LABEL `version` reported a stale value when the bind-mounted
    source was updated via git pull + restart

User clarification (2026-09-02): "image 跟 cortex 版本没有对应关系,
只是个环境, 代码是外挂" -- the image is just an environment, the code
is a plugin.

## 决策 (Decision)

### Image tag policy

  * Tag: `memory-cortex-stack:latest` (no version suffix)
  * Source code version: lives in `api_pg.py:app.version` and the top
    line of `VERSION` (git-tracked)
  * Hot update (source-only): `cortex-deploy.sh` -- `git pull` + restart
    container. Seconds. No image change.
  * Image rebuild (apt/pip/python): `cortex-rebuild.sh` -- rebuilds
    `:latest`. Minutes. Triggered only when Dockerfile changes.

### Changes

  * Dockerfile LABEL: dropped `org.opencontainers.image.version` and
    `.revision`. Description updated to point at api_pg.py for the
    running version. Top comment updated.
  * rebuild.sh: `NEW_TAG=latest` (was `6.5.0`). Header comment explains
    the tag policy.
  * 8.2 host: retagged existing image `memory-cortex-stack:6.5.0` ->
    `:latest` (no rebuild, just `docker tag`), then deleted the
    `:6.5.0` alias. No rebuild needed because we only changed the tag
    name, not the image contents.

### Future rules

  * NEVER bump `:latest` (it always means "latest base image"). The
    actual running app version is in `/health.version` response field.
  * When you change source code:
    1. update VERSION file
    2. update app.version in api_pg.py
    3. commit + push
    4. `cortex-deploy.sh` to hot-deploy
  * When you change base image (new apt packages, new pip packages,
    python version bump):
    1. edit Dockerfile
    2. commit + push
    3. `cortex-rebuild.sh` to rebuild :latest and restart container

## Verification

  * 8.2 host: `docker images | grep cortex` shows only `memory-cortex-stack:latest`
  * `docker inspect memory-cortex-stack:latest --format '{{.Id}}'` returns
    the same sha256 as before the rename (no layer rebuild)
  * `/health.version` still returns `"6.5.1"` (correct -- source version
    lives in code, not in image tag)
