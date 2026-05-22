"""Phase 6 commit 49 — runtime/plugins.py round-trip + filter behavior."""

from __future__ import annotations

from pathlib import Path

from media_engine.runtime.plugins import (
    PLUGINS_TOML_NAME,
    CatalogState,
    load_catalog,
    save_catalog,
)


def test_load_catalog_missing_file_returns_empty(tmp_path: Path) -> None:
    state = load_catalog(tmp_path)
    assert state.hidden_ops == frozenset()
    assert state.hidden_backends == frozenset()


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    before = CatalogState(
        hidden_ops=frozenset({"audio.transcribe", "video.extract_audio"}),
        hidden_backends=frozenset(
            {"audio.transcribe__mlx-whisper", "search.semantic__pgvector"}
        ),
    )
    path = save_catalog(tmp_path, before)
    assert path.name == PLUGINS_TOML_NAME
    after = load_catalog(tmp_path)
    assert after.hidden_ops == before.hidden_ops
    assert after.hidden_backends == before.hidden_backends


def test_save_catalog_creates_config_dir(tmp_path: Path) -> None:
    nested = tmp_path / "fresh" / "config"
    assert not nested.exists()
    save_catalog(nested, CatalogState(hidden_ops=frozenset({"x.y"})))
    assert (nested / PLUGINS_TOML_NAME).is_file()


def test_save_serialises_sorted_for_diff_readability(tmp_path: Path) -> None:
    save_catalog(
        tmp_path,
        CatalogState(
            hidden_ops=frozenset({"z.op", "a.op", "m.op"}),
            hidden_backends=frozenset(),
        ),
    )
    text = (tmp_path / PLUGINS_TOML_NAME).read_text(encoding="utf-8")
    a_idx = text.index('"a.op"')
    m_idx = text.index('"m.op"')
    z_idx = text.index('"z.op"')
    assert a_idx < m_idx < z_idx


def test_malformed_toml_falls_back_to_empty(tmp_path: Path) -> None:
    (tmp_path / PLUGINS_TOML_NAME).write_text(
        "this = is = not = valid = toml\n[broken",
        encoding="utf-8",
    )
    state = load_catalog(tmp_path)
    assert state.hidden_ops == frozenset()
    assert state.hidden_backends == frozenset()


def test_non_string_entries_are_filtered_out(tmp_path: Path) -> None:
    (tmp_path / PLUGINS_TOML_NAME).write_text(
        "hidden_ops = [\"keep.me\", 42, true]\n"
        "hidden_backends = []\n",
        encoding="utf-8",
    )
    state = load_catalog(tmp_path)
    assert state.hidden_ops == frozenset({"keep.me"})


def test_filter_ops_drops_hidden() -> None:
    state = CatalogState(hidden_ops=frozenset({"audio.transcribe"}))
    assert state.filter_ops(["audio.transcribe", "video.extract_audio"]) == [
        "video.extract_audio"
    ]


def test_filter_backends_uses_op_scoped_keys() -> None:
    state = CatalogState(
        hidden_backends=frozenset(
            {CatalogState.backend_key("audio.transcribe", "mlx-whisper")}
        )
    )
    assert state.filter_backends(
        "audio.transcribe", ["mlx-whisper", "gemini"]
    ) == ["gemini"]
    # The same backend name under a different op is *not* hidden.
    assert state.filter_backends(
        "audio.diarize", ["mlx-whisper"]
    ) == ["mlx-whisper"]


def test_is_op_visible_methods() -> None:
    state = CatalogState(hidden_ops=frozenset({"x.y"}))
    assert state.is_op_visible("a.b") is True
    assert state.is_op_visible("x.y") is False


def test_backend_key_form() -> None:
    # Locked-in surface: op_name + "__" + backend_name (matches MCP
    # exporter's tool-name encoding so round-trips stay clean).
    assert (
        CatalogState.backend_key("audio.transcribe", "mlx-whisper")
        == "audio.transcribe__mlx-whisper"
    )
