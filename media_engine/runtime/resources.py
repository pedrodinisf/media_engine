"""``resources.yaml`` loader — declarative resource overrides.

The DAG executor enforces ``Operation.declared_resources`` via shared
``asyncio.Semaphore``s. Defaults live in
``runtime.dag.DEFAULT_RESOURCE_CAPACITIES`` (apple_neural_engine=1,
apple_gpu=1, cloud_concurrent=8). Operators tune those defaults — or
remap which ops claim which resource — without recompiling, by
dropping a ``{config_dir}/resources.yaml`` file.

Format:

.. code-block:: yaml

    apple_neural_engine:
      capacity: 1
      operations: [audio.transcribe, frames.analyze, video.multimodal]
    apple_gpu:
      capacity: 1
      operations: [audio.diarize, intelligence.analyze]
    cloud_concurrent:
      capacity: 8

The ``operations`` list, when present, **replaces** the
``declared_resources`` tuple of those ops in the live registry (a
remap, not a merge). Resources only listed by capacity (no
``operations`` key) leave the existing claims intact and just tweak
the semaphore size.

This file is read once at ``Engine.open_session()`` (and any time a
fresh engine is built). Daemons pick up changes on restart — that's
the same lifecycle as every other config knob.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from media_engine.ops import OpRegistry


class ResourcesConfigError(RuntimeError):
    """Raised when ``resources.yaml`` is malformed or references unknown ops."""


@dataclass
class ResourceSpec:
    name: str
    capacity: int
    operations: list[str] = field(default_factory=lambda: [])  # noqa: PIE807


@dataclass
class ResourcesConfig:
    resources: list[ResourceSpec] = field(default_factory=lambda: [])  # noqa: PIE807

    def capacities(self) -> dict[str, int]:
        return {r.name: r.capacity for r in self.resources}

    def remap(self) -> dict[str, list[str]]:
        """Return ``{op_name: [resource, …]}`` for every op the file remaps.

        Resources without an ``operations`` list are absent — they only
        change capacity, not which ops claim them.
        """
        out: dict[str, list[str]] = {}
        for spec in self.resources:
            for op_name in spec.operations:
                out.setdefault(op_name, []).append(spec.name)
        return out


def load_resources_config(path: Path | None) -> ResourcesConfig:
    """Parse the resources YAML file. Missing file → empty config (defaults stand)."""
    if path is None or not path.exists():
        return ResourcesConfig()
    return load_resources_config_from_string(
        path.read_text(encoding="utf-8"), source_label=str(path)
    )


def load_resources_config_from_string(
    text: str, *, source_label: str = "resources.yaml"
) -> ResourcesConfig:
    """Parse + structurally validate ``resources.yaml`` text (no disk I/O).

    Mirrors :func:`load_resources_config` but takes a string, so the Web UI's
    config editor can validate before writing (same pattern as
    ``load_profile_from_string``). Does NOT check that referenced op names
    exist — use :func:`validate_resources_yaml` for that. Empty text → empty
    config."""
    try:
        loaded: Any = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ResourcesConfigError(f"{source_label}: invalid YAML — {e}") from e
    raw: dict[str, Any] = (
        cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
    )
    if loaded is not None and not isinstance(loaded, dict):
        raise ResourcesConfigError(
            f"{source_label}: top-level YAML must be a mapping (got "
            f"{type(loaded).__name__})"
        )
    specs: list[ResourceSpec] = []
    _ALLOWED_KEYS = {"capacity", "operations"}
    for name, body in raw.items():
        if isinstance(body, int):
            specs.append(ResourceSpec(name=name, capacity=int(body)))
            continue
        if not isinstance(body, dict):
            raise ResourcesConfigError(
                f"{source_label}: resource {name!r} body must be int or mapping "
                f"(got {type(body).__name__})"
            )
        body_d: dict[str, Any] = cast(dict[str, Any], body)
        # Reject typos in the resource body. Without this, ``capcity: 1``
        # silently creates a resource with default capacity=1 and the
        # operator never finds out why their override didn't apply.
        unknown_keys = sorted(set(body_d) - _ALLOWED_KEYS)
        if unknown_keys:
            raise ResourcesConfigError(
                f"{source_label}: resource {name!r} has unknown key(s) "
                f"{unknown_keys!r}; allowed: "
                f"{sorted(_ALLOWED_KEYS)!r}"
            )
        capacity_raw: Any = body_d.get("capacity", 1)
        if not isinstance(capacity_raw, int) or capacity_raw < 1:
            raise ResourcesConfigError(
                f"{source_label}: resource {name!r} capacity must be a positive int"
            )
        ops_raw: Any = body_d.get("operations", [])
        if ops_raw and not isinstance(ops_raw, list):
            raise ResourcesConfigError(
                f"{source_label}: resource {name!r} operations must be a list"
            )
        ops_list: list[Any] = (
            cast(list[Any], ops_raw) if isinstance(ops_raw, list) else []
        )
        operations = [str(o) for o in ops_list]
        specs.append(
            ResourceSpec(
                name=name, capacity=int(capacity_raw), operations=operations
            )
        )
    return ResourcesConfig(resources=specs)


# Snapshot of each op class's compile-time ``declared_resources`` tuple so
# repeated ``apply_resources_config`` calls behave predictably (a config
# that drops an op back to defaults must really drop it back).
_ORIGINAL_DECLARED_RESOURCES: dict[str, tuple[str, ...]] = {}


def apply_resources_config(config: ResourcesConfig) -> None:
    """Mutate the op registry so each remapped op declares its new resource set.

    Ops not mentioned keep their compile-time ``declared_resources``.
    Ops mentioned have their tuple **replaced** with the resources
    that list them — a remap, not a union. The original tuple is
    snapshotted the first time an op is touched, so a later config
    file that no longer mentions an op restores its default.

    The function is **atomic**: we validate every referenced op name
    before mutating any classvar. A YAML file that references one
    unknown op fails the whole apply, leaving the registry untouched.
    """
    remap = config.remap()
    # Validate first so an unknown op name doesn't leave the registry
    # half-restored / half-applied.
    unknown = [op for op in remap if not OpRegistry.has(op)]
    if unknown:
        raise ResourcesConfigError(
            f"resources.yaml references unknown op(s) {unknown!r}"
        )
    # Restore previously-overridden ops to their compile-time defaults
    # when the new config doesn't mention them.
    for op_name, original in _ORIGINAL_DECLARED_RESOURCES.items():
        if op_name not in remap and OpRegistry.has(op_name):
            op_class = OpRegistry.get(op_name)
            op_class.declared_resources = original  # type: ignore[misc]
    for op_name, resources in remap.items():
        op_class = OpRegistry.get(op_name)
        if op_name not in _ORIGINAL_DECLARED_RESOURCES:
            _ORIGINAL_DECLARED_RESOURCES[op_name] = op_class.declared_resources
        op_class.declared_resources = tuple(resources)  # type: ignore[misc]


def default_resources_path(config_dir: Path) -> Path:
    return config_dir / "resources.yaml"


def validate_resources_yaml(text: str) -> ResourcesConfig:
    """Structurally validate ``resources.yaml`` text AND verify referenced op
    names exist — WITHOUT mutating the registry.

    The op-name check mirrors :func:`apply_resources_config` (which is
    mutating and runs at session open); this non-mutating variant lets the
    Web UI config editor reject an ``operations: [typo.op]`` before it's
    written to disk. Raises :class:`ResourcesConfigError`."""
    config = load_resources_config_from_string(text)
    unknown = [op for op in config.remap() if not OpRegistry.has(op)]
    if unknown:
        raise ResourcesConfigError(
            f"resources.yaml references unknown op(s) {unknown!r}"
        )
    return config


def write_resources_config(config_dir: Path, text: str) -> Path:
    """Validate then atomically write ``{config_dir}/resources.yaml``.

    Validates first (raises :class:`ResourcesConfigError`), then writes via a
    same-dir temp file + ``os.replace``. Returns the written path."""
    validate_resources_yaml(text)
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "resources.yaml"
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return target
