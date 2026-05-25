#!/usr/bin/env bash
# media_engine — update the engine to the latest commit on the current
# branch. Postgres + Caddy are NOT touched (no DB downtime, no cert
# reissue).

set -euo pipefail
# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
require_env

log "git pull..."
git -C "${REPO_ROOT}" pull --ff-only

log "rebuild engine image..."
dc build engine

log "recreate engine container (no-deps; postgres + caddy stay up)..."
dc up -d --no-deps engine

log "wait for /ready (max 120s)..."
i=0
while (( i < 60 )); do
    if dc exec -T engine curl -fsS http://127.0.0.1:8000/ready >/dev/null 2>&1; then
        log "engine ready"
        log "running alembic migrate (idempotent)..."
        dc exec -T engine med db migrate
        log "done"
        exit 0
    fi
    sleep 2
    i=$((i+1))
done

err "engine did not become ready in 120s"
dc logs --tail 80 engine >&2
exit 1
