"""Smoke tests for the ``med speakers`` CLI group.

We introspect the compiled click command tree rather than assert on rendered
``--help`` text: Rich wraps/truncates option names at narrow terminal widths
(CI has no tty), which made help-string assertions flaky. The command params
are the actual contract and are width-independent.
"""

from __future__ import annotations

import typer

from media_engine.cli import app
from media_engine.cli.speakers import app as speakers_app


def _speakers_group():
    return typer.main.get_command(speakers_app)


def _flags(command_name: str) -> list[str]:
    cmd = _speakers_group().commands[command_name]
    return [opt for param in cmd.params for opt in param.opts]


def test_speakers_group_mounted_with_all_commands() -> None:
    # Mounted on the root app...
    root = typer.main.get_command(app)
    assert "speakers" in root.commands
    # ...with the four subcommands.
    assert set(_speakers_group().commands) == {
        "embed-voice", "cluster", "match", "purge"
    }


def test_embed_voice_has_diarization_option() -> None:
    flags = _flags("embed-voice")
    assert "--diarization" in flags
    assert "-d" in flags


def test_cluster_has_expected_options() -> None:
    flags = _flags("cluster")
    assert "--min-cluster-size" in flags
    assert "--reconcile-threshold" in flags


def test_match_has_top_k_option() -> None:
    assert "--top-k" in _flags("match")


def test_purge_has_yes_and_namespace() -> None:
    flags = _flags("purge")
    assert "--yes" in flags
    assert "--namespace" in flags
