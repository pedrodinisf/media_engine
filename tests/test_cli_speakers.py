"""Smoke tests for the ``med speakers`` CLI group."""

from __future__ import annotations

from typer.testing import CliRunner

from media_engine.cli import app

runner = CliRunner()


def test_speakers_group_help() -> None:
    r = runner.invoke(app, ["speakers", "--help"])
    assert r.exit_code == 0
    for cmd in ("embed-voice", "cluster", "match", "purge"):
        assert cmd in r.output


def test_embed_voice_help_shows_diarization_option() -> None:
    r = runner.invoke(app, ["speakers", "embed-voice", "--help"])
    assert r.exit_code == 0
    assert "--diarization" in r.output


def test_match_help_shows_top_k() -> None:
    r = runner.invoke(app, ["speakers", "match", "--help"])
    assert r.exit_code == 0
    assert "--top-k" in r.output


def test_purge_help_shows_yes_flag() -> None:
    r = runner.invoke(app, ["speakers", "purge", "--help"])
    assert r.exit_code == 0
    assert "--yes" in r.output
