"""Tests for ``med batch`` (cli/batch.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from media_engine.cli import app
from media_engine.cli.batch import _build_pipeline, _read_input_file, _slug


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL", f"sqlite+pysqlite:///{tmp_path / 'cache.db'}"
    )
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_slug_is_idempotent_and_clamped() -> None:
    assert _slug("https://example.com/x?y=1") == "https___example_com_x_y_1"
    assert len(_slug("a" * 200)) == 32
    assert _slug("") == "item"


def test_read_input_file_strips_blanks_and_comments(tmp_path: Path) -> None:
    f = tmp_path / "list.txt"
    f.write_text(
        "# top comment\n"
        "/path/one\n"
        "\n"
        "/path/two\n"
        "  # indented comment kept (no trim before #)\n"
        "/path/three\n"
    )
    items = _read_input_file(f)
    assert items == ["/path/one", "/path/two", "/path/three"]


def test_read_input_file_missing_raises(tmp_path: Path) -> None:
    import typer
    with pytest.raises(typer.BadParameter):
        _read_input_file(tmp_path / "nope.txt")


def test_read_input_file_empty_raises(tmp_path: Path) -> None:
    import typer
    f = tmp_path / "empty.txt"
    f.write_text("# only comments\n#\n")
    with pytest.raises(typer.BadParameter):
        _read_input_file(f)


def test_build_pipeline_disambiguates_duplicate_slugs() -> None:
    pipeline = _build_pipeline(
        ["/x", "/x", "/y"], op="acquire.upload",
        input_arg="source_path", extra_params={},
    )
    ids = [n.id for n in pipeline.nodes]
    assert len(ids) == 3
    assert len(set(ids)) == 3  # all distinct


def test_build_pipeline_passes_extra_params() -> None:
    pipeline = _build_pipeline(
        ["/x"], op="acquire.upload",
        input_arg="source_path",
        extra_params={"link_mode": "hardlink"},
    )
    assert pipeline.nodes[0].params == {
        "source_path": "/x",
        "link_mode": "hardlink",
    }


def test_batch_acquire_upload_smoke(
    runner: CliRunner, cli_env: Path, sample_mp4: Path, sample_m4a: Path
) -> None:
    listfile = cli_env / "items.txt"
    listfile.write_text(f"{sample_mp4}\n{sample_m4a}\n")
    result = runner.invoke(app, ["batch", str(listfile), "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert len(payload["successes"]) == 2
    assert payload["failures"] == {}


def test_batch_handles_one_bad_path(
    runner: CliRunner, cli_env: Path, sample_mp4: Path, tmp_path: Path
) -> None:
    listfile = tmp_path / "items.txt"
    listfile.write_text(f"{sample_mp4}\n{tmp_path / 'nope.mp4'}\n")
    result = runner.invoke(app, ["batch", str(listfile), "--json"])
    # Exit non-zero because one node failed.
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert len(payload["successes"]) == 1
    assert len(payload["failures"]) == 1
    failure = next(iter(payload["failures"].values()))
    assert "FileNotFoundError" in failure["error_class"]


def test_batch_param_collision_rejected(
    runner: CliRunner, cli_env: Path, sample_mp4: Path
) -> None:
    listfile = cli_env / "items.txt"
    listfile.write_text(f"{sample_mp4}\n")
    result = runner.invoke(
        app,
        ["batch", str(listfile), "--param", "source_path=/other"],
    )
    assert result.exit_code != 0


def test_batch_invalid_param_format_rejected(
    runner: CliRunner, cli_env: Path, sample_mp4: Path
) -> None:
    listfile = cli_env / "items.txt"
    listfile.write_text(f"{sample_mp4}\n")
    result = runner.invoke(
        app, ["batch", str(listfile), "--param", "no-equals"],
    )
    assert result.exit_code != 0
