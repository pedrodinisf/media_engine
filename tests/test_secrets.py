"""Tests for media_engine.runtime.secrets."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from media_engine.runtime.secrets import (
    KNOWN_SECRETS,
    load_secrets,
    parse_secrets,
    read_secrets,
    secrets_path,
    write_secrets,
)


def test_parse_secrets_handles_comments_and_blanks() -> None:
    body = """
# header comment
GEMINI_API_KEY=abc123

# another section
ANTHROPIC_API_KEY="quoted-value"
HF_TOKEN='single-quoted'
# malformed below — should be silently skipped
NOT VALID=ignored
=missingkey
"""
    parsed = parse_secrets(body)
    assert parsed == {
        "GEMINI_API_KEY": "abc123",
        "ANTHROPIC_API_KEY": "quoted-value",
        "HF_TOKEN": "single-quoted",
    }


def test_parse_secrets_rejects_invalid_keys() -> None:
    body = "lowercase=skipped\n9DIGITSTART=skipped\nVALID_KEY=kept"
    parsed = parse_secrets(body)
    assert parsed == {"VALID_KEY": "kept"}


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    write_secrets(
        tmp_path,
        {"GEMINI_API_KEY": "key-one", "HF_TOKEN": "key-two"},
    )
    assert read_secrets(tmp_path) == {
        "GEMINI_API_KEY": "key-one",
        "HF_TOKEN": "key-two",
    }


def test_write_secrets_chmod_0600(tmp_path: Path) -> None:
    write_secrets(tmp_path, {"GEMINI_API_KEY": "x"})
    mode = stat.S_IMODE(secrets_path(tmp_path).stat().st_mode)
    # 0o600 — user rw, no group/other.
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_write_secrets_delete_via_none_and_empty(tmp_path: Path) -> None:
    write_secrets(tmp_path, {"A": "1", "B": "2", "C": "3"})
    write_secrets(tmp_path, {"A": None, "B": ""})
    assert read_secrets(tmp_path) == {"C": "3"}


def test_write_secrets_quotes_values_with_whitespace(tmp_path: Path) -> None:
    write_secrets(tmp_path, {"FOO": "has spaces"})
    body = secrets_path(tmp_path).read_text()
    assert 'FOO="has spaces"' in body


def test_write_secrets_rejects_bad_key_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid env var name"):
        write_secrets(tmp_path, {"lowercase-key": "x"})


def test_load_secrets_respects_existing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_secrets(tmp_path, {"GEMINI_API_KEY": "from-file"})
    monkeypatch.setenv("GEMINI_API_KEY", "from-shell")
    touched = load_secrets(tmp_path, override=False)
    assert touched == []
    assert os.environ["GEMINI_API_KEY"] == "from-shell"


def test_load_secrets_override_true_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_secrets(tmp_path, {"GEMINI_API_KEY": "from-file"})
    monkeypatch.setenv("GEMINI_API_KEY", "from-shell")
    touched = load_secrets(tmp_path, override=True)
    assert touched == ["GEMINI_API_KEY"]
    assert os.environ["GEMINI_API_KEY"] == "from-file"


def test_load_secrets_missing_file_is_noop(tmp_path: Path) -> None:
    assert load_secrets(tmp_path) == []


def test_known_secrets_catalog_shape() -> None:
    """Catalog drives the Web UI; guard against accidental key removal."""
    names = {entry["name"] for entry in KNOWN_SECRETS}
    assert "GEMINI_API_KEY" in names
    assert "ANTHROPIC_API_KEY" in names
    assert "HF_TOKEN" in names
    for entry in KNOWN_SECRETS:
        assert set(entry.keys()) >= {"name", "label", "category", "used_by"}
