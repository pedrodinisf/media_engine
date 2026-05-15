"""Tests for runtime/model_pool.py."""

from __future__ import annotations

import threading

import pytest

from media_engine.runtime.model_pool import ModelPool


def test_get_or_load_caches_first_call() -> None:
    pool = ModelPool()
    calls = 0

    def loader() -> str:
        nonlocal calls
        calls += 1
        return "instance"

    a = pool.get_or_load("k", loader)
    b = pool.get_or_load("k", loader)
    assert a is b
    assert calls == 1


def test_different_keys_get_different_instances() -> None:
    pool = ModelPool()
    a = pool.get_or_load("k1", lambda: object())
    b = pool.get_or_load("k2", lambda: object())
    assert a is not b
    assert pool.has("k1")
    assert pool.has("k2")


def test_forget_removes() -> None:
    pool = ModelPool()
    pool.get_or_load("k", lambda: "x")
    assert pool.forget("k") is True
    assert pool.has("k") is False
    assert pool.forget("k") is False


def test_clear_drops_everything() -> None:
    pool = ModelPool()
    for k in ("a", "b", "c"):
        pool.get_or_load(k, lambda: 1)
    pool.clear()
    assert pool.keys() == []


def test_keys_is_sorted() -> None:
    pool = ModelPool()
    for k in ("c", "a", "b"):
        pool.get_or_load(k, lambda: 1)
    assert pool.keys() == ["a", "b", "c"]


def test_total_bytes_estimate_sums() -> None:
    pool = ModelPool()
    pool.get_or_load("a", lambda: 1, bytes_estimate=100)
    pool.get_or_load("b", lambda: 2, bytes_estimate=300)
    assert pool.total_bytes_estimate() == 400


def test_concurrent_loads_dedupe() -> None:
    """Two threads racing on the same key see the same instance."""
    pool = ModelPool()
    barrier = threading.Barrier(2)
    results: list[object] = []

    def worker() -> None:
        barrier.wait()
        results.append(pool.get_or_load("shared", lambda: object()))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Both should have ended up with the SAME instance (the second loader's
    # output is dropped on the floor by design — see ModelPool docstring).
    assert results[0] is results[1]


def test_loader_exception_propagates() -> None:
    pool = ModelPool()
    with pytest.raises(RuntimeError, match="kaboom"):

        def loader() -> str:
            raise RuntimeError("kaboom")

        pool.get_or_load("k", loader)
    # Failed load did not populate.
    assert pool.has("k") is False
