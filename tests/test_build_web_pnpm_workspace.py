"""Regression — pnpm-workspace.yaml allows esbuild's lifecycle scripts.

Audit finding F-001. Without ``onlyBuiltDependencies: [esbuild]`` in
``web/pnpm-workspace.yaml``, pnpm 11 raises ``ERR_PNPM_IGNORED_BUILDS``
and ``scripts/build_web.sh`` exits 1 — a fresh contributor following
the CLAUDE.md onboarding instructions fails at the first onboarding
step. Pinning the workspace config here prevents that drift.

We don't shell out to ``pnpm install`` (slow, network-y, hosts may
lack pnpm); we just assert the config file declares the right keys.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def test_workspace_allowlists_esbuild_lifecycle() -> None:
    repo_root = Path(__file__).parent.parent
    cfg_path = repo_root / "web" / "pnpm-workspace.yaml"
    assert cfg_path.exists(), "web/pnpm-workspace.yaml is required by pnpm 11"
    with cfg_path.open() as f:
        cfg = yaml.safe_load(f)
    deps = cfg.get("onlyBuiltDependencies", [])
    assert "esbuild" in deps, (
        "esbuild must be in onlyBuiltDependencies — without it pnpm 11 "
        "exits 1 with ERR_PNPM_IGNORED_BUILDS during `pnpm install`."
    )
    assert cfg.get("verifyDepsBeforeRun") is False, (
        "verifyDepsBeforeRun must be False — otherwise pnpm 11 re-runs "
        "the lifecycle gate before every `pnpm <script>` invocation and "
        "fires ERR_PNPM_IGNORED_BUILDS even with onlyBuiltDependencies set."
    )
    # The allowBuilds value must be a real boolean, not a placeholder
    # string. This is the exact regression that shipped: allowBuilds
    # carried the literal text "set this to true or false", which pnpm
    # does not treat as approval, so `bash scripts/build_web.sh` failed
    # on a clean checkout despite onlyBuiltDependencies being set.
    allow_builds = cfg.get("allowBuilds") or {}
    assert allow_builds.get("esbuild") is True, (
        "allowBuilds.esbuild must be the boolean True — a placeholder "
        "string is not treated as build approval by pnpm."
    )
