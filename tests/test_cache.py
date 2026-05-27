"""Tests for runtime/cache.py.

Covers: write/read, cache miss → record → cache hit, concurrent reads (WAL),
prefix resolution, lineage tree, namespace isolation, Pydantic↔ORM round-trip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from media_engine.artifacts import Audio, Kind, Video
from media_engine.runtime.cache import Cache, to_orm, to_pydantic


def _now(offset_min: int = 0) -> datetime:
    return datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC) + timedelta(minutes=offset_min)


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(f"sqlite+pysqlite:///{tmp_path / 'c.db'}")


def _video(idx: str, tmp_path: Path, **kw: object) -> Video:
    return Video(
        id=idx,
        path=tmp_path / f"{idx}.mp4",
        metadata={"duration": 1.0},
        created_at=_now(),
        **kw,  # type: ignore[arg-type]
    )


def _audio(
    idx: str,
    tmp_path: Path,
    parent_id: str,
    run_id: str | None = None,
) -> Audio:
    return Audio(
        id=idx,
        path=tmp_path / f"{idx}.wav",
        metadata={"sample_rate": 16000, "channels": 1},
        derived_from=(parent_id,),
        produced_by=run_id,
        created_at=_now(1),
    )


# ─────────────────────────────────────────────────────────────────
# Pydantic ↔ ORM boundary
# ─────────────────────────────────────────────────────────────────


def test_to_orm_to_pydantic_round_trip(tmp_path: Path) -> None:
    v = _video("aaaa", tmp_path)
    row = to_orm(v)
    back = to_pydantic(row)
    assert back == v


def test_to_orm_serializes_metadata_deterministically(tmp_path: Path) -> None:
    v1 = Video(
        id="abcd",
        path=tmp_path / "v.mp4",
        metadata={"a": 1, "b": 2},
        created_at=_now(),
    )
    v2 = Video(
        id="abcd",
        path=tmp_path / "v.mp4",
        metadata={"b": 2, "a": 1},
        created_at=_now(),
    )
    assert to_orm(v1).metadata_json == to_orm(v2).metadata_json


# ─────────────────────────────────────────────────────────────────
# Artifact storage
# ─────────────────────────────────────────────────────────────────


def test_upsert_then_get(cache: Cache, tmp_path: Path) -> None:
    v = _video("aaaaa", tmp_path)
    cache.upsert_artifact(v)
    back = cache.get_artifact("aaaaa")
    assert back == v


def test_get_returns_none_for_missing(cache: Cache) -> None:
    assert cache.get_artifact("does_not_exist") is None


def test_upsert_idempotent(cache: Cache, tmp_path: Path) -> None:
    v = _video("bbbbb", tmp_path)
    cache.upsert_artifact(v)
    cache.upsert_artifact(v)
    assert cache.get_artifact("bbbbb") == v


def test_list_artifacts_filter_by_kind(cache: Cache, tmp_path: Path) -> None:
    cache.upsert_artifact(_video("vvvvv", tmp_path))
    cache.upsert_artifact(_audio("aaaaa", tmp_path, parent_id="vvvvv"))
    videos = cache.list_artifacts(kind=Kind.Video)
    audios = cache.list_artifacts(kind=Kind.Audio)
    assert {a.id for a in videos} == {"vvvvv"}
    assert {a.id for a in audios} == {"aaaaa"}


def test_list_artifacts_limit(cache: Cache, tmp_path: Path) -> None:
    for i in range(5):
        cache.upsert_artifact(_video(f"v{i}xxx", tmp_path))
    rows = cache.list_artifacts(limit=2)
    assert len(rows) == 2


def test_list_artifacts_namespace_isolation(cache: Cache, tmp_path: Path) -> None:
    v_default = _video("vvvvv", tmp_path)
    v_other = Video(
        id="other",
        path=tmp_path / "x.mp4",
        namespace="alt",
        created_at=_now(),
    )
    cache.upsert_artifact(v_default)
    cache.upsert_artifact(v_other)
    assert {a.id for a in cache.list_artifacts(namespace="default")} == {"vvvvv"}
    assert {a.id for a in cache.list_artifacts(namespace="alt")} == {"other"}


def test_upsert_artifact_raises_on_namespace_conflict(
    cache: Cache, tmp_path: Path
) -> None:
    """Same id, two namespaces — the cache rejects with a clear
    ``ValueError`` instead of leaving a deferred ``IntegrityError``.

    The schema has ``id`` as the primary key, so cross-tenant
    re-registration of the same bytes is not supported in v1.
    """
    cache.upsert_artifact(_video("shared", tmp_path))
    foreign = Video(
        id="shared",
        path=tmp_path / "shared.mp4",
        namespace="tenant-foo",
        created_at=_now(),
    )
    with pytest.raises(ValueError, match="already exists in namespace"):
        cache.upsert_artifact(foreign)


# ─────────────────────────────────────────────────────────────────
# Operation runs (cache miss → record → cache hit)
# ─────────────────────────────────────────────────────────────────


def test_find_cached_run_miss(cache: Cache) -> None:
    out = cache.find_cached_run(
        op_name="video.extract_audio",
        op_version="1.0.0",
        backend_name=None,
        backend_version=None,
        params_hash="deadbeef",
        input_ids=["vvvvv"],
    )
    assert out is None


def test_record_then_find_hit(cache: Cache) -> None:
    started = _now()
    finished = _now() + timedelta(seconds=2)
    run_id = cache.record_run(
        op_name="video.extract_audio",
        op_version="1.0.0",
        backend_name="ffmpeg",
        backend_version="1",
        params={"sample_rate": 16000},
        params_hash="phash",
        input_ids=["vvvvv"],
        output_ids=["aaaaa"],
        cost_estimate={"local_seconds": 0.5},
        actual_cost={"local_seconds": 0.4},
        duration_seconds=2.0,
        started_at=started,
        finished_at=finished,
    )
    assert run_id  # uuid hex string

    hit = cache.find_cached_run(
        op_name="video.extract_audio",
        op_version="1.0.0",
        backend_name="ffmpeg",
        backend_version="1",
        params_hash="phash",
        input_ids=["vvvvv"],
    )
    assert hit == ["aaaaa"]


def test_find_cached_run_input_id_order_independent(cache: Cache) -> None:
    cache.record_run(
        op_name="x.y",
        op_version="1.0.0",
        backend_name=None,
        backend_version=None,
        params={},
        params_hash="ph",
        input_ids=["bbbbb", "aaaaa"],
        output_ids=["ccccc"],
        cost_estimate=None,
        actual_cost=None,
        duration_seconds=None,
        started_at=_now(),
        finished_at=_now(),
    )
    hit = cache.find_cached_run(
        op_name="x.y",
        op_version="1.0.0",
        backend_name=None,
        backend_version=None,
        params_hash="ph",
        input_ids=["aaaaa", "bbbbb"],
    )
    assert hit == ["ccccc"]


def test_find_cached_run_namespace_isolation(cache: Cache) -> None:
    cache.record_run(
        op_name="x.y", op_version="1", backend_name=None, backend_version=None,
        params={}, params_hash="p", input_ids=["i"], output_ids=["o-default"],
        cost_estimate=None, actual_cost=None, duration_seconds=None,
        started_at=_now(), finished_at=_now(), namespace="default",
    )
    cache.record_run(
        op_name="x.y", op_version="1", backend_name=None, backend_version=None,
        params={}, params_hash="p", input_ids=["i"], output_ids=["o-alt"],
        cost_estimate=None, actual_cost=None, duration_seconds=None,
        started_at=_now(), finished_at=_now(), namespace="alt",
    )
    assert cache.find_cached_run(
        op_name="x.y", op_version="1", backend_name=None, backend_version=None,
        params_hash="p", input_ids=["i"], namespace="default",
    ) == ["o-default"]
    assert cache.find_cached_run(
        op_name="x.y", op_version="1", backend_name=None, backend_version=None,
        params_hash="p", input_ids=["i"], namespace="alt",
    ) == ["o-alt"]


# ─────────────────────────────────────────────────────────────────
# Prefix resolution
# ─────────────────────────────────────────────────────────────────


def test_resolve_id_prefix_unique(cache: Cache, tmp_path: Path) -> None:
    cache.upsert_artifact(_video("abcd1234", tmp_path))
    cache.upsert_artifact(_video("xxxx9999", tmp_path))
    assert cache.resolve_id_prefix("abcd") == ["abcd1234"]


def test_resolve_id_prefix_ambiguous(cache: Cache, tmp_path: Path) -> None:
    cache.upsert_artifact(_video("abcd1111", tmp_path))
    cache.upsert_artifact(_video("abcd2222", tmp_path))
    matches = cache.resolve_id_prefix("abcd")
    assert set(matches) == {"abcd1111", "abcd2222"}


def test_resolve_id_prefix_too_short_raises(cache: Cache) -> None:
    with pytest.raises(ValueError, match="at least 4"):
        cache.resolve_id_prefix("abc")


# ─────────────────────────────────────────────────────────────────
# Lineage tree (3 levels)
# ─────────────────────────────────────────────────────────────────


def test_lineage_tree_three_levels(cache: Cache, tmp_path: Path) -> None:
    # video → audio (run1) → transcript (run2)
    v = _video("video000", tmp_path)
    cache.upsert_artifact(v)
    run1 = cache.record_run(
        op_name="video.extract_audio", op_version="1.0.0",
        backend_name="ffmpeg", backend_version="1",
        params={"sample_rate": 16000}, params_hash="ph1",
        input_ids=["video000"], output_ids=["audio000"],
        cost_estimate=None, actual_cost=None, duration_seconds=1.0,
        started_at=_now(), finished_at=_now(),
    )
    a = _audio("audio000", tmp_path, parent_id="video000", run_id=run1)
    cache.upsert_artifact(a)
    run2 = cache.record_run(
        op_name="audio.transcribe", op_version="1.0.0",
        backend_name="mlx-whisper", backend_version="1",
        params={"model": "small"}, params_hash="ph2",
        input_ids=["audio000"], output_ids=["trans000"],
        cost_estimate=None, actual_cost=None, duration_seconds=2.0,
        started_at=_now(), finished_at=_now(),
    )
    t = Audio(  # use Audio for shape; Transcript would also work
        id="trans000",
        path=tmp_path / "t.json",
        derived_from=("audio000",),
        produced_by=run2,
        created_at=_now(),
    )
    cache.upsert_artifact(t)

    tree = cache.lineage_tree("trans000")
    assert tree is not None
    assert tree.artifact.id == "trans000"
    assert tree.op_run is not None and tree.op_run.op_name == "audio.transcribe"
    assert len(tree.parents) == 1
    audio_node = tree.parents[0]
    assert audio_node.artifact.id == "audio000"
    assert audio_node.op_run is not None and audio_node.op_run.op_name == "video.extract_audio"
    assert len(audio_node.parents) == 1
    assert audio_node.parents[0].artifact.id == "video000"


def test_lineage_tree_missing_root(cache: Cache) -> None:
    assert cache.lineage_tree("does_not_exist") is None


def test_lineage_tree_depth_limit(cache: Cache, tmp_path: Path) -> None:
    # video → audio → transcript; depth=1 truncates after audio
    v = _video("video000", tmp_path)
    cache.upsert_artifact(v)
    a = _audio("audio000", tmp_path, parent_id="video000")
    cache.upsert_artifact(a)
    t = Audio(
        id="trans000", path=tmp_path / "t.json",
        derived_from=("audio000",), produced_by=None, created_at=_now(),
    )
    cache.upsert_artifact(t)
    tree = cache.lineage_tree("trans000", max_depth=1)
    assert tree is not None
    assert len(tree.parents) == 1
    # depth=1 means the audio node still resolves but its further parents don't
    assert tree.parents[0].artifact.id == "audio000"
    assert tree.parents[0].parents == []
    # The truncated audio node carries the explicit "we stopped here" flag
    # so REST / CLI consumers can render it rather than silently lying.
    assert tree.parents[0].truncated_reason == "max_depth"
    assert tree.truncated_reason is None  # the root walked successfully


def test_lineage_tree_depth_zero_flags_truncation(
    cache: Cache, tmp_path: Path
) -> None:
    """``max_depth=0`` truncates immediately if the artifact has parents."""
    v = _video("video111", tmp_path)
    cache.upsert_artifact(v)
    a = _audio("audio111", tmp_path, parent_id="video111")
    cache.upsert_artifact(a)
    tree = cache.lineage_tree("audio111", max_depth=0)
    assert tree is not None
    assert tree.parents == []
    assert tree.truncated_reason == "max_depth"


def test_lineage_tree_leaf_no_truncation(cache: Cache, tmp_path: Path) -> None:
    """A leaf (no ``derived_from``) is never flagged truncated, even at
    depth 0."""
    v = _video("video222", tmp_path)
    cache.upsert_artifact(v)
    tree = cache.lineage_tree("video222", max_depth=0)
    assert tree is not None
    assert tree.truncated_reason is None


# ─────────────────────────────────────────────────────────────────
# WAL pragma + concurrent reads
# ─────────────────────────────────────────────────────────────────


def test_sqlite_wal_mode_enabled(cache: Cache) -> None:
    with cache.session() as s:
        result = s.execute(__import__("sqlalchemy").text("PRAGMA journal_mode")).scalar()
        assert str(result).lower() == "wal"


def test_concurrent_caches_share_db(tmp_path: Path) -> None:
    """Two Cache handles against the same DB file see each other's writes."""
    db_url = f"sqlite+pysqlite:///{tmp_path / 'shared.db'}"
    a = Cache(db_url)
    b = Cache(db_url)
    try:
        a.upsert_artifact(
            Video(id="shared00", path=tmp_path / "x.mp4", created_at=_now())
        )
        back = b.get_artifact("shared00")
        assert back is not None and back.id == "shared00"
    finally:
        a.close()
        b.close()


