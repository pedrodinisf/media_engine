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


# ─────────────────────────────────────────────────────────────────
# B-009 — composites walk delegates_to
# ─────────────────────────────────────────────────────────────────


def test_composite_inherits_delegate_overalls_when_ok() -> None:
    """Sanity: if the leaf is ok, the composite rolls up to ok."""
    op_cls = OpRegistry.get("intelligence.summarize")
    report = check_op(op_cls)
    assert report.embedded is True
    assert report.delegate_overalls == {"intelligence.extract": check_op(
        OpRegistry.get("intelligence.extract")
    ).overall}
    # Whatever the leaf is, the composite matches it.
    assert report.overall == report.delegate_overalls["intelligence.extract"]


def test_composite_unavailable_when_delegate_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every leaf backend is missing deps, the composite is red.

    Monkey-patches BackendRegistry.get to inject a synthetic 'all-deps-
    missing' backend report so we don't need to actually uninstall mlx-
    lm / pyannote / etc.
    """
    from media_engine.runtime import doctor as doctor_mod

    original_check_backend = doctor_mod.check_backend

    def fake_check_backend(backend_cls: type) -> doctor_mod.BackendDoctorReport:
        # Mark every backend reachable through this op tree as unavailable.
        return doctor_mod.BackendDoctorReport(
            op_name=backend_cls.op_name,
            backend_name=backend_cls.name,
            backend_version=backend_cls.version,
            requirements=[
                doctor_mod.RequirementCheck(
                    kind="service",
                    name="synthetic",
                    status="missing",
                    detail="injected by test_doctor",
                )
            ],
            overall="unavailable",
        )

    monkeypatch.setattr(doctor_mod, "check_backend", fake_check_backend)
    op_cls = OpRegistry.get("intelligence.summarize")
    report = doctor_mod.check_op(op_cls)
    assert report.embedded is True
    assert report.delegate_overalls == {"intelligence.extract": "unavailable"}
    assert report.overall == "unavailable"
    # Cleanup — pytest's monkeypatch handles teardown automatically.
    del original_check_backend


def test_composite_with_unregistered_delegate_records_note() -> None:
    """If a composite declares a delegate that isn't registered, doctor
    flags the composite as unavailable and adds a note. Defensive guard
    against a typo'd delegates_to entry."""
    from pydantic import BaseModel

    from media_engine.artifacts import Kind
    from media_engine.ops import Operation, OpRegistry, register_op
    from media_engine.runtime.doctor import check_op as _check

    class _Params(BaseModel):
        pass

    class _SyntheticComposite(Operation):
        name = "test.synthetic_composite_b009"
        version = "1.0.0"
        input_kinds = (Kind.MarkdownArtifact,)
        output_kinds = (Kind.MarkdownArtifact,)
        params_model = _Params
        delegates_to = ("test.does_not_exist_b009",)

        async def run(
            self, inputs: list, params: BaseModel, ctx: object
        ) -> list:
            return inputs

    try:
        register_op(_SyntheticComposite)
        report = _check(_SyntheticComposite)
        assert report.overall == "unavailable"
        assert report.delegate_overalls == {
            "test.does_not_exist_b009": "unavailable"
        }
        assert any("not registered" in n for n in report.notes)
    finally:
        OpRegistry._ops.pop(_SyntheticComposite.name, None)


def test_composite_cycle_guard() -> None:
    """A composite that recursively names itself doesn't loop forever."""
    from pydantic import BaseModel

    from media_engine.artifacts import Kind
    from media_engine.ops import Operation, OpRegistry, register_op
    from media_engine.runtime.doctor import check_op as _check

    class _Params(BaseModel):
        pass

    class _CyclicComposite(Operation):
        name = "test.cyclic_composite_b009"
        version = "1.0.0"
        input_kinds = (Kind.MarkdownArtifact,)
        output_kinds = (Kind.MarkdownArtifact,)
        params_model = _Params
        delegates_to = ("test.cyclic_composite_b009",)

        async def run(
            self, inputs: list, params: BaseModel, ctx: object
        ) -> list:
            return inputs

    try:
        register_op(_CyclicComposite)
        # Must terminate.
        report = _check(_CyclicComposite)
        # The single delegate is skipped by the cycle guard — there are
        # no delegate_overalls entries, and a note records the skip.
        assert any("cycle guard" in n for n in report.notes)
        # And the composite must report unavailable (not the embedded
        # default of "ok"), so the Settings UI doesn't render a green
        # checkmark for an op no one can actually run. Audit fix from
        # the Phase 6.6 fresh-eyes review.
        assert report.overall == "unavailable"
    finally:
        OpRegistry._ops.pop(_CyclicComposite.name, None)


def test_doctor_dict_round_trip_carries_delegate_fields() -> None:
    """The JSON surface (consumed by the Settings UI) must include the new
    fields so the per-op expand block can render the delegate breakdown."""
    op_cls = OpRegistry.get("audio.transcribe_diarized")
    report = check_op(op_cls)
    assert report.embedded is True
    # The op declares delegates_to=(audio.transcribe, audio.diarize); the
    # breakdown must populate both entries.
    assert set(report.delegate_overalls.keys()) == {
        "audio.transcribe",
        "audio.diarize",
    }
    payload = diagnose(op_filter="audio.transcribe_diarized").to_dict()
    [composite_dict] = payload["ops"]  # type: ignore[index, assignment]
    assert "delegate_overalls" in composite_dict
    assert "notes" in composite_dict
