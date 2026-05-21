"""Tests for Engine.run — execution + caching + lineage."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_engine.artifacts import Audio, Kind, Video

# Ensure ops are registered for these tests.
from media_engine.ops.acquire import upload as _upload_op  # noqa: F401
from media_engine.ops.video import extract_audio as _extract_op  # noqa: F401
from media_engine.runtime.engine import Engine


async def test_run_acquire_upload_returns_video(
    engine: Engine, sample_mp4: Path
) -> None:
    [art] = await engine.run("acquire.upload", source_path=sample_mp4)
    assert isinstance(art, Video)
    assert art.kind is Kind.Video
    assert art.path.exists()


async def test_run_acquire_then_extract_chain(
    engine: Engine, sample_mp4: Path
) -> None:
    [video] = await engine.run("acquire.upload", source_path=sample_mp4)
    [audio] = await engine.run("video.extract_audio", inputs=[video.id])
    assert isinstance(audio, Audio)
    assert audio.kind is Kind.Audio
    assert audio.derived_from == (video.id,)
    assert audio.produced_by is not None  # stamped with run id
    assert audio.metadata["sample_rate"] == 16000


async def test_run_cache_hit_avoids_rerun(
    engine: Engine, sample_mp4: Path, mocker
) -> None:
    [video] = await engine.run("acquire.upload", source_path=sample_mp4)
    [a1] = await engine.run("video.extract_audio", inputs=[video.id])

    spy = mocker.spy(__import__("subprocess"), "Popen")
    [a2] = await engine.run("video.extract_audio", inputs=[video.id])

    ffmpeg_calls = [c for c in spy.call_args_list if "ffmpeg" in str(c.args[0][0])]
    assert ffmpeg_calls == [], "second run should be a cache hit, not a re-invocation"
    assert a1.id == a2.id


async def test_run_param_change_yields_new_id(
    engine: Engine, sample_mp4: Path
) -> None:
    [video] = await engine.run("acquire.upload", source_path=sample_mp4)
    [a16] = await engine.run("video.extract_audio", inputs=[video.id], sample_rate=16000)
    [a44] = await engine.run("video.extract_audio", inputs=[video.id], sample_rate=44100)
    assert a16.id != a44.id


async def test_run_stamps_engine_namespace_on_outputs(
    tmp_path: Path, sample_mp4: Path
) -> None:
    """Regression: when the engine runs in a non-default namespace,
    outputs must land in that namespace.

    Ops construct artifacts with the Pydantic default
    ``namespace="default"``; the engine is the single place that owns
    the namespace decision per ``Engine.run`` call. Without the stamp
    a multi-tenant deployment writes orphan artifacts that the caller
    can't read back through the same handle.
    """
    from media_engine.config import EngineConfig

    cfg = EngineConfig(
        permanent_store=tmp_path / "store",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
        min_free_gb=0,
        namespace="tenant-foo",
    )
    with Engine.open_quick(cfg) as engine:
        [art] = await engine.run("acquire.upload", source_path=sample_mp4)
    assert art.namespace == "tenant-foo"
    # Re-open and look up under both namespaces to confirm isolation.
    with Engine.open_quick(cfg) as e2:
        assert e2.cache.get_artifact(art.id, namespace="tenant-foo") is not None
        assert e2.cache.get_artifact(art.id, namespace="default") is None


async def test_run_unknown_op_raises(engine: Engine) -> None:
    with pytest.raises(LookupError, match="No operation"):
        await engine.run("never.heard")


async def test_run_input_kind_mismatch_raises(
    engine: Engine, sample_m4a: Path
) -> None:
    [audio] = await engine.run("acquire.upload", source_path=sample_m4a)
    with pytest.raises(ValueError, match="kind mismatch"):
        await engine.run("video.extract_audio", inputs=[audio.id])


async def test_run_invalid_params_raises(
    engine: Engine, sample_mp4: Path
) -> None:
    from pydantic import ValidationError

    [video] = await engine.run("acquire.upload", source_path=sample_mp4)
    with pytest.raises(ValidationError):
        await engine.run(
            "video.extract_audio",
            inputs=[video.id],
            channels=99,  # only 1 or 2 allowed
        )


async def test_run_unknown_input_id_raises(engine: Engine) -> None:
    with pytest.raises(LookupError, match="input artifact not found"):
        await engine.run("video.extract_audio", inputs=["does_not_exist_id"])


async def test_run_records_lineage(
    engine: Engine, sample_mp4: Path
) -> None:
    [video] = await engine.run("acquire.upload", source_path=sample_mp4)
    [audio] = await engine.run("video.extract_audio", inputs=[video.id])
    tree = engine.lineage(audio.id)
    assert tree is not None
    assert tree.artifact.id == audio.id
    assert tree.op_run is not None
    assert tree.op_run.op_name == "video.extract_audio"
    assert len(tree.parents) == 1
    assert tree.parents[0].artifact.id == video.id


async def test_estimate_op_cost_no_run(
    engine: Engine, sample_mp4: Path
) -> None:
    est = engine.estimate_op_cost("acquire.upload", source_path=sample_mp4)
    assert est.local_seconds >= 0


def test_estimate_op_cost_unknown_op_raises(engine: Engine) -> None:
    with pytest.raises(LookupError):
        engine.estimate_op_cost("never.heard", source_path=Path("/x"))
