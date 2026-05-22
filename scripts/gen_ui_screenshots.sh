#!/usr/bin/env bash
# Phase 6 commit 50 — regenerate the six bundled Web UI screenshots
# at docs/web_ui/.
#
# Operator-invoked (NOT part of the CI gate). Boots a clean engine on
# an isolated permanent_store + namespace, seeds synthetic fixture
# artifacts, drives Playwright through each panel, and writes:
#
#   docs/web_ui/ingest.png            (Ingest panel — upload tab)
#   docs/web_ui/run.png               (Run panel — op picker + form)
#   docs/web_ui/jobs.png              (Jobs dashboard — live table)
#   docs/web_ui/catalog-detail.png    (Catalog detail — Transcript preview)
#   docs/web_ui/lineage.png           (Lineage graph viewer)
#   docs/web_ui/profile-workspace.png (Profile composer + YAML editor)
#
# Prerequisites:
#   - `uv sync` (Python deps including --extra api)
#   - `pnpm -C web install --frozen-lockfile && pnpm -C web build`
#   - `pnpm -C web exec playwright install chromium`
#   - `jq` on PATH (used for token extraction)
#
# The script is idempotent: it tears down its temp store on exit so a
# rerun starts from a known-empty state.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="${repo_root}/docs/web_ui"
mkdir -p "${out_dir}"

# Tooling preflight — fail fast with actionable hints.
for cmd in uv pnpm jq curl; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        echo "[gen_ui_screenshots] missing required tool: ${cmd}" >&2
        exit 1
    fi
done

# Isolated workspace so the screenshots never touch real artifacts.
work_dir="$(mktemp -d -t med_screenshots.XXXXXX)"
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
export MEDIA_ENGINE_NAMESPACE="screenshots"
export MEDIA_ENGINE_MIN_FREE_GB="1"
export MEDIA_ENGINE_NO_BROWSER="1"
mkdir -p "${MEDIA_ENGINE_PERMANENT_STORE}"

cd "${repo_root}"

echo "[gen_ui_screenshots] migrating cache schema"
uv run med db migrate >/dev/null

echo "[gen_ui_screenshots] minting bootstrap token"
# Pass --namespace explicitly: `med api token create` defaults to
# "default" and doesn't read MEDIA_ENGINE_NAMESPACE on its own.
token="$(uv run med api token create \
    --json \
    --label screenshots \
    --namespace "${MEDIA_ENGINE_NAMESPACE}" \
    | jq -r .secret)"
if [[ -z "${token}" || "${token}" == "null" ]]; then
    echo "[gen_ui_screenshots] token mint failed" >&2
    exit 1
fi

echo "[gen_ui_screenshots] seeding synthetic fixture artifacts"
uv run python scripts/_screenshot_fixtures.py

echo "[gen_ui_screenshots] booting med web start on :8765"
port="8765"
uv run med web start --host 127.0.0.1 --port "${port}" --no-open \
    >"${work_dir}/server.log" 2>&1 &
server_pid=$!

# Wait for /ready (up to 30s).
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${port}/ready" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
if ! curl -fsS "http://127.0.0.1:${port}/ready" >/dev/null 2>&1; then
    echo "[gen_ui_screenshots] engine did not become ready in 30s; tail of log:" >&2
    tail -40 "${work_dir}/server.log" >&2 || true
    exit 1
fi

echo "[gen_ui_screenshots] driving Playwright"
cd "${repo_root}/web"
MEDIA_ENGINE_WEB_E2E_BASE_URL="http://127.0.0.1:${port}" \
MEDIA_ENGINE_WEB_E2E_TOKEN="${token}" \
MEDIA_ENGINE_WEB_E2E_OUT_DIR="${out_dir}" \
    pnpm exec playwright test tests/e2e/screenshots.spec.ts \
        --project=chromium \
        --reporter=list

echo "[gen_ui_screenshots] regenerated:"
ls -la "${out_dir}"/*.png 2>/dev/null || echo "  (none — Playwright run produced no PNGs)"
