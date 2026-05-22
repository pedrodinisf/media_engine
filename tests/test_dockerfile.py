"""Dockerfile structural regression test (Phase 6 commit 50).

The default `docker build .` should produce a runtime image with the
SvelteKit dist tree already inside but no Node toolchain. That invariant
is reviewable visually, but a structural test catches accidental
regressions (a careless `apt-get install nodejs` in the runtime stage,
a dropped `--from=ui-build` COPY, etc.) at CI time, before they ship in
a wheel.

Paranoia-grade: parses the Dockerfile as text. No actual `docker build`
is invoked — too slow for the unit suite, and the Docker daemon isn't
available in every CI environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    path = Path(__file__).resolve().parents[1] / "infra" / "docker" / "Dockerfile"
    assert path.exists(), f"Dockerfile not found at {path}"
    return path.read_text()


def _parse_stage_name(line: str) -> str | None:
    """Return the stage name from a `FROM ... AS <name>` line, or None."""
    stripped = line.strip()
    upper = stripped.upper()
    if not (upper.startswith("FROM ") and " AS " in upper):
        return None
    # Case-insensitive split — Dockerfile reference allows AS or as.
    sep = " AS " if " AS " in stripped else " as "
    tail = stripped.split(sep, 1)[1]
    return tail.strip().split()[0]


def _stage_blocks(text: str) -> dict[str, str]:
    """Split the Dockerfile into `{stage_name: body}` chunks.

    A new stage begins at every `FROM ... AS <name>` directive. Lines
    are kept verbatim including comments — the assertions below look
    for substring matches.
    """
    stages: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []
    for raw_line in text.splitlines():
        stage_name = _parse_stage_name(raw_line)
        if stage_name is not None:
            if current_name is not None:
                stages[current_name] = "\n".join(current_lines)
            current_name = stage_name
            current_lines = [raw_line]
            continue
        if current_name is not None:
            current_lines.append(raw_line)
    if current_name is not None:
        stages[current_name] = "\n".join(current_lines)
    return stages


def test_ui_build_stage_present(dockerfile_text: str) -> None:
    stages = _stage_blocks(dockerfile_text)
    assert "ui-build" in stages, (
        "ui-build stage is missing — Phase 6 commit 50 added it to keep "
        "Node out of the runtime image"
    )
    body = stages["ui-build"]
    assert "pnpm" in body, "ui-build stage should invoke pnpm"
    assert "web build" in body, "ui-build stage should run `pnpm -C web build`"


def test_runtime_stage_has_no_node_apt_install(dockerfile_text: str) -> None:
    stages = _stage_blocks(dockerfile_text)
    assert "runtime" in stages, "runtime stage is missing"
    body = stages["runtime"].lower()
    # The runtime image must be Node-free. If anyone ever adds nodejs to
    # the apt install line we want that change to fail loudly.
    assert "nodejs" not in body, (
        "runtime stage installs nodejs — keep the Node toolchain confined "
        "to the ui-build stage so the wheel/Docker image stays slim"
    )
    assert " npm " not in f" {body} ", (
        "runtime stage references npm — keep the Node toolchain confined "
        "to the ui-build stage"
    )


def test_api_only_stage_present(dockerfile_text: str) -> None:
    stages = _stage_blocks(dockerfile_text)
    assert "api-only" in stages, (
        "api-only stage is missing — Phase 6 commit 50 added it so "
        "deployments that don't want the GUI can `docker build --target "
        "api-only` and skip the UI dist copy"
    )


def test_runtime_copies_dist_from_ui_build(dockerfile_text: str) -> None:
    stages = _stage_blocks(dockerfile_text)
    body = stages["runtime"]
    assert "--from=ui-build" in body, (
        "runtime stage doesn't copy from ui-build — the SPA dist tree "
        "won't make it into the final image"
    )
    assert "media_engine/web/dist" in body, (
        "runtime stage should COPY the dist tree to "
        "media_engine/web/dist so FastAPI's StaticFiles mount activates"
    )


def test_default_target_is_runtime(dockerfile_text: str) -> None:
    """The last FROM in the file is the default `docker build .` target.

    The default must be `runtime` so end-users get the UI by default; the
    api-only variant is opt-in via `--target api-only`.
    """
    last_from = None
    for line in dockerfile_text.splitlines():
        stage_name = _parse_stage_name(line)
        if stage_name is not None:
            last_from = stage_name
    assert last_from == "runtime", (
        f"Default Dockerfile target should be `runtime`, found `{last_from}`. "
        "Swap the order so the UI-bundled image is what `docker build .` produces."
    )
