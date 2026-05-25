#!/usr/bin/env bash
# media_engine — one-shot Hetzner Ubuntu deploy.
#
# Idempotent. Safe to re-run. Walks 21 ordered steps:
#   pre-flight → apt → docker daemon → ufw → fail2ban → ssh hardening →
#   unattended-upgrades → swap → hetzner volume detect → repo → .env →
#   secrets.env → build → up → wait → migrate → token → doctor → summary.
#
# Run as a non-root user with passwordless sudo, from the repo root:
#   bash deploy/hetzner/bootstrap.sh
#
# See docs/Hetzner_Deployment_Handbook.md for the full operator guide.

set -euo pipefail

# ─── Paths + globals ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
SECRETS_FILE="${SCRIPT_DIR}/secrets.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
SECRETS_EXAMPLE="${SCRIPT_DIR}/secrets.env.example"
DAEMON_JSON="${SCRIPT_DIR}/daemon.json"
UU_CONF="${SCRIPT_DIR}/unattended-upgrades.conf"

# Container's engine user uid (Dockerfile: `useradd --create-home engine`,
# which on a fresh slim image gets uid 1000). Verified post-build at
# step 13b.
ENGINE_UID_DEFAULT=1000

LOG_PREFIX="\033[1;36m[bootstrap]\033[0m"
WARN_PREFIX="\033[1;33m[warn]\033[0m"
ERR_PREFIX="\033[1;31m[error]\033[0m"
OK_PREFIX="\033[1;32m[ok]\033[0m"

log()  { printf "%b %s\n" "${LOG_PREFIX}" "$*"; }
warn() { printf "%b %s\n" "${WARN_PREFIX}" "$*"; }
err()  { printf "%b %s\n" "${ERR_PREFIX}" "$*" >&2; }
ok()   { printf "%b %s\n" "${OK_PREFIX}" "$*"; }

trap_summary() {
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        err "bootstrap aborted with exit code ${rc}"
        err "re-run after fixing; all completed steps are idempotent"
    fi
}
trap trap_summary EXIT

# ─── dc(): compose wrapper — always includes both -f files
# NOTE: deliberately does NOT pass --project-directory. Compose
# resolves relative paths in -f files (build.context AND bind mount
# sources) against the project directory, which DEFAULTS to the dir
# of the first -f file (infra/docker/). Upstream's `context: ../..`
# is designed for that default; our override's `../../deploy/hetzner/`
# bind mounts walk back to the repo root from the same anchor.
# Overriding --project-directory breaks both.
dc() {
    docker compose \
        --env-file "${ENV_FILE}" \
        -f "${REPO_ROOT}/infra/docker/docker-compose.yaml" \
        -f "${REPO_ROOT}/deploy/hetzner/docker-compose.override.yaml" \
        "$@"
}

# ─── Step 1: pre-flight ─────────────────────────────────────────────
step_preflight() {
    log "Step 1/21 — pre-flight"

    if [[ ${EUID} -eq 0 ]]; then
        err "Run as a non-root user with passwordless sudo, not as root."
        err "  sudo adduser deploy && sudo usermod -aG sudo deploy"
        exit 1
    fi
    if ! sudo -n true 2>/dev/null; then
        err "Passwordless sudo is required for this user."
        err "  echo '${USER} ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/${USER}"
        exit 1
    fi

    # shellcheck disable=SC1091
    . /etc/os-release
    if [[ "${ID:-}" != "ubuntu" ]] || [[ ! "${VERSION_ID:-}" =~ ^(22\.04|24\.04)$ ]]; then
        err "Unsupported OS: ${PRETTY_NAME:-unknown}. Need Ubuntu 22.04 or 24.04."
        exit 1
    fi
    local arch
    arch="$(uname -m)"
    if [[ "${arch}" != "x86_64" ]]; then
        err "Unsupported arch: ${arch}. This script targets Hetzner CX/CPX (x86_64)."
        err "For ARM Ampere (CAX), validate the playwright chromium apt list first."
        exit 1
    fi

    # Check we're under the repo root (bootstrap lives in deploy/hetzner/).
    if [[ ! -f "${REPO_ROOT}/pyproject.toml" ]] || [[ ! -d "${REPO_ROOT}/infra/docker" ]]; then
        err "Repo layout looks wrong. Expected pyproject.toml + infra/docker/ at ${REPO_ROOT}."
        exit 1
    fi

    ok "ubuntu ${VERSION_ID} ${arch}, repo at ${REPO_ROOT}"
}

