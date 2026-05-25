# media_engine — shared shell helpers for the deploy/hetzner/ scripts.
#
# Source this from update.sh, backup.sh, restore.sh, logs.sh, doctor.sh,
# shell.sh. Provides the `dc()` compose wrapper, path globals, and
# small log helpers. Bootstrap.sh inlines its own copy of these (it
# runs before this file is necessarily present at deploy time).

# shellcheck shell=bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

LOG_PREFIX="\033[1;36m[$(basename "${BASH_SOURCE[1]}" .sh)]\033[0m"
WARN_PREFIX="\033[1;33m[warn]\033[0m"
ERR_PREFIX="\033[1;31m[error]\033[0m"

log()  { printf "%b %s\n" "${LOG_PREFIX}" "$*"; }
warn() { printf "%b %s\n" "${WARN_PREFIX}" "$*"; }
err()  { printf "%b %s\n" "${ERR_PREFIX}" "$*" >&2; }

require_env() {
    if [[ ! -f "${ENV_FILE}" ]]; then
        err "${ENV_FILE} missing — run bootstrap.sh first."
        exit 1
    fi
}

dc() {
    # See bootstrap.sh dc() — do NOT pass --project-directory.
    docker compose \
        --env-file "${ENV_FILE}" \
        -f "${REPO_ROOT}/infra/docker/docker-compose.yaml" \
        -f "${REPO_ROOT}/deploy/hetzner/docker-compose.override.yaml" \
        "$@"
}
