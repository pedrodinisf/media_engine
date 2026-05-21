"""Dockerfile smoke test — gated by ``MEDIA_ENGINE_TEST_DOCKER=1``.

Plan §11 commit 33: "build Dockerfile in CI (skip if no Docker)". The
full build is expensive (resolves the dep graph + downloads ffmpeg),
so we don't run it on every ``pytest`` invocation. Set the env var
to opt in — typically inside a release-gate CI job.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


@pytest.mark.skipif(
    os.environ.get("MEDIA_ENGINE_TEST_DOCKER") != "1"
    or not _docker_available(),
    reason="set MEDIA_ENGINE_TEST_DOCKER=1 and have a working docker daemon",
)
def test_docker_image_builds() -> None:
    """``docker build`` against the bundled Dockerfile must succeed."""
    dockerfile = REPO_ROOT / "infra" / "docker" / "Dockerfile"
    assert dockerfile.exists()
    result = subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "-f",
            str(dockerfile),
            "-t",
            "media-engine:pytest",
            str(REPO_ROOT),
        ],
        capture_output=True,
        timeout=900,  # the slow path includes uv sync + base images
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
