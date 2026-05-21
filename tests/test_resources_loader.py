"""``resources.yaml`` loader — capacity overrides + op remap."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_engine.ops import OpRegistry
from media_engine.runtime.dag import DEFAULT_RESOURCE_CAPACITIES, make_semaphores
from media_engine.runtime.resources import (
    ResourcesConfigError,
    apply_resources_config,
    load_resources_config,
)


@pytest.fixture(autouse=True)
def _restore_resources() -> None:
    """Empty config restores compile-time declared_resources after each test."""
    yield
    apply_resources_config(load_resources_config(None))


# ─────────────────────────────────────────────────────────────────
# Loader behavior
# ─────────────────────────────────────────────────────────────────


def test_missing_file_returns_empty_config(tmp_path: Path) -> None:
    cfg = load_resources_config(tmp_path / "does-not-exist.yaml")
    assert cfg.capacities() == {}
    assert cfg.remap() == {}


def test_load_simple_capacity(tmp_path: Path) -> None:
    p = tmp_path / "resources.yaml"
    p.write_text(
        """
apple_neural_engine:
  capacity: 2
cloud_concurrent: 16
""",
        encoding="utf-8",
    )
    cfg = load_resources_config(p)
    assert cfg.capacities() == {
        "apple_neural_engine": 2,
        "cloud_concurrent": 16,
    }
    assert cfg.remap() == {}


def test_load_with_operations(tmp_path: Path) -> None:
    p = tmp_path / "resources.yaml"
    p.write_text(
        """
apple_neural_engine:
  capacity: 1
  operations: [audio.transcribe, frames.analyze]
""",
        encoding="utf-8",
    )
    cfg = load_resources_config(p)
    assert cfg.capacities() == {"apple_neural_engine": 1}
    assert cfg.remap() == {
        "audio.transcribe": ["apple_neural_engine"],
        "frames.analyze": ["apple_neural_engine"],
    }


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    p = tmp_path / "resources.yaml"
    p.write_text("::: bad :::", encoding="utf-8")
    with pytest.raises(ResourcesConfigError):
        load_resources_config(p)


def test_bad_capacity_raises(tmp_path: Path) -> None:
    p = tmp_path / "resources.yaml"
    p.write_text("apple_gpu:\n  capacity: -1\n", encoding="utf-8")
    with pytest.raises(ResourcesConfigError, match="positive int"):
        load_resources_config(p)


def test_unknown_op_raises_on_apply(tmp_path: Path) -> None:
    p = tmp_path / "resources.yaml"
    p.write_text(
        "apple_neural_engine:\n  capacity: 1\n  operations: [does.not_exist]\n",
        encoding="utf-8",
    )
    cfg = load_resources_config(p)
    with pytest.raises(ResourcesConfigError, match="unknown op"):
        apply_resources_config(cfg)


# ─────────────────────────────────────────────────────────────────
# Apply effects: declared_resources + semaphores
# ─────────────────────────────────────────────────────────────────


def test_apply_remaps_op_declared_resources(tmp_path: Path) -> None:
    p = tmp_path / "resources.yaml"
    p.write_text(
        """
apple_gpu:
  capacity: 1
  operations: [audio.transcribe]
""",
        encoding="utf-8",
    )
    apply_resources_config(load_resources_config(p))
    op = OpRegistry.get("audio.transcribe")
    assert op.declared_resources == ("apple_gpu",)


def test_apply_restores_default_when_op_dropped(tmp_path: Path) -> None:
    """Apply A, then apply B that drops the op — original tuple returns."""
    op = OpRegistry.get("audio.transcribe")
    original = op.declared_resources

    p = tmp_path / "resources.yaml"
    p.write_text(
        "apple_gpu:\n  capacity: 1\n  operations: [audio.transcribe]\n",
        encoding="utf-8",
    )
    apply_resources_config(load_resources_config(p))
    assert op.declared_resources == ("apple_gpu",)

    # New config with no operations clause → declared_resources reverts.
    apply_resources_config(load_resources_config(None))
    assert op.declared_resources == original


def test_make_semaphores_honors_overrides() -> None:
    sems = make_semaphores({"cloud_concurrent": 32, "new_resource": 4})
    # Override sticks.
    assert sems["cloud_concurrent"]._value == 32  # noqa: SLF001
    # New resource is created.
    assert "new_resource" in sems
    # Untouched defaults stay.
    assert (
        sems["apple_neural_engine"]._value  # noqa: SLF001
        == DEFAULT_RESOURCE_CAPACITIES["apple_neural_engine"]
    )
