#!/usr/bin/env bash
# Phase 6.7 — verify the observability surface end-to-end against a real
# browser. Boots a clean `med web start`, mints a token, drives the
# Playwright spec at `web/tests/e2e/flows/observability.spec.ts`, and
# tears down the temp store on exit.
#
# Specs:
#   * Job-detail tab bar exposes the new "Logs" tab.
#   * Logs tab source-filter dropdown is wired.
#   * A `video.extract_audio` submission streams ffmpeg LogLines into
#     the Logs tab within 10s (proves the log_pump backend wiring).
#
# Operator-invoked. Exits non-zero if any spec fails.
#
# Prerequisites: `uv sync`, `pnpm -C web install`, chromium downloaded
# (`pnpm -C web exec playwright install chromium`).
#
#   bash scripts/verify_observability.sh           # headless
#   bash scripts/verify_observability.sh --headed  # opens a window

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:-}"

for cmd in uv pnpm jq curl; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        echo "[verify_observability] missing required tool: ${cmd}" >&2
        exit 1
    fi
done

work_dir="$(mktemp -d -t med_obs.XXXXXX)"
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
export MEDIA_ENGINE_NAMESPACE="obs-verify"
export MEDIA_ENGINE_CACHE_DB_URL="sqlite+pysqlite:///${work_dir}/cache.db"
export MEDIA_ENGINE_MIN_FREE_GB="0"
export MEDIA_ENGINE_NO_BROWSER="1"
mkdir -p "${MEDIA_ENGINE_PERMANENT_STORE}"

cd "${repo_root}"

echo "[verify_observability] migrating cache schema"
uv run med db migrate >/dev/null

echo "[verify_observability] minting bootstrap token"
token="$(uv run med api token create \
    --json \
    --label obs-verify \
    | jq -r .secret)"
if [[ -z "${token}" || "${token}" == "null" ]]; then
    echo "[verify_observability] token mint failed" >&2
    exit 1
fi

port="8771"
echo "[verify_observability] booting med web start on :${port}"
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
    echo "[verify_observability] engine did not become ready in 30s; tail of log:" >&2
    tail -40 "${work_dir}/server.log" >&2 || true
    exit 1
fi

playwright_args=(
    "tests/e2e/flows/observability.spec.ts"
    "--project=chromium"
    "--reporter=list"
)
if [[ "${mode}" == "--headed" ]]; then
    playwright_args+=("--headed")
fi

echo "[verify_observability] driving Playwright"
cd "${repo_root}/web"
MEDIA_ENGINE_WEB_E2E_BASE_URL="http://127.0.0.1:${port}" \
MEDIA_ENGINE_WEB_E2E_TOKEN="${token}" \
MEDIA_ENGINE_WEB_E2E_FIXTURE="${repo_root}/tests/fixtures/sample.mp4" \
    pnpm exec playwright test "${playwright_args[@]}"

echo "[verify_observability] PASS — Logs tab + gauges + LogLine pipe green"
