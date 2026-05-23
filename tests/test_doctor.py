"""``med doctor`` — op + backend dependency surfacing.

Verifies the runtime checker and the CLI wrapper. The doctor is read-
only and idempotent: a clean run on the registered catalog produces
the same shape every time.
"""

from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from media_engine.cli import app
from media_engine.ops import OpRegistry
from media_engine.runtime.doctor import (
    check_binary,
    check_env,
    check_hardware,
    check_memory,
    check_op,
    check_service,
    diagnose,
)


def test_check_env_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCTOR_TEST_VAR", "x")
    r = check_env("DOCTOR_TEST_VAR")
    assert r.status == "ok"


def test_check_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCTOR_TEST_VAR", raising=False)
    r = check_env("DOCTOR_TEST_VAR")
    assert r.status == "missing"


def test_check_binary_ok() -> None:
    # `python` is guaranteed to be on PATH if the tests are running
    r = check_binary("python") if os.name == "posix" else check_binary("python.exe")
    # Some CI environments only have python3; fall back gracefully.
    if r.status != "ok":
        r = check_binary("python3")
    assert r.status == "ok"


def test_check_binary_missing() -> None:
    r = check_binary("definitely-not-a-real-binary-xyz123")
    assert r.status == "missing"


def test_check_service_ok() -> None:
    # `pytest` itself is installed since the tests are running
    r = check_service("pytest")
    assert r.status == "ok"


def test_check_service_missing() -> None:
    r = check_service("definitely-not-a-real-package-xyz123")
    assert r.status == "missing"


def test_check_service_dash_underscore_variant() -> None:
    """Doctor tries dash-to-underscore (PyPI mlx-lm → import mlx_lm)."""
    # `pytest-cov` is a common dev dep; importable as `pytest_cov`.
    r = check_service("pytest-cov")
    # Don't assert ok — the dep may or may not be installed in this
    # environment. Just assert the candidate-list logic ran.
    assert "pytest_cov" in r.detail or r.status == "ok"


def test_check_hardware_apple_silicon() -> None:
    r = check_hardware("apple_silicon")
    # We don't know the test host, but the check must complete with a
    # known status and include the platform tag in the detail.
    assert r.status in ("ok", "missing")
    assert "/" in r.detail


def test_check_hardware_unknown_tag() -> None:
    r = check_hardware("quantum_computer")
    assert r.status == "degraded"


def test_check_memory_satisfied() -> None:
    r = check_memory(0.001)
    assert r.status == "ok"


def test_check_memory_insufficient() -> None:
    r = check_memory(99_999.0)
    assert r.status == "missing"


def test_diagnose_covers_all_ops() -> None:
    """Every registered op shows up in the report exactly once."""
    report = diagnose()
    assert len(report.ops) == len(OpRegistry.list_all())
    names = [o.op_name for o in report.ops]
    assert len(set(names)) == len(names)  # no dupes


def test_diagnose_summary_rolls_up() -> None:
    report = diagnose()
    total = (
        report.summary.get("ok", 0)
        + report.summary.get("degraded", 0)
        + report.summary.get("unavailable", 0)
    )
    assert total == len(report.ops)


def test_diagnose_filter_by_prefix() -> None:
    report = diagnose(op_filter="audio.")
    assert len(report.ops) > 0
    assert all(o.op_name.startswith("audio.") for o in report.ops)


def test_diagnose_filter_by_exact_name() -> None:
    report = diagnose(op_filter="audio.transcribe")
    # Prefix match returns transcribe + transcribe_diarized + maybe more.
    names = {o.op_name for o in report.ops}
    assert "audio.transcribe" in names


def test_check_op_marks_embedded() -> None:
    """Ops with no Backend subclasses are flagged ``embedded``."""
    op_cls = OpRegistry.get("acquire.upload")
    report = check_op(op_cls)
    assert report.embedded is True
    assert report.backends == []


def test_check_op_resolves_backends_for_multi_backend_op() -> None:
    op_cls = OpRegistry.get("intelligence.extract")
    report = check_op(op_cls)
    assert report.embedded is False
    backend_names = {b.backend_name for b in report.backends}
    # extract has at least gemini + mlx-lm + claude as registered backends.
    assert {"gemini", "mlx-lm", "claude"}.issubset(backend_names)


def test_check_op_marks_router() -> None:
    """Ops that override ``select_backend`` are flagged ``has_router``."""
    op_cls = OpRegistry.get("intelligence.extract")
    report = check_op(op_cls)
    assert report.has_router is True


def test_op_unavailable_when_every_backend_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When all backends are missing deps, the op rolls up to unavailable."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # mlx-lm isn't installed in this environment per the doctor run
    # above; if it ever is, this test still works because the env-gated
    # gemini + claude backends will already be unavailable.
    op_cls = OpRegistry.get("intelligence.extract")
    report = check_op(op_cls)
    # If the test environment somehow has mlx-lm + apple silicon + the
    # API keys missing, mlx-lm would be ok and overall would be ok.
    # Skip this assertion if that's the case (CI has neither).
    statuses = {b.overall for b in report.backends}
    if statuses == {"unavailable"}:
        assert report.overall == "unavailable"


def test_cli_doctor_prints_summary() -> None:
    runner = CliRunner()
    # Exit code may be 0 or 1 depending on whether the test host has
    # every backend's deps. We only assert the output shape.
    result = runner.invoke(app, ["doctor"])
    assert "Doctor summary" in result.stdout


def test_cli_doctor_json_round_trip() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--json"])
    # Exit code is 0 or 1; we only care that stdout is parseable JSON.
    payload = json.loads(result.stdout)
    assert "summary" in payload
    assert "ops" in payload
    assert isinstance(payload["ops"], list)
    assert len(payload["ops"]) >= 30  # at least 34 ops; tolerate growth/skips


def test_cli_doctor_op_filter_deep_view() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--op", "search.fulltext"])
    assert "search.fulltext" in result.stdout
    assert "sqlite-fts5" in result.stdout


def test_cli_doctor_op_filter_no_match_exits_2() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--op", "this.op.does.not.exist"])
    assert result.exit_code == 2
