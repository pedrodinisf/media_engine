# media_engine — production container.
#
# Four-stage build:
# - ui-build  builds the SvelteKit SPA into media_engine/web/dist/. Node
#             lives ONLY in this stage; the runtime image stays Node-free.
# - builder   uses `uv` to resolve + install the runtime Python deps.
# - api-only  the headless Python runtime (REST + MCP, no /ui mount).
#             Use with `docker build --target api-only` for deployments
#             that don't want the GUI.
# - runtime   extends api-only with the prebuilt SPA dist tree. This is
#             the default final stage — `docker build .` produces this.
#
# Targets linux/amd64 and linux/arm64 via `docker buildx --platform`.
# The default CMD boots the REST API on 0.0.0.0:8000; override with
# `med daemon start --foreground` or any other med subcommand.

# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.11
ARG NODE_VERSION=22

# ─────────────────────────────────────────────────────────────────────
# Stage 1: ui-build — SvelteKit SPA → media_engine/web/dist/
# ─────────────────────────────────────────────────────────────────────
FROM node:${NODE_VERSION}-bookworm-slim AS ui-build

# pnpm via corepack — pinned in web/package.json's `packageManager` field.
RUN corepack enable

WORKDIR /work

# Copy ONLY the web source. svelte.config.js writes the dist tree to
# `../media_engine/web/dist` (a sibling of web/), so we need /work/web/
# laid out the same way the repo is. pnpm install creates node_modules
# under web/ and pnpm build writes the output via the relative path.
COPY web ./web
RUN pnpm -C web install --frozen-lockfile \
    && pnpm -C web build

# The build above leaves `/work/media_engine/web/dist/` populated.


# ─────────────────────────────────────────────────────────────────────
# Stage 2: builder — Python deps via uv
# ─────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

# uv is the project's package manager (see pyproject.toml [tool.uv]).
COPY --from=ghcr.io/astral-sh/uv:0.5.5 /uv /usr/local/bin/uv

# System deps the build step needs (ffmpeg lands in the runtime stage).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Resolve dependencies in a separate layer so source-only edits don't
# bust the dep cache.
COPY pyproject.toml uv.lock README.md ./
COPY media_engine ./media_engine
# alembic.ini lives at the repo root; the migration scripts ship inside
# the package at media_engine/_alembic/ (architecture.md §11 — "Alembic
# migrations ship inside the package"). cli/db.py builds the alembic
# Config in-process and pins script_location at the packaged dir, so a
# root-level alembic/ copy is intentionally absent — there is no
# alembic/ at the repo root, so the previous COPY here failed at build.
COPY alembic.ini ./alembic.ini

ENV UV_LINK_MODE=copy
RUN uv sync --no-dev --extra api --extra postgres --extra acquire-url --extra search --frozen


# ─────────────────────────────────────────────────────────────────────
# Stage 3: api-only — headless Python runtime (no /ui mount)
# ─────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS api-only

# Runtime apt packages:
#   ffmpeg   — required by every media op (ffmpeg + ffprobe)
#   libpq5   — psycopg's runtime library
#   curl     — used by the HEALTHCHECK + handy for ad-hoc debugging
#   ca-certificates — needed for any HTTPS fetch
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libpq5 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user to run the process; nothing else writes to / .
RUN useradd --create-home --shell /bin/bash engine
WORKDIR /home/engine/app

COPY --from=builder --chown=engine:engine /build /home/engine/app
ENV PATH="/home/engine/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEDIA_ENGINE_LOG_FORMAT=json \
    MEDIA_ENGINE_PERMANENT_STORE=/var/lib/media_engine

VOLUME ["/var/lib/media_engine"]

USER engine

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/ready || exit 1

CMD ["med", "api", "start", "--host", "0.0.0.0", "--port", "8000"]


# ─────────────────────────────────────────────────────────────────────
# Stage 4: runtime — api-only + prebuilt SPA dist tree (default target)
# ─────────────────────────────────────────────────────────────────────
FROM api-only AS runtime

# Drop the SvelteKit dist tree built in stage 1 into place. FastAPI's
# StaticFiles mount activates automatically when
# media_engine/web/dist/index.html is present (see
# media_engine/api/app.py).
COPY --from=ui-build --chown=engine:engine \
     /work/media_engine/web/dist \
     /home/engine/app/media_engine/web/dist
