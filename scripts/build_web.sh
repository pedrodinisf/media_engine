#!/usr/bin/env bash
# Phase 6 commit 40 — build the SvelteKit SPA into media_engine/web/dist/.
#
# Invocation:
#   - CI / `hatch build`: runs this first so the wheel ships the dist tree.
#   - Local dev: contributors run this once before `med web start`.
#
# Idempotent. No-op when the lockfile + sources haven't changed (pnpm's
# install + vite's incremental build both handle that).

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}/web"

# Prefer corepack so we always run the exact `packageManager` version
# pinned in web/package.json, regardless of whatever `pnpm` happens to
# be on PATH — a mismatched global pnpm (e.g. an older major) chokes on
# pnpm-workspace.yaml settings that only exist in newer schema versions.
if command -v corepack >/dev/null 2>&1; then
    pnpm() { corepack pnpm "$@"; }
elif ! command -v pnpm >/dev/null 2>&1; then
    echo "[build_web] Neither corepack nor pnpm found on PATH. Install via:" >&2
    echo "    corepack enable && corepack prepare pnpm@latest --activate" >&2
    exit 1
fi

echo "[build_web] pnpm install --frozen-lockfile"
pnpm install --frozen-lockfile

echo "[build_web] pnpm build"
pnpm build

echo "[build_web] dist tree:"
ls -la "${repo_root}/media_engine/web/dist" | head -10