# ─── Step 2: apt baseline ───────────────────────────────────────────
step_apt_baseline() {
    log "Step 2/21 — apt baseline + Docker official repo"

    # Docker's official repo (avoids docker.io which lags + bundles old compose).
    if ! command -v docker >/dev/null 2>&1; then
        sudo install -m 0755 -d /etc/apt/keyrings
        sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            -o /etc/apt/keyrings/docker.asc
        sudo chmod a+r /etc/apt/keyrings/docker.asc
        local codename
        codename="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
            | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
        sudo apt-get update -qq
        sudo apt-get install -y \
            docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin
    fi

    # Other host packages.
    local pkgs=(ufw fail2ban jq curl git ca-certificates unattended-upgrades)
    local missing=()
    for p in "${pkgs[@]}"; do
        if ! dpkg -s "${p}" >/dev/null 2>&1; then
            missing+=("${p}")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        sudo apt-get update -qq
        sudo apt-get install -y "${missing[@]}"
    fi

    ok "docker $(docker --version | awk '{print $3}' | tr -d ',') + apt baseline ready"
}

# ─── Step 3: docker daemon hardening ────────────────────────────────
step_docker_daemon() {
    log "Step 3/21 — docker daemon.json"
    sudo install -m 0644 "${DAEMON_JSON}" /etc/docker/daemon.json
    sudo systemctl restart docker
    ok "daemon.json installed; docker restarted"
}

# ─── Step 4: docker group ───────────────────────────────────────────
# If the user isn't yet in the docker group, add them and exit so they
# can re-login (or `newgrp docker`). The script is idempotent — the
# second invocation passes this check and continues. We deliberately
# do NOT try to wrap subsequent steps in `sg docker -c '...'`: that
# path is hostile to function definitions, variable scoping, and
# argument quoting, and the round-trip cost of one re-login is small.
step_docker_group() {
    log "Step 4/21 — docker group membership"
    if ! id -nG "${USER}" | grep -qw docker; then
        sudo usermod -aG docker "${USER}"
        warn "Added '${USER}' to the docker group."
        warn "Please log out + back in (or run: newgrp docker)"
        warn "then re-run this script. Everything done so far is idempotent."
        exit 0
    fi
    # Sanity check: can we actually talk to the daemon?
    if ! docker info >/dev/null 2>&1; then
        err "User is in docker group but 'docker info' failed."
        err "Try: newgrp docker  (or log out + back in), then re-run."
        exit 1
    fi
    ok "user '${USER}' can talk to the docker daemon"
}

# ─── Step 5: UFW ────────────────────────────────────────────────────
step_ufw() {
    log "Step 5/21 — UFW (host firewall)"
    sudo ufw --force default deny incoming >/dev/null
    sudo ufw --force default allow outgoing >/dev/null
    sudo ufw allow 22/tcp >/dev/null
    sudo ufw allow 80/tcp >/dev/null
    sudo ufw allow 443/tcp >/dev/null
    sudo ufw --force enable >/dev/null
    ok "ufw: 22, 80, 443 open; default deny incoming"
}

# ─── Step 6: fail2ban ───────────────────────────────────────────────
step_fail2ban() {
    log "Step 6/21 — fail2ban"
    sudo tee /etc/fail2ban/jail.d/sshd-local.conf >/dev/null <<'EOF'
[sshd]
enabled  = true
port     = ssh
maxretry = 3
findtime = 10m
bantime  = 1h
EOF
    sudo systemctl enable --now fail2ban >/dev/null
    sudo systemctl restart fail2ban
    ok "fail2ban sshd jail active (3 retries / 10m, 1h ban)"
}

