#!/usr/bin/env bash
# Phase 6.5 — drive the Settings (Doctor + Secrets) + B-005 spec
# against a clean `med web start`. Boots an isolated tmp store +
# namespace, mints a bearer token, runs the Playwright spec, and
# tears down on exit.
#
# Operator-invoked. Exits non-zero if the spec fails.
#
#   bash scripts/verify_settings.sh             # headless
#   bash scripts/verify_settings.sh --headed    # watch live

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:-}"

for cmd in uv pnpm jq curl; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        echo "[verify_settings] missing required tool: ${cmd}" >&2
        exit 1
    fi
done

work_dir="$(mktemp -d -t med_settings.XXXXXX)"
server_pid=""

cleanup() {
    if [[ -n "${server_pid}" ]]; then
        kill "${server_pid}" 2>/dev/null || true
        wait "${server_pid}" 2>/dev/null || true
    fi
    rm -rf "${work_dir}"
}
trap cleanup EXIT

export MEDIA_ENGINE_PERMANENT_STORE="${work_dir}/store"
export MEDIA_ENGINE_NAMESPACE="settings-verify"
export MEDIA_ENGINE_CACHE_DB_URL="sqlite+pysqlite:///${work_dir}/cache.db"
export MEDIA_ENGINE_MIN_FREE_GB="0"
export MEDIA_ENGINE_NO_BROWSER="1"
export MEDIA_ENGINE_CONFIG_DIR="${work_dir}/config"
mkdir -p "${MEDIA_ENGINE_PERMANENT_STORE}" "${MEDIA_ENGINE_CONFIG_DIR}"

cd "${repo_root}"

echo "[verify_settings] migrating cache schema"
uv run med db migrate >/dev/null

echo "[verify_settings] minting bootstrap token"
token="$(uv run med api token create \
    --json \
    --label settings-verify \
    | jq -r .secret)"
if [[ -z "${token}" || "${token}" == "null" ]]; then
    echo "[verify_settings] token mint failed" >&2
    exit 1
fi

port="8768"
echo "[verify_settings] booting med web start on :${port}"
uv run med web start --host 127.0.0.1 --port "${port}" --no-open \
    >"${work_dir}/server.log" 2>&1 &
server_pid=$!

for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${port}/ready" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
if ! curl -fsS "http://127.0.0.1:${port}/ready" >/dev/null 2>&1; then
    echo "[verify_settings] engine did not become ready in 30s; tail of log:" >&2
    tail -40 "${work_dir}/server.log" >&2 || true
    exit 1
fi

playwright_args=(
    "tests/e2e/flows/settings_and_b005.spec.ts"
    "--project=chromium"
    "--reporter=list"
)
if [[ "${mode}" == "--headed" ]]; then
    playwright_args+=("--headed")
fi

echo "[verify_settings] driving Playwright"
cd "${repo_root}/web"
MEDIA_ENGINE_WEB_E2E_BASE_URL="http://127.0.0.1:${port}" \
MEDIA_ENGINE_WEB_E2E_TOKEN="${token}" \
    pnpm exec playwright test "${playwright_args[@]}"

echo "[verify_settings] PASS — Settings + B-005 specs green"
