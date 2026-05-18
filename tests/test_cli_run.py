"""CLI: ``med run`` cost-preview UX + ``med cost`` reporting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from media_engine.cli import app


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_run_help(runner: CliRunner) -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "cost preview" in result.stdout.lower()


def test_run_unknown_op(runner: CliRunner, cli_env: Path) -> None:
    # Error text goes to stderr (rich stderr Console); assert the exit code.
    result = runner.invoke(app, ["run", "no.such.op"])
    assert result.exit_code == 1


def test_run_dry_run_prints_estimate_and_exits(
    runner: CliRunner, cli_env: Path
) -> None:
    result = runner.invoke(
        app,
        ["--json", "--dry-run", "run", "intelligence.summarize"],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["op"] == "intelligence.summarize"
    assert "cost_estimate" in payload


def test_run_prompts_without_yes(
    runner: CliRunner, cli_env: Path
) -> None:
    # Decline the confirmation → no work, clean exit.
    result = runner.invoke(
        app,
        ["run", "intelligence.summarize"],
        input="n\n",
    )
    assert result.exit_code == 0
    assert "Cost preview" in result.stdout


def test_cost_summary_empty(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["--json", "cost", "summary"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["runs"] == 0
    assert payload["by_op"] == []


def test_cost_ls_empty(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["--json", "cost", "ls"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == []


def test_cost_bad_since(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["cost", "summary", "--since", "nope"])
    assert result.exit_code == 2