# ─── Step 7: SSH hardening ──────────────────────────────────────────
step_ssh_hardening() {
    log "Step 7/21 — SSH hardening"
    local authkeys="${HOME}/.ssh/authorized_keys"
    if [[ ! -s "${authkeys}" ]]; then
        warn "${authkeys} is empty or missing — SKIPPING ssh hardening to avoid lockout."
        warn "  After uploading a key:"
        warn "    1) ssh-copy-id ${USER}@<this-host>"
        warn "    2) re-run this script (idempotent)"
        return 0
    fi
    sudo tee /etc/ssh/sshd_config.d/99-media-engine-hardening.conf >/dev/null <<EOF
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
UsePAM yes
AllowUsers ${USER}
EOF
    sudo systemctl reload ssh
    ok "ssh: key-only, root login disabled, AllowUsers=${USER}"
}

# ─── Step 8: unattended-upgrades ────────────────────────────────────
step_unattended_upgrades() {
    log "Step 8/21 — unattended security upgrades"
    sudo install -m 0644 "${UU_CONF}" \
        /etc/apt/apt.conf.d/52unattended-upgrades-local
    sudo systemctl enable --now unattended-upgrades >/dev/null
    ok "security-only auto-patching enabled (no auto-reboot)"
}

# ─── Step 9: swap ───────────────────────────────────────────────────
step_swap() {
    log "Step 9/21 — swap (4 GB if absent)"
    if [[ -n "$(swapon --show --noheadings)" ]]; then
        ok "swap already configured: $(swapon --show --bytes --noheadings | awk '{print $1, $3}')"
    else
        sudo fallocate -l 4G /swapfile
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile >/dev/null
        sudo swapon /swapfile
        if ! grep -q '^/swapfile' /etc/fstab; then
            echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
        fi
        ok "swap: 4 GB /swapfile enabled"
    fi
    echo 'vm.swappiness = 10' | sudo tee /etc/sysctl.d/99-media-engine-swappiness.conf >/dev/null
    sudo sysctl -q -w vm.swappiness=10
    ok "vm.swappiness = 10"
}

# ─── Step 10: Hetzner volume detection (informational) ──────────────
step_hetzner_volume() {
    log "Step 10/21 — Hetzner Cloud Volume detection"
    local vols
    vols=$(ls -d /mnt/HC_Volume_* 2>/dev/null || true)
    if [[ -z "${vols}" ]]; then
        warn "No Hetzner Cloud Volume detected under /mnt/HC_Volume_*."
        warn "Running on the root disk. For >20 GB of HF model cache +"
        warn "artifacts, attach a Volume in Hetzner Console, then re-run."
        warn "(Bootstrap continues; this is advisory.)"
        return 0
    fi
    ok "detected Hetzner Volume(s): ${vols}"
    warn "Volume-based storage is documented in the handbook §9. The"
    warn "current bootstrap uses named docker volumes (engine-store,"
    warn "postgres-data) on the root disk. To bind those onto the"
    warn "Hetzner Volume, see handbook §9 'Sizing, storage, swap'."
}

# ─── Step 11: disk space check ──────────────────────────────────────
step_disk_check() {
    log "Step 11/21 — free disk space"
    local free_gb
    free_gb=$(df -BG --output=avail /var/lib | tail -1 | tr -dc '0-9')
    if [[ "${free_gb}" -lt 20 ]]; then
        err "Only ${free_gb} GB free at /var/lib — bootstrap needs ≥20 GB."
        err "Mount a Hetzner Volume or resize the root disk first."
        exit 1
    fi
    ok "${free_gb} GB free at /var/lib"
}

