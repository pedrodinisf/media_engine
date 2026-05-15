"""End-to-end smoke test for the ``med`` CLI.

Drives Typer via CliRunner against a tmp permanent_store. Covers:
``acquire`` → captures id → ``extract-audio`` → ``ls`` → ``show`` → ``lineage``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from media_engine.cli import app


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a tmp permanent_store + cache via env vars."""
    store = tmp_path / "store"
    work = tmp_path / "work"
    cache_db = tmp_path / "cache.db"
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(store))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(work))
    monkeypatch.setenv("MEDIA_ENGINE_CACHE_DB_URL", f"sqlite+pysqlite:///{cache_db}")
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    # mix_stderr=False so we can inspect stdout cleanly when needed
    return CliRunner()


def test_help(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "med" in result.stdout.lower() or "Universal" in result.stdout


def test_config_command(runner: CliRunner, cli_env: Path) -> None:
    """Rich may truncate long paths in the table render; only assert exit and
    that the table rendered. Path verification uses the JSON variant below."""
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0, result.stdout
    assert "Engine configuration" in result.stdout
    assert "permanent_store" in result.stdout


def test_config_command_json(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["--json", "config"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["permanent_store"] == str(cli_env / "store")


def test_ops_command(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["ops"])
    assert result.exit_code == 0, result.stdout
    assert "acquire.upload" in result.stdout
    assert "video.extract_audio" in result.stdout


def test_ops_command_json(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["--json", "ops"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    names = {op["name"] for op in payload}
    assert "acquire.upload" in names
    assert "video.extract_audio" in names


def test_ls_empty_store(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "no artifacts" in result.stdout.lower()


def test_full_smoke_flow(
    runner: CliRunner, cli_env: Path, sample_mp4: Path
) -> None:
    # 1. acquire → captures full id (one per line)
    result = runner.invoke(app, ["acquire", str(sample_mp4)])
    assert result.exit_code == 0, result.stdout
    video_id = result.stdout.strip()
    assert len(video_id) == 64  # sha256 hex

    # 2. ls shows the video
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0, result.stdout
    assert video_id[:12] in result.stdout

    # 3. extract-audio by full id
    result = runner.invoke(app, ["extract-audio", video_id])
    assert result.exit_code == 0, result.stdout
    audio_id = result.stdout.strip()
    assert len(audio_id) == 64

    # 4. extract-audio by prefix (git-style resolution)
    short = video_id[:8]
    result = runner.invoke(app, ["extract-audio", short])
    assert result.exit_code == 0, result.stdout
    # cache hit; same audio id
    assert result.stdout.strip() == audio_id

    # 5. ls --kind Audio shows the audio
    result = runner.invoke(app, ["ls", "--kind", "audio"])
    assert result.exit_code == 0
    assert audio_id[:12] in result.stdout

    # 6. show the audio (by prefix)
    result = runner.invoke(app, ["show", audio_id[:8]])
    assert result.exit_code == 0, result.stdout
    assert "sample_rate" in result.stdout

    # 7. lineage of the audio shows video parent
    result = runner.invoke(app, ["lineage", audio_id[:8]])
    assert result.exit_code == 0, result.stdout
    assert video_id[:12] in result.stdout
    assert "video.extract_audio" in result.stdout


def test_show_unknown_id_exits_nonzero(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["show", "deadbeef"])
    assert result.exit_code != 0


def test_lineage_unknown_id_exits_nonzero(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["lineage", "deadbeef"])
    assert result.exit_code != 0


def test_acquire_missing_file_exits_nonzero(
    runner: CliRunner, cli_env: Path, tmp_path: Path
) -> None:
    result = runner.invoke(app, ["acquire", str(tmp_path / "nope.mp4")])
    assert result.exit_code != 0


def test_dry_run_acquire_prints_cost(
    runner: CliRunner, cli_env: Path, sample_mp4: Path
) -> None:
    result = runner.invoke(app, ["--dry-run", "acquire", str(sample_mp4)])
    assert result.exit_code == 0, result.stdout
    assert "cost" in result.stdout.lower() or "local_seconds" in result.stdout
    # No new artifacts created.
    ls = runner.invoke(app, ["ls"])
    assert "no artifacts" in ls.stdout.lower()


def test_dry_run_extract_audio_prints_cost(
    runner: CliRunner, cli_env: Path, sample_mp4: Path
) -> None:
    # First, actually acquire so we have a video id.
    acq = runner.invoke(app, ["acquire", str(sample_mp4)])
    video_id = acq.stdout.strip()

    result = runner.invoke(app, ["--dry-run", "extract-audio", video_id])
    assert result.exit_code == 0, result.stdout
    assert "cost" in result.stdout.lower() or "local_seconds" in result.stdout

    # No audio created.
    ls = runner.invoke(app, ["ls", "--kind", "audio"])
    assert "no artifacts" in ls.stdout.lower()


def test_json_acquire_emits_artifact_payload(
    runner: CliRunner, cli_env: Path, sample_mp4: Path
) -> None:
    result = runner.invoke(app, ["--json", "acquire", str(sample_mp4)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload[0]["kind"] == "video"


def test_ls_invalid_kind_exits_nonzero(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["ls", "--kind", "nonexistent"])
    assert result.exit_code != 0
