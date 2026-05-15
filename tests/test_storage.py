"""Tests for runtime/storage.py LocalFSStorage."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from media_engine.runtime.storage import LocalFSStorage


@pytest.fixture
def storage(tmp_path: Path) -> LocalFSStorage:
    return LocalFSStorage(tmp_path / "store", tmp_path / "work")


def test_artifact_path_sharded(storage: LocalFSStorage) -> None:
    p = storage.artifact_path("abcd1234", ".mp4")
    assert p == storage.permanent_store / "artifacts" / "ab" / "abcd1234.mp4"


def test_artifact_path_normalizes_extension(storage: LocalFSStorage) -> None:
    a = storage.artifact_path("abcd", "mp4")
    b = storage.artifact_path("abcd", ".mp4")
    assert a == b


def test_artifact_path_empty_extension(storage: LocalFSStorage) -> None:
    p = storage.artifact_path("abcd", "")
    assert p.name == "abcd"


def test_artifact_path_rejects_short_sha(storage: LocalFSStorage) -> None:
    with pytest.raises(ValueError):
        storage.artifact_path("a", ".mp4")
    with pytest.raises(ValueError):
        storage.artifact_path("", ".mp4")


def test_store_file_copy(storage: LocalFSStorage, tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello")
    dest = storage.store_file(src, "ab" + "0" * 62, ".bin")
    assert dest.exists()
    assert dest.read_bytes() == b"hello"
    # source still exists; copy is a copy
    assert src.exists()
    assert os.stat(src).st_ino != os.stat(dest).st_ino


def test_store_file_hardlink(storage: LocalFSStorage, tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello hardlink")
    dest = storage.store_file(src, "cd" + "0" * 62, ".bin", link_mode="hardlink")
    assert dest.exists()
    assert os.stat(src).st_ino == os.stat(dest).st_ino


def test_store_file_idempotent(storage: LocalFSStorage, tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"twice")
    a = storage.store_file(src, "ef" + "0" * 62, ".bin")
    b = storage.store_file(src, "ef" + "0" * 62, ".bin")
    assert a == b


def test_store_file_atomic_no_tmp_leftover(storage: LocalFSStorage, tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"data")
    dest = storage.store_file(src, "12" + "0" * 62, ".bin")
    leftover = dest.with_suffix(dest.suffix + ".tmp")
    assert not leftover.exists()


def test_workdir_lifecycle(storage: LocalFSStorage) -> None:
    d = storage.ensure_workdir("job-xyz")
    assert d.exists() and d.is_dir()
    (d / "scratch.txt").write_text("hi")
    storage.cleanup_workdir("job-xyz")
    assert not d.exists()