# ─── Step 12: .env materialization ──────────────────────────────────
step_env_file() {
    log "Step 12/21 — .env (compose-level config)"
    if [[ ! -f "${ENV_FILE}" ]]; then
        cp "${ENV_EXAMPLE}" "${ENV_FILE}"
    fi
    chmod 600 "${ENV_FILE}"

    # shellcheck disable=SC1090
    set -a; . "${ENV_FILE}"; set +a

    prompt_var() {
        local name="$1" label="$2" current default="${3:-}"
        current="$(grep -E "^${name}=" "${ENV_FILE}" | head -1 | cut -d= -f2-)"
        if [[ -z "${current}" || "${current}" == *example* ]]; then
            local val
            printf "  %s [%s]: " "${label}" "${default:-required}" >&2
            read -r val
            val="${val:-${default}}"
            if [[ -z "${val}" ]]; then
                err "${name} is required."
                exit 1
            fi
            # sed in-place (BSD/GNU-compatible-ish via a tmp file).
            local tmp
            tmp=$(mktemp)
            awk -v k="${name}" -v v="${val}" '
                BEGIN { kv = k "=" v }
                $0 ~ "^" k "=" { print kv; next }
                { print }
            ' "${ENV_FILE}" > "${tmp}"
            # Append if the key wasn't already in the file.
            if ! grep -q "^${name}=" "${tmp}"; then
                echo "${name}=${val}" >> "${tmp}"
            fi
            mv "${tmp}" "${ENV_FILE}"
            chmod 600 "${ENV_FILE}"
        fi
    }

    prompt_var MEDIA_ENGINE_DOMAIN "Public domain (DNS A-record points here)"
    prompt_var MEDIA_ENGINE_ACME_EMAIL "Let's Encrypt notification email"

    # Postgres password: auto-generate if empty.
    if ! grep -qE '^POSTGRES_PASSWORD=.+' "${ENV_FILE}"; then
        local pw
        pw="$(openssl rand -base64 24 | tr -d '\n')"
        awk -v v="${pw}" '$0 ~ /^POSTGRES_PASSWORD=/ { print "POSTGRES_PASSWORD=" v; next } { print }' \
            "${ENV_FILE}" > "${ENV_FILE}.tmp"
        mv "${ENV_FILE}.tmp" "${ENV_FILE}"
        chmod 600 "${ENV_FILE}"
        ok "auto-generated POSTGRES_PASSWORD"
    fi

    # Re-source.
    # shellcheck disable=SC1090
    set -a; . "${ENV_FILE}"; set +a
    ok ".env: domain=${MEDIA_ENGINE_DOMAIN}"
}

# ─── Step 13: secrets.env materialization ───────────────────────────
step_secrets_file() {
    log "Step 13/21 — secrets.env (API keys, mounted into container)"
    if [[ ! -f "${SECRETS_FILE}" ]]; then
        cp "${SECRETS_EXAMPLE}" "${SECRETS_FILE}"
    fi
    chmod 600 "${SECRETS_FILE}"

    prompt_secret() {
        local name="$1" label="$2" current
        current="$(grep -E "^${name}=" "${SECRETS_FILE}" | head -1 | cut -d= -f2-)"
        if [[ -z "${current}" ]]; then
            local val
            printf "  %s (blank to skip): " "${label}" >&2
            read -r val
            if [[ -n "${val}" ]]; then
                awk -v k="${name}" -v v="${val}" '
                    $0 ~ "^" k "=" { print k "=" v; next }
                    { print }
                ' "${SECRETS_FILE}" > "${SECRETS_FILE}.tmp"
                mv "${SECRETS_FILE}.tmp" "${SECRETS_FILE}"
                chmod 600 "${SECRETS_FILE}"
            fi
        fi
    }

    prompt_secret GEMINI_API_KEY    "Google Gemini API key"
    prompt_secret ANTHROPIC_API_KEY "Anthropic Claude API key"
    prompt_secret OPENAI_API_KEY    "OpenAI API key"
    prompt_secret HF_TOKEN          "Hugging Face token (needed for pyannote/diarize)"

    # Chown to the container's engine uid so the in-container user can
    # read+write. The uid is verified post-build; we use the default
    # here and re-chown after the image is built.
    sudo chown "${ENGINE_UID_DEFAULT}:${ENGINE_UID_DEFAULT}" "${SECRETS_FILE}"
    sudo chmod 600 "${SECRETS_FILE}"
    ok "secrets.env written (chmod 600, chown ${ENGINE_UID_DEFAULT}:${ENGINE_UID_DEFAULT})"
}

