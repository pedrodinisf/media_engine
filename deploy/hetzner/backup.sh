#!/usr/bin/env bash
# media_engine — back up Postgres + the artifact/HF-cache volume to a
# timestamped directory under deploy/hetzner/backups/. Pushing to an
# offsite location (Hetzner Storage Box via rclone, S3, etc.) is left
# to a per-operator cron — see docs/Hetzner_Deployment_Handbook.md §7.

set -euo pipefail
# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
require_env

ts="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="${SCRIPT_DIR}/backups/${ts}"
mkdir -p "${backup_dir}"
chmod 700 "${SCRIPT_DIR}/backups" "${backup_dir}"

# shellcheck disable=SC1090
set -a; . "${ENV_FILE}"; set +a

log "Postgres: pg_dump → ${backup_dir}/postgres.sql.gz"
dc exec -T postgres \
    pg_dump -U media_engine -d media_engine --no-owner --no-privileges \
    | gzip > "${backup_dir}/postgres.sql.gz"
size_pg=$(du -h "${backup_dir}/postgres.sql.gz" | cut -f1)
log "  ${size_pg}"

log "Artifact volume: engine-store → ${backup_dir}/engine-store.tar.gz"
# Run a throwaway alpine container with the named volume mounted and
# tar the contents straight onto the host filesystem.
volume_name="$(dc config --format json 2>/dev/null \
    | jq -r '.volumes["engine-store"].name // "media_engine_engine-store"')"
docker run --rm \
    -v "${volume_name}:/src:ro" \
    -v "${backup_dir}:/dst" \
    alpine:3.20 \
    sh -c 'cd /src && tar czf /dst/engine-store.tar.gz .'
size_vol=$(du -h "${backup_dir}/engine-store.tar.gz" | cut -f1)
log "  ${size_vol}"

# Record the engine version + secrets.env (mode 600 in the backup too).
log "Manifest: version + .env + secrets.env"
dc exec -T engine python -c 'import media_engine; print(media_engine.__version__)' \
    > "${backup_dir}/engine-version.txt" 2>/dev/null || true
cp "${ENV_FILE}" "${backup_dir}/.env"
cp "${SCRIPT_DIR}/secrets.env" "${backup_dir}/secrets.env"
chmod 600 "${backup_dir}/.env" "${backup_dir}/secrets.env"

log "Done: ${backup_dir}"
log "  Total: $(du -sh "${backup_dir}" | cut -f1)"
log ""
log "Offsite copy (example, rclone to Hetzner Storage Box):"
log "  rclone copy --progress ${backup_dir} hetzner-sb:media_engine/${ts}/"
