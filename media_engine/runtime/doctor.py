"""Op + backend dependency doctor.

Walks ``OpRegistry`` and ``BackendRegistry`` and evaluates each
backend's ``BackendRequirements`` against the current environment
(env vars, binaries on PATH, importable Python packages, hardware,
memory). Produces a structured report consumed by ``med doctor`` and
the (optional) REST surface.

Why this exists: the op→backend→deps contract is declared on the
``Backend`` subclass but never actively surfaced. An operator who
runs ``med run audio.transcribe`` on a fresh checkout has no way to
discover ahead of time that ``mlx-whisper`` isn't installed; the
failure happens deep inside ``Engine.run`` with a stack trace, not at
the boundary. This module makes the implicit contract explicit, so
"what works on this machine right now?" becomes a single command.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
from dataclasses import asdict, dataclass, field
from typing import Literal

from media_engine.backends._base import Backend, BackendRegistry
from media_engine.ops import Operation, OpRegistry
from media_engine.runtime.hardware import total_memory_gb

CheckKind = Literal["env", "binary", "service", "hardware", "memory"]
Status = Literal["ok", "missing", "degraded"]
Overall = Literal["ok", "degraded", "unavailable"]


@dataclass
class RequirementCheck:
    kind: CheckKind
    name: str
    status: Status
    detail: str = ""


@dataclass
class BackendDoctorReport:
    op_name: str
    backend_name: str
    backend_version: str
    requirements: list[RequirementCheck] = field(default_factory=lambda: [])  # noqa: PIE807
    overall: Overall = "ok"


@dataclass
class OpDoctorReport:
    op_name: str
    op_version: str
    input_kinds: list[str] = field(default_factory=lambda: [])  # noqa: PIE807
    output_kinds: list[str] = field(default_factory=lambda: [])  # noqa: PIE807
    default_backend: str | None = None
    has_router: bool = False  # op overrides select_backend
    embedded: bool = False  # op has no registered Backend subclasses
    backends: list[BackendDoctorReport] = field(default_factory=lambda: [])  # noqa: PIE807
    overall: Overall = "ok"


@dataclass
class DoctorReport:
    ops: list[OpDoctorReport] = field(default_factory=lambda: [])  # noqa: PIE807
    summary: dict[str, int] = field(default_factory=lambda: {})  # noqa: PIE807

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "ops": [_op_to_dict(o) for o in self.ops],
        }


def _op_to_dict(op: OpDoctorReport) -> dict[str, object]:
    payload = asdict(op)
    return payload


# ─────────────────────────────────────────────────────────────────
# Atomic requirement checks
# ─────────────────────────────────────────────────────────────────


def check_env(name: str) -> RequirementCheck:
    val = os.environ.get(name)
    if val:
        return RequirementCheck(
            kind="env", name=name, status="ok", detail="set"
        )
    return RequirementCheck(
        kind="env", name=name, status="missing", detail="not set"
    )


def check_binary(name: str) -> RequirementCheck:
    path = shutil.which(name)
    if path:
        return RequirementCheck(
            kind="binary", name=name, status="ok", detail=path
        )
    return RequirementCheck(
        kind="binary", name=name, status="missing", detail="not on PATH"
    )


def check_service(name: str) -> RequirementCheck:
    """Service = importable Python package.

    Package names declared in ``BackendRequirements.services`` follow PyPI
    conventions (``mlx-lm``, ``rapidocr-onnxruntime``, ``open_clip_torch``).
    The importable module name often differs: dashes become underscores,
    PyPI ``pyannote.audio`` imports as ``pyannote.audio`` but ``mlx-lm``
    imports as ``mlx_lm``. We try the obvious variants before giving up.
    """
    candidates: list[str] = []
    for variant in (name, name.replace("-", "_"), name.replace(".", "_")):
        if variant not in candidates:
            candidates.append(variant)
    for cand in candidates:
        try:
            if importlib.util.find_spec(cand) is not None:
                detail = (
                    f"importable as {cand!r}"
                    if cand != name
                    else "installed"
                )
                return RequirementCheck(
                    kind="service", name=name, status="ok", detail=detail
                )
        except (ImportError, ValueError):
            # Some packages raise ImportError on find_spec when a
            # transitive dep is missing — treat as missing.
            continue
    return RequirementCheck(
        kind="service",
        name=name,
        status="missing",
        detail=f"Python package not importable (tried: {', '.join(candidates)})",
    )


def check_hardware(name: str) -> RequirementCheck:
    if name == "apple_silicon":
        is_mac_arm = (
            platform.system() == "Darwin" and platform.machine() == "arm64"
        )
        return RequirementCheck(
            kind="hardware",
            name=name,
            status="ok" if is_mac_arm else "missing",
            detail=f"{platform.system()}/{platform.machine()}",
        )
    # Unknown hardware tag — we can't validate, so don't block.
    return RequirementCheck(
        kind="hardware",
        name=name,
        status="degraded",
        detail="unknown hardware tag (no checker registered)",
    )


def check_memory(required_gb: float) -> RequirementCheck:
    total = total_memory_gb()
    label = f"≥{required_gb:.1f} GB RAM"
    if total >= required_gb:
        return RequirementCheck(
            kind="memory",
            name=label,
            status="ok",
            detail=f"{total:.1f} GB total",
        )
    return RequirementCheck(
        kind="memory",
        name=label,
        status="missing",
        detail=f"only {total:.1f} GB total",
    )


# ─────────────────────────────────────────────────────────────────
# Roll-ups
# ─────────────────────────────────────────────────────────────────


def _roll_backend(checks: list[RequirementCheck]) -> Overall:
    if any(c.status == "missing" for c in checks):
        return "unavailable"
    if any(c.status == "degraded" for c in checks):
        return "degraded"
    return "ok"


def _roll_op(backend_reports: list[BackendDoctorReport]) -> Overall:
    if not backend_reports:
        # Embedded op (no Backend subclass) — assume ok; we can't
        # introspect its body.
        return "ok"
    statuses = [b.overall for b in backend_reports]
    if any(s == "ok" for s in statuses):
        return "ok"
    if any(s == "degraded" for s in statuses):
        return "degraded"
    return "unavailable"


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────


def check_backend(backend_cls: type[Backend]) -> BackendDoctorReport:
    reqs: list[RequirementCheck] = []
    req_spec = backend_cls.requires
    reqs.extend(check_env(v) for v in req_spec.env)
    reqs.extend(check_binary(v) for v in req_spec.binaries)
    reqs.extend(check_service(v) for v in req_spec.services)
    reqs.extend(check_hardware(v) for v in req_spec.hardware)
    if req_spec.min_memory_gb > 0:
        reqs.append(check_memory(req_spec.min_memory_gb))
    return BackendDoctorReport(
        op_name=backend_cls.op_name,
        backend_name=backend_cls.name,
        backend_version=backend_cls.version,
        requirements=reqs,
        overall=_roll_backend(reqs),
    )


def _has_custom_router(op_cls: type[Operation]) -> bool:
    """True if the op overrides ``select_backend`` (i.e. routes by params)."""
    return op_cls.select_backend is not Operation.select_backend


def check_op(op_cls: type[Operation]) -> OpDoctorReport:
    backend_names = BackendRegistry.for_op(op_cls.name)
    backend_reports: list[BackendDoctorReport] = []
    for name in backend_names:
        backend_reports.append(check_backend(BackendRegistry.get(op_cls.name, name)))
    return OpDoctorReport(
        op_name=op_cls.name,
        op_version=op_cls.version,
        input_kinds=[k.value for k in op_cls.input_kinds],
        output_kinds=[k.value for k in op_cls.output_kinds],
        default_backend=op_cls.default_backend,
        has_router=_has_custom_router(op_cls),
        embedded=not backend_names,
        backends=backend_reports,
        overall=_roll_op(backend_reports),
    )


def diagnose(op_filter: str | None = None) -> DoctorReport:
    """Run the full doctor across every registered op.

    ``op_filter`` — when set, only ops whose name matches the prefix
    (or exact name) are checked.
    """
    ops = OpRegistry.list_all()
    if op_filter is not None:
        ops = [op for op in ops if op.name == op_filter or op.name.startswith(op_filter)]
    reports = [check_op(op) for op in ops]
    summary = {"ok": 0, "degraded": 0, "unavailable": 0}
    for r in reports:
        summary[r.overall] += 1
    return DoctorReport(ops=reports, summary=summary)


__all__ = [
    "BackendDoctorReport",
    "CheckKind",
    "DoctorReport",
    "OpDoctorReport",
    "Overall",
    "RequirementCheck",
    "Status",
    "check_backend",
    "check_binary",
    "check_env",
    "check_hardware",
    "check_memory",
    "check_op",
    "check_service",
    "diagnose",
]
