"""Tests for runtime/lineage.py LineageNode shape."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from media_engine.artifacts import Audio, Video
from media_engine.runtime.lineage import LineageNode, OperationRunRef


def _now() -> datetime:
    return datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


def _video(tmp_path: Path, suffix: str = "") -> Video:
    return Video(id=f"vid{suffix}", path=tmp_path / f"v{suffix}.mp4", created_at=_now())


def _op_run(suffix: str = "") -> OperationRunRef:
    return OperationRunRef(
        id=f"run{suffix}",
        op_name="video.extract_audio",
        op_version="1.0.0",
        backend_name="ffmpeg",
        backend_version="1",
        started_at=_now(),
        finished_at=_now(),
        duration_seconds=1.2,
        params={"sample_rate": 16000},
    )


def test_lineage_node_leaf_round_trip(tmp_path: Path) -> None:
    leaf = LineageNode(artifact=_video(tmp_path))
    j = leaf.model_dump_json()
    back = LineageNode.model_validate_json(j)
    assert back == leaf


def test_lineage_node_recursive_round_trip(tmp_path: Path) -> None:
    # audio derived from video
    audio = Audio(
        id="aud",
        path=tmp_path / "a.wav",
        derived_from=("vid",),
        produced_by="run1",
        created_at=_now(),
    )
    parent = LineageNode(artifact=_video(tmp_path))
    child = LineageNode(artifact=audio, op_run=_op_run("1"), parents=[parent])
    j = child.model_dump_json()
    back = LineageNode.model_validate_json(j)
    assert back == child
    assert len(back.parents) == 1
    assert back.parents[0].artifact.id == "vid"


def test_lineage_node_three_levels_deep(tmp_path: Path) -> None:
    grandparent = LineageNode(artifact=_video(tmp_path, "1"))
    parent = LineageNode(
        artifact=Audio(
            id="aud2",
            path=tmp_path / "a2.wav",
            derived_from=("vid1",),
            produced_by="r1",
            created_at=_now(),
        ),
        op_run=_op_run("a"),
        parents=[grandparent],
    )
    child = LineageNode(
        artifact=Audio(
            id="aud3",
            path=tmp_path / "a3.wav",
            derived_from=("aud2",),
            produced_by="r2",
            created_at=_now(),
        ),
        op_run=_op_run("b"),
        parents=[parent],
    )
    assert child.parents[0].parents[0].artifact.id == "vid1"
