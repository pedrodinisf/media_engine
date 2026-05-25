#!/usr/bin/env bash
# media_engine — restore from a backup.sh artifact directory.
#
# Usage:  bash deploy/hetzner/restore.sh deploy/hetzner/backups/<timestamp>
#
# Will:
#   - confirm interactively (DESTRUCTIVE)
#   - stop the engine container
#   - drop + recreate the postgres database, then load the pg_dump
#   - wipe + repopulate the engine-store named volume from the tarball
#   - bring the engine back up
#   - run alembic migrate (in case the backup is older than current schema)
#
# Caddy + cert volumes are NOT touched.

set -euo pipefail
# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
require_env

backup_dir="${1:-}"
if [[ -z "${backup_dir}" || ! -d "${backup_dir}" ]]; then
    err "Usage: bash deploy/hetzner/restore.sh <backup-dir>"
    err "Available:"
    ls -1 "${SCRIPT_DIR}/backups/" 2>/dev/null | sed 's|^|  |' >&2 || true
    exit 1
fi
backup_dir="$(cd "${backup_dir}" && pwd)"

pg_sql="${backup_dir}/postgres.sql.gz"
store_tgz="${backup_dir}/engine-store.tar.gz"
for f in "${pg_sql}" "${store_tgz}"; do
    if [[ ! -f "${f}" ]]; then
        err "missing: ${f}"
        exit 1
    fi
done

warn "DESTRUCTIVE: this drops the cache DB and wipes the engine-store volume."
warn "Source: ${backup_dir}"
printf "Type 'restore' to confirm: " >&2
read -r confirm
if [[ "${confirm}" != "restore" ]]; then
    err "aborted"
    exit 1
fi

# shellcheck disable=SC1090
set -a; . "${ENV_FILE}"; set +a

log "stopping engine..."
dc stop engine

log "dropping + recreating cache DB..."
dc exec -T postgres \
    psql -U media_engine -d postgres -v ON_ERROR_STOP=1 -c \
    "DROP DATABASE IF EXISTS media_engine; CREATE DATABASE media_engine OWNER media_engine;"
# Re-create the pgvector extension on the fresh DB.
dc exec -T postgres \
    psql -U media_engine -d media_engine -v ON_ERROR_STOP=1 \
    -c 'CREATE EXTENSION IF NOT EXISTS vector;'

log "restoring pg_dump..."
gunzip -c "${pg_sql}" \
    | dc exec -T postgres psql -U media_engine -d media_engine -v ON_ERROR_STOP=1

log "wiping + repopulating engine-store volume..."
volume_name="$(dc config --format json 2>/dev/null \
    | jq -r '.volumes["engine-store"].name // "media_engine_engine-store"')"
docker run --rm -v "${volume_name}:/dst" alpine:3.20 sh -c 'rm -rf /dst/* /dst/.[!.]* /dst/..?* 2>/dev/null || true'
docker run --rm \
    -v "${volume_name}:/dst" \
    -v "${backup_dir}:/src:ro" \
    alpine:3.20 \
    sh -c 'cd /dst && tar xzf /src/engine-store.tar.gz'

log "starting engine..."
dc up -d engine

log "alembic migrate (in case the backup is older than current schema)..."
sleep 5
dc exec -T engine med db migrate

log "done. Verify with: bash deploy/hetzner/doctor.sh"