# ─── Step 14: build engine image ────────────────────────────────────
step_build() {
    log "Step 14/21 — build engine image (~10 min cold)"
    dc build engine
    ok "engine image built: media_engine:hetzner"

    # Verify the actual engine-user uid inside the built image; re-chown
    # the host secrets.env if it differs from our default.
    local real_uid
    real_uid="$(docker run --rm media_engine:hetzner id -u engine)"
    if [[ "${real_uid}" != "${ENGINE_UID_DEFAULT}" ]]; then
        warn "engine uid in image is ${real_uid}, not ${ENGINE_UID_DEFAULT}; re-chowning secrets.env"
        sudo chown "${real_uid}:${real_uid}" "${SECRETS_FILE}"
    fi
}

# ─── Step 15: postgres up + wait healthy ────────────────────────────
step_postgres_up() {
    log "Step 15/21 — postgres up"
    dc up -d postgres
    local i=0
    while (( i < 60 )); do
        local state
        # `dc ps --format json` emits one JSON-per-line in newer compose
        # versions; -rs slurps it into an array so the same jq works
        # for legacy array-shaped output too.
        state=$(dc ps --format json postgres 2>/dev/null \
                | jq -rs 'if length == 0 then "" else .[0].Health // "" end')
        if [[ "${state}" == "healthy" ]]; then
            ok "postgres healthy"
            return 0
        fi
        sleep 2
        i=$((i+1))
    done
    err "postgres did not become healthy in 120s"
    dc logs --tail 50 postgres >&2
    exit 1
}

# ─── Step 16: engine + caddy up ─────────────────────────────────────
step_engine_caddy_up() {
    log "Step 16/21 — engine + caddy up"
    dc up -d engine caddy
    ok "engine + caddy started"
}

# ─── Step 17: wait for TLS + /ready ─────────────────────────────────
step_wait_ready() {
    log "Step 17/21 — waiting for https://${MEDIA_ENGINE_DOMAIN}/ready"
    log "  (Let's Encrypt HTTP-01 issuance can take 30–90s on first run)"
    local i=0
    while (( i < 150 )); do
        if curl -fsSk --max-time 5 "https://${MEDIA_ENGINE_DOMAIN}/ready" >/dev/null 2>&1; then
            ok "https://${MEDIA_ENGINE_DOMAIN}/ready returned 200"
            return 0
        fi
        sleep 2
        i=$((i+1))
    done
    err "/ready did not return 200 within 5 minutes"
    err "Common causes:"
    err "  - DNS A-record for ${MEDIA_ENGINE_DOMAIN} doesn't point at this VPS"
    err "  - Hetzner Cloud Firewall blocking port 80 (HTTP-01 needs port 80)"
    err "  - Cert issuance rate-limited (5 per week per registered domain)"
    err "Caddy logs:"
    dc logs --tail 80 caddy >&2
    exit 1
}

# ─── Step 18: alembic migrate ───────────────────────────────────────
step_migrate() {
    log "Step 18/21 — alembic migrate (cache schema)"
    dc exec -T engine med db migrate
    ok "alembic upgrade head complete"
}

