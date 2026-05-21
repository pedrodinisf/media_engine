# media_engine — production container.
#
# Multi-stage build:
# - builder stage uses `uv` to resolve + install the runtime deps
# - runtime stage ships a slim Python image with only the wheel
#   environment + the source tree
#
# Targets linux/amd64 and linux/arm64 via `docker buildx --platform`.
# The default CMD boots the REST API on 0.0.0.0:8000; override with
# `med daemon start --foreground` or any other med subcommand.

# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.11

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
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

ENV UV_LINK_MODE=copy
RUN uv sync --no-dev --extra api --extra postgres --extra acquire-url --extra search --frozen


FROM python:${PYTHON_VERSION}-slim AS runtime

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

COPY --from=builder /build /home/engine/app
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
