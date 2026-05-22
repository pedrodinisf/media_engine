"""Phase 6 commit 40 — ``med web start`` CLI.

Covers the surface in ``media_engine/cli/web.py``: it must error
clearly when the dist tree is missing (so a fresh contributor knows
exactly which `pnpm` command to run), and the auto-detect-display logic
must respect ``MEDIA_ENGINE_NO_BROWSER``.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from media_engine.cli import app as cli_app
from media_engine.cli.web import _should_open_browser

runner = CliRunner()


def test_med_web_start_errors_when_dist_missing(tmp_path: Path) -> None:
    """Without the dist tree, the CLI exits 1 with a clear remediation hint."""
    fake_dist = tmp_path / "no-dist"
    with patch("media_engine.cli.web.ui_dist_dir", return_value=fake_dist):
        result = runner.invoke(cli_app, ["web", "start", "--no-open"])
    assert result.exit_code == 1
    # The hint must point at the pnpm command — that's the contract the
    # contributor docs + the plan §4 build-flow rely on. Rich routes the
    # error to stderr; CliRunner exposes it via .output (combined).
    combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "pnpm" in combined
    assert "build" in combined


def test_med_web_start_appears_in_help() -> None:
    """`med --help` lists the web subcommand alongside api/daemon/etc."""
    result = runner.invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    assert "web" in result.stdout


@pytest.mark.parametrize(
    ("env", "platform", "expected"),
    [
        # Explicit --no-open / --open always wins; not tested here (covered
        # by the typer flag dispatch). These cases assert the auto-detect
        # branch.
        ({}, "darwin", True),
        ({}, "win32", True),
        ({}, "linux", False),
        ({"DISPLAY": ":0"}, "linux", True),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", True),
        ({"MEDIA_ENGINE_NO_BROWSER": "1", "DISPLAY": ":0"}, "linux", False),
    ],
)
def test_should_open_browser_auto_detect(
    env: dict[str, str], platform: str, expected: bool
) -> None:
    """The auto-detect path picks open=True only when a display is plausible."""
    new_env = {**os.environ, **env}
    new_env.pop("MEDIA_ENGINE_NO_BROWSER", None) if "MEDIA_ENGINE_NO_BROWSER" not in env else None
    # Clear the no-browser env so the test cases that don't set it aren't
    # poisoned by a CI default. (We re-set it inside the patched env if
    # the case calls for it.)
    base_env = {k: v for k, v in os.environ.items() if k != "MEDIA_ENGINE_NO_BROWSER"}
    base_env.pop("DISPLAY", None)
    base_env.pop("WAYLAND_DISPLAY", None)
    final_env = {**base_env, **env}
    with patch.dict(os.environ, final_env, clear=True), patch("sys.platform", platform):
        assert _should_open_browser(None) is expected


def test_should_open_browser_explicit_flag_wins() -> None:
    """When the user passes --open or --no-open, env + platform are ignored."""
    with patch.dict(os.environ, {"MEDIA_ENGINE_NO_BROWSER": "1"}, clear=False):
        assert _should_open_browser(True) is True
        assert _should_open_browser(False) is False