# ─────────────────────────────────────────────────────────────────
# Survives reopen (the artifact persists across Cache instances)
# ─────────────────────────────────────────────────────────────────


def test_cache_survives_close_reopen(tmp_path: Path) -> None:
    db_url = f"sqlite+pysqlite:///{tmp_path / 'persisted.db'}"
    c1 = Cache(db_url)
    c1.upsert_artifact(Video(id="persistx", path=tmp_path / "x.mp4", created_at=_now()))
    c1.close()

    c2 = Cache(db_url)
    try:
        back = c2.get_artifact("persistx")
        assert back is not None and back.id == "persistx"
    finally:
        c2.close()


def test_list_artifacts_hides_ephemeral_by_default(
    cache: Cache, tmp_path: Path
) -> None:
    """The catalog list excludes artifacts marked
    ``metadata.ephemeral = true`` (today: the single-frame FrameSets
    that video.comprehend's fan-out produces). ``include_ephemeral=True``
    opts back in for debugging."""
    from media_engine.artifacts import FrameSet
    # Normal artifact — should show.
    cache.upsert_artifact(_video("v" * 8, tmp_path))
    # Ephemeral FrameSet — should be hidden by default.
    fs_ephemeral = FrameSet(
        id="e" * 64,
        path=tmp_path / "e.json",
        metadata={
            "frame_ids": ["f" * 64],
            "original_indices": [0],
            "parent_frameset_id": "p" * 64,
            "parent_position": 0,
            "ephemeral": True,
        },
        derived_from=("p" * 64,),
        created_at=_now(),
    )
    cache.upsert_artifact(fs_ephemeral)

    hidden = cache.list_artifacts()
    assert all(a.id != fs_ephemeral.id for a in hidden), (
        "ephemeral=True must be filtered out by default"
    )
    visible = cache.list_artifacts(include_ephemeral=True)
    assert any(a.id == fs_ephemeral.id for a in visible), (
        "include_ephemeral=True must show them"
    )


def test_list_artifacts_hides_legacy_single_frame_framesets(
    cache: Cache, tmp_path: Path
) -> None:
    """Backwards-compat: artifacts persisted BEFORE the
    metadata.ephemeral flag was added (i.e. by a pre-fix
    video.comprehend run) are still hidden, because they have the
    distinctive ``parent_position`` field that no other op writes."""
    from media_engine.artifacts import FrameSet
    fs_legacy = FrameSet(
        id="l" * 64,
        path=tmp_path / "l.json",
        metadata={
            # Note: no ``ephemeral`` field — this is the pre-flag shape
            # that lives in existing stores.
            "frame_ids": ["x" * 64],
            "original_indices": [42],
            "parent_frameset_id": "p" * 64,
            "parent_position": 42,
            "fps": 0.3,
        },
        derived_from=("p" * 64,),
        created_at=_now(),
    )
    cache.upsert_artifact(fs_legacy)
    hidden = cache.list_artifacts()
    assert all(a.id != fs_legacy.id for a in hidden), (
        "legacy single-frame FrameSets (parent_position present) must be hidden"
    )
