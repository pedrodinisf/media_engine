"""Smoke for the ``med search`` CLI (fulltext mode — always-on)."""

from __future__ import annotations

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


def test_search_help(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["search", "--help"])
    assert result.exit_code == 0
    assert "semantic" in result.output
    assert "fulltext" in result.output
    assert "hybrid" in result.output


def test_search_bad_mode_rejected(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["search", "x", "--mode", "bogus"])
    assert result.exit_code != 0
    assert "bogus" in result.output or "mode" in result.output


def test_search_bad_kind_rejected(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["search", "x", "--kind", "not-a-kind"])
    assert result.exit_code != 0


def test_search_empty_corpus_json(cli_env: Path) -> None:
    import json as _json

    result = CliRunner().invoke(
        app, ["search", "ducks", "--mode", "fulltext", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert payload["mode"] == "fulltext"
    assert payload["results"] == []


def test_search_returns_seeded_result(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: seed a transcript, then run `med search` against it."""
    import json as _json

    from media_engine.bootstrap import register_all
    from media_engine.config import EngineConfig
    from media_engine.runtime.engine import Engine

    register_all(force=True)
    cfg = EngineConfig.load()
    with Engine.open_quick(cfg) as engine:
        from tests._search_helpers import make_transcript

        target = make_transcript(
            engine,
            key="tariffs",
            text="Heavy tariffs and the diplomatic communique that followed.",
        )

    result = CliRunner().invoke(
        app, ["search", "tariffs", "--mode", "fulltext", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert payload["mode"] == "fulltext"
    assert payload["results"], "expected at least one hit"
    assert payload["results"][0]["artifact_id"] == target.id
