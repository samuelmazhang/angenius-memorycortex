# syntax=docker/dockerfile:1.4

# Memory Cortex unified container (image tag :latest; source version in api_pg.py)
# Architecture (2026-09-01 venv migration):
#   * image: python:3.11-slim + postgresql-16 + pgvector + python3-venv
#   * /opt/cortex-venv: venv with all business pip packages (baked at build)
#   * /app/src:      bind-mounted RO from host (git pull updates instantly)
#   * /opt/cortex/schema.sql + /usr/local/bin/entrypoint.sh: bind-mounted RO
#   * /var/lib/postgresql/data: docker volume (persists across restarts)
#   * /root/.arcadia/plugins/memory-cortex/models: bind-mounted RO
#
# Hot update flow (no image rebuild):
#   git push && ssh 8.2 'cd /opt/cortex-deploy/work && git pull && #     sudo /opt/cortex-deploy/cortex-deploy.sh'   (no image rebuild)
#
# Image rebuild flow (only when Dockerfile or business deps change):
#   sudo /opt/cortex-deploy/cortex-rebuild.sh

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# Add PostgreSQL + pgvector + python3-venv from domestic apt mirrors.
# Default deb.debian.org / apt.postgresql.org are slow/unreachable from
# the 8.2 lab in China, so we route through:
#   - Debian: mirrors.ustc.edu.cn (primary) + mirrors.aliyun.com (fallback)
#   - PostgreSQL pgdg: mirrors.aliyun.com
# Both signed by the same keys as upstream, so the packages are
# bit-identical to deb.debian.org / apt.postgresql.org.
# Two-phase apt setup:
#   Phase A: use the default deb.debian.org repos to install wget + gnupg +
#            ca-certificates (the only tools needed to bootstrap the
#            China mirrors). Default repos are reachable for this one-shot
#            bootstrap; all subsequent operations go through China mirrors.
#   Phase B: swap in USTC + Aliyun pgdg, install the real deps.
# python:3.11-slim ships without wget/gnupg, so we cannot pipe pgdg.asc
# through them BEFORE they are installed.
RUN set -e \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       wget gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    # Disable default debian.sources so apt-get does not try to reach
# deb.debian.org first. Replace with USTC + Aliyun in DEB822 format.
    && rm -f /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list \
    && printf 'Types: deb\nURIs: https://mirrors.ustc.edu.cn/debian\nSuites: trixie trixie-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: https://mirrors.ustc.edu.cn/debian-security\nSuites: trixie-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' \
       > /etc/apt/sources.list.d/ustc.sources \
    && printf 'Types: deb\nURIs: https://mirrors.aliyun.com/debian\nSuites: trixie trixie-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' \
       > /etc/apt/sources.list.d/aliyun.sources \
    # PostgreSQL pgdg key + repo, via Aliyun mirror
    && wget -qO - https://mirrors.aliyun.com/postgresql/repos/apt/ACCC4CF8.asc \
       | gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg \
    && echo 'deb https://mirrors.aliyun.com/postgresql/repos/apt trixie-pgdg main' \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       lsb-release curl gosu python3-venv \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*

# Build a venv at /opt/cortex-venv with ALL business deps baked in.
# Speed: pin fastmcp 2.x (4.x pulls 60+ transitive deps via py-key-value-aio /
# jaraco.* / mcp 2.x and takes 15+ min from default PyPI). Use Tsinghua
# mirror to skip the slow PyPI round-trips from China; 60s timeout
# prevents a single slow mirror slot from hanging the build.
#
# After this RUN, the venv is the source of truth for ALL pip deps. To
# add/upgrade a package, edit this RUN and rebuild the image. The
# running containers do not need to re-install anything on start.
#
# V6.6.0 fix (2026-10-30): sentence-transformers + torch are REQUIRED, not
# optional. The corpus was built with nomic-embed-text-v1.5 (768-dim). When
# this dependency was missing, get_nomic_model() returned "unavailable" and
# embed_text() silently fell back to mpnet while the caller still stored
# model_version='nomic-v1.5' — splitting the corpus into two incompatible
# vector spaces under one label and making 2333 of 2583 chunks
# vector-invisible (measured: 30/30 of the vector channel's top-30 came from
# the other 250 chunks).
#
# The CPU-only torch wheel is used deliberately: the deployment target has no
# GPU (nvidia-smi absent) and the CUDA wheel pulls ~5.5GB of nvidia-* libs
# for no benefit. The PyTorch CPU index is used for torch itself; the rest
# still comes from the Tsinghua mirror.
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m venv /opt/cortex-venv \
    && /opt/cortex-venv/bin/pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && /opt/cortex-venv/bin/pip config set global.timeout 60 \
    && /opt/cortex-venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/cortex-venv/bin/pip install \
       fastapi 'uvicorn[standard]' psycopg2-binary pydantic onnxruntime tokenizers \
       requests numpy python-dotenv \
       'fastmcp>=2.0,<3' \
       opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp \
    && /opt/cortex-venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && /opt/cortex-venv/bin/pip install sentence-transformers

# Make venv the default Python for the container. entrypoint.sh uses
# `exec python -m src.api_pg` and this PATH setting makes `python`
# resolve to /opt/cortex-venv/bin/python.
ENV PATH="/opt/cortex-venv/bin:${PATH}" \
    VIRTUAL_ENV="/opt/cortex-venv" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# Init script
COPY server/docker/unified/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# DO NOT COPY server/src/ or VERSION -- they come from bind mounts
# at /app/src and /app/VERSION. Source updates via git pull + restart,
# no image rebuild needed. Bind mounts configured at container start:
#   -v /opt/cortex-deploy/work/server/src:/app/src:ro
#   -v /opt/cortex-deploy/work/VERSION:/app/VERSION:ro
#   -v /opt/cortex-deploy/work/server/docker/unified/schema.sql:/opt/cortex/schema.sql:ro
#   -v /opt/cortex-deploy/work/server/docker/unified/entrypoint.sh:/usr/local/bin/entrypoint.sh:ro

# Image metadata (image tag is :latest; app.version lives in api_pg.py)
LABEL org.opencontainers.image.title="memory-cortex-stack" \
      org.opencontainers.image.description="Memory Cortex unified (PG+pgvector+cortex) — venv-baked deps, bind-mounted source. The running app.version is in api_pg.py and updates via git pull + container restart." \
      org.opencontainers.image.source="https://127.0.0.1:16080/<user>/Memory-Cortex" \
      org.opencontainers.image.created="2026-09-01"

EXPOSE 1880 5432

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -sS -m 5 http://localhost:1880/health 2>/dev/null | grep -q healthy || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