# ─── Step 19: bootstrap bearer token ────────────────────────────────
step_token() {
    log "Step 19/21 — minting bootstrap bearer token"
    # `med api token create` plain mode: secret on stdout, context on
    # stderr. dc exec -T avoids TTY allocation so stdout is clean.
    BOOTSTRAP_TOKEN="$(dc exec -T engine med api token create --label bootstrap 2>/dev/null | tail -1)"
    if [[ -z "${BOOTSTRAP_TOKEN}" ]]; then
        err "Empty token from 'med api token create'. Re-run:"
        err "  bash deploy/hetzner/shell.sh -c 'med api token create --label bootstrap'"
        exit 1
    fi
    ok "bootstrap token minted (saved to summary)"
}

# ─── Step 20: doctor matrix ─────────────────────────────────────────
step_doctor() {
    log "Step 20/21 — med doctor matrix"
    local report
    report="$(dc exec -T engine med doctor --json 2>/dev/null || true)"
    if [[ -z "${report}" ]]; then
        warn "med doctor returned no JSON; falling back to text output:"
        dc exec -T engine med doctor >&2 || true
        return 0
    fi

    # Pretty-print per-op status with green/red.
    echo "${report}" | jq -r '
        def color(s):
            if s == "ok" then "[32m" + s + "[0m"
            elif s == "degraded" then "[33m" + s + "[0m"
            elif s == "unavailable" then "[31m" + s + "[0m"
            else s end;
        "Doctor summary — ok=\(.summary.ok)  degraded=\(.summary.degraded)  unavailable=\(.summary.unavailable)",
        "",
        (.ops[] | "  \(color(.overall))\t\(.op_name)\t(default backend: \(.default_backend // "—"))")
    ' 2>/dev/null || echo "${report}"

    warn "Expected red: audio.transcribe (only the mlx-whisper backend exists; Apple-only)."
    warn "  Composites that delegate to it (audio.transcribe_diarized) will"
    warn "  inherit the red status — the doctor walker traverses delegates_to."
    warn "Possibly degraded: diarize.* (only if HF_TOKEN was left blank in secrets.env)."
}

# ─── Step 21: final summary ─────────────────────────────────────────
step_summary() {
    log "Step 21/21 — done"
    cat <<EOF

══════════════════════════════════════════════════════════════════════
  media_engine — Hetzner deploy ready
══════════════════════════════════════════════════════════════════════

  Web UI    https://${MEDIA_ENGINE_DOMAIN}/ui
  REST API  https://${MEDIA_ENGINE_DOMAIN}/
  Health    https://${MEDIA_ENGINE_DOMAIN}/health
  Ready     https://${MEDIA_ENGINE_DOMAIN}/ready

  Bootstrap bearer token (SAVE NOW — cannot be recovered):

    ${BOOTSTRAP_TOKEN}

  Test it:
    curl -H "Authorization: Bearer ${BOOTSTRAP_TOKEN}" \\
      https://${MEDIA_ENGINE_DOMAIN}/ops | jq .

  Day-2 operations:
    bash deploy/hetzner/update.sh    # git pull + rebuild engine
    bash deploy/hetzner/backup.sh    # pg_dump + tar artifact volume
    bash deploy/hetzner/doctor.sh    # op×backend health matrix
    bash deploy/hetzner/logs.sh      # tail container logs

  Volume locations (named docker volumes):
    engine-store    permanent artifacts + HF model cache
    postgres-data   cache.db + cost ledger + tokens (pgvector)
    caddy-data      Let's Encrypt certs (DO NOT lose this)
    caddy-config    Caddy runtime config

  Full handbook:
    docs/Hetzner_Deployment_Handbook.md

══════════════════════════════════════════════════════════════════════
EOF
}

# ─── main ───────────────────────────────────────────────────────────
main() {
    step_preflight
    step_apt_baseline
    step_docker_daemon
    step_docker_group
    step_ufw
    step_fail2ban
    step_ssh_hardening
    step_unattended_upgrades
    step_swap
    step_hetzner_volume
    step_disk_check
    step_env_file
    step_secrets_file
    step_build
    step_postgres_up
    step_engine_caddy_up
    step_wait_ready
    step_migrate
    step_token
    step_doctor
    step_summary
}

main "$@"
