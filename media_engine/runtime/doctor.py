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
    # Status of the *production hot path* — the backend Engine.run would
    # actually pick if the caller doesn't pass --backend. For router ops
    # this is the static default; the router may pick something else at
    # runtime based on params (e.g. model prefix). ``None`` for embedded
    # ops with no Backend layer.
    default_backend_status: Overall | None = None
    # For embedded composites: per-delegate overall status. Empty for
    # non-composites and for composites whose delegates_to is empty.
    # Walking this lets the Settings UI render an "intelligence.summarize
    # is red because intelligence.extract is red" breakdown without
    # re-doctoring the delegate ops client-side.
    delegate_overalls: dict[str, Overall] = field(default_factory=lambda: {})  # noqa: PIE807
    # Free-text notes attached during the walk (e.g. "delegate X not
    # registered"). Operator-readable; not parsed.
    notes: list[str] = field(default_factory=lambda: [])  # noqa: PIE807


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


def _roll_op(
    backend_reports: list[BackendDoctorReport],
    default_backend_status: Overall | None,
    has_router: bool,
) -> Overall:
    """Roll the overall op status.

    Rules:
      - Embedded (no backends): assume ok.
      - Router op: ok if *any* backend works (the caller can pick the
        working one via params).
      - Non-router op with a default backend: the op's effective status
        IS the default backend's status. A working alternative doesn't
        help if no caller passes ``--backend`` to reach it.
      - No default backend and not embedded: ok if any backend works
        (the caller must always specify, so doctor parity with router).
    """
    if not backend_reports:
        return "ok"
    if not has_router and default_backend_status is not None:
        return default_backend_status
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


_OVERALL_PRIORITY: dict[Overall, int] = {"ok": 0, "degraded": 1, "unavailable": 2}


def check_op(
    op_cls: type[Operation],
    _visited: frozenset[str] = frozenset(),
) -> OpDoctorReport:
    """Build the per-op doctor report.

    Embedded composites (no Backend layer but ``delegates_to`` populated)
    take the *worst* overall of their delegates as their own overall
    status — closes B-009. ``_visited`` defends against a hypothetical
    cycle in the delegation graph; none of the current composites cycle
    but the guard is cheap.
    """
    backend_names = BackendRegistry.for_op(op_cls.name)
    backend_reports: list[BackendDoctorReport] = []
    for name in backend_names:
        backend_reports.append(check_backend(BackendRegistry.get(op_cls.name, name)))
    has_router = _has_custom_router(op_cls)
    default_backend_status: Overall | None = None
    if op_cls.default_backend is not None:
        for b in backend_reports:
            if b.backend_name == op_cls.default_backend:
                default_backend_status = b.overall
                break

    embedded = not backend_names
    delegate_overalls: dict[str, Overall] = {}
    notes: list[str] = []

    overall = _roll_op(backend_reports, default_backend_status, has_router)

    if embedded and op_cls.delegates_to:
        # Composite. Recursively check each delegate; our overall is the
        # worst of theirs. Skip self-references and already-visited nodes
        # to break any cycle defensively.
        next_visited = _visited | {op_cls.name}
        cycles_skipped = 0
        for delegate_name in op_cls.delegates_to:
            if delegate_name in next_visited:
                notes.append(
                    f"delegate {delegate_name!r} skipped (cycle guard)"
                )
                cycles_skipped += 1
                continue
            try:
                delegate_cls = OpRegistry.get(delegate_name)
            except (KeyError, LookupError):
                delegate_overalls[delegate_name] = "unavailable"
                notes.append(
                    f"delegate {delegate_name!r} not registered"
                )
                continue
            delegate_report = check_op(delegate_cls, next_visited)
            delegate_overalls[delegate_name] = delegate_report.overall
        if delegate_overalls:
            overall = max(
                delegate_overalls.values(),
                key=lambda o: _OVERALL_PRIORITY[o],
            )
        elif cycles_skipped:
            # Every delegate was self-referential — composite is un-runnable
            # because none of its delegates is reachable. Without this branch
            # the overall would stay at the embedded default of "ok" and the
            # Settings UI would falsely flag the cycle as healthy.
            overall = "unavailable"

    return OpDoctorReport(
        op_name=op_cls.name,
        op_version=op_cls.version,
        input_kinds=[k.value for k in op_cls.input_kinds],
        output_kinds=[k.value for k in op_cls.output_kinds],
        default_backend=op_cls.default_backend,
        has_router=has_router,
        embedded=embedded,
        backends=backend_reports,
        overall=overall,
        default_backend_status=default_backend_status,
        delegate_overalls=delegate_overalls,
        notes=notes,
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
