"""CLI: ``med events`` history + tail (no-daemon path)."""

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


def test_events_help(runner: CliRunner) -> None:
    result = runner.invoke(app, ["events", "--help"])
    assert result.exit_code == 0
    assert "history" in result.stdout
    assert "tail" in result.stdout


def test_history_empty(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["--json", "events", "history"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == []


def test_history_after_a_run(
    runner: CliRunner, cli_env: Path
) -> None:
    from media_engine.config import EngineConfig
    from media_engine.runtime.engine import Engine

    # Drive one real op so events get persisted into the shared cache.db.
    cfg = EngineConfig.load()
    with Engine.open_quick(cfg) as e:
        e.cache.record_event(
            ts=__import__("datetime").datetime.now(
                __import__("datetime").UTC
            ),
            event_type="op_completed",
            op_run_id="abc123",
            op_name="acquire.upload",
            payload_json="{}",
        )

    result = runner.invoke(app, ["--json", "events", "history"])
    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["type"] == "op_completed"
    assert rows[0]["op_name"] == "acquire.upload"


def test_history_bad_since(runner: CliRunner, cli_env: Path) -> None:
    result = runner.invoke(app, ["events", "history", "--since", "xyz"])
    assert result.exit_code == 2


def test_tail_without_daemon_errors(
    runner: CliRunner, cli_env: Path
) -> None:
    # No daemon socket → tail can't subscribe; clean non-zero exit.
    result = runner.invoke(app, ["events", "tail", "--no-follow"])
    assert result.exit_code == 1
