"""Tests for runtime/server_manager.py.

Uses ``sleep`` (a tiny POSIX command always present) as the demo subprocess.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from media_engine.runtime.server_manager import ServerManager


@pytest.fixture
def manager(tmp_path: Path) -> ServerManager:
    return ServerManager(tmp_path / "server-state")


def test_start_writes_pid_and_runs(manager: ServerManager) -> None:
    pid = manager.start("demo", ["sleep", "30"])
    assert pid > 0
    assert manager.pid_of("demo") == pid
    assert manager.is_alive("demo")
    manager.stop("demo")


def test_start_idempotent_returns_existing_pid(manager: ServerManager) -> None:
    pid1 = manager.start("demo", ["sleep", "30"])
    pid2 = manager.start("demo", ["sleep", "30"])
    assert pid1 == pid2
    manager.stop("demo")


def test_stop_cleans_pid_file(manager: ServerManager) -> None:
    manager.start("demo", ["sleep", "30"])
    assert manager.stop("demo") is True
    assert manager.pid_of("demo") is None
    assert manager.is_alive("demo") is False


def test_stop_no_running_returns_false(manager: ServerManager) -> None:
    assert manager.stop("never_started") is False


def test_meta_persisted(manager: ServerManager) -> None:
    manager.start("demo", ["sleep", "30"], meta={"model": "test-model"})
    health = manager.health_check("demo")
    assert health.model == "test-model"
    manager.stop("demo")


def test_log_path_returns_file(manager: ServerManager) -> None:
    p = manager.log_path("demo")
    assert isinstance(p, Path)
    assert p.name.endswith(".log")


def test_log_tail_after_writes(manager: ServerManager) -> None:
    manager.start("demo", ["sh", "-c", "echo hello && sleep 30"])
    # give the subprocess a moment to write its line
    time.sleep(0.2)
    tail = manager.log_tail("demo", n=5)
    assert "hello" in tail
    manager.stop("demo")


def test_restart_replaces_process(manager: ServerManager) -> None:
    pid1 = manager.start("demo", ["sleep", "30"])
    pid2 = manager.restart("demo", ["sleep", "30"])
    assert pid1 != pid2
    assert manager.is_alive("demo")
    manager.stop("demo")


def test_pid_of_returns_none_for_missing(manager: ServerManager) -> None:
    assert manager.pid_of("nope") is None


def test_is_alive_for_dead_pid_returns_false(
    manager: ServerManager, tmp_path: Path
) -> None:
    # Write a clearly-dead PID
    (manager.state_dir / "ghost.pid").write_text("99999999")
    assert manager.is_alive("ghost") is False


def test_health_check_no_url_just_process(manager: ServerManager) -> None:
    manager.start("demo", ["sleep", "30"])
    h = manager.health_check("demo")
    assert h.running is True
    assert h.healthy is True  # no url → healthy iff alive
    assert h.error is None
    manager.stop("demo")


def test_state_dir_is_created(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "server-state"
    assert not target.exists()
    ServerManager(target)
    assert target.is_dir()
