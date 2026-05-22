"""Seed synthetic fixture artifacts for the Web UI screenshot run.

Called by `scripts/gen_ui_screenshots.sh` after the temp permanent_store
+ namespace are set up but before `med web start` boots. The fixtures
populate the Catalog (so the catalog browser / detail / lineage shots
have something to display), and seed one completed Job (so the Jobs
dashboard is non-empty).

Everything written here lands in the isolated MEDIA_ENGINE_PERMANENT_STORE
the bash wrapper provisions; nothing touches the operator's real artifacts.

Intentionally avoids running real ops — that would pull in ML deps,
network, and tens of seconds of wall time. The shapes match what the
engine emits in production (same Pydantic models, same metadata fields).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from media_engine.artifacts.analysis import SessionAnalysis
from media_engine.artifacts.media import Video
from media_engine.artifacts.text import Diarization, Transcript
from media_engine.config import EngineConfig
from media_engine.runtime.cache import Cache


def _placeholder_video_bytes() -> bytes:
    """A tiny but valid mp4 container. The screenshots use the metadata
    panel, not the player — we just need a file to point at.
    """
    # Minimal ftyp + mdat box. Not playable but ffprobe-parseable.
    ftyp = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    mdat = b"\x00\x00\x00\x08mdat"
    return ftyp + mdat


def main() -> None:
    cfg = EngineConfig()
    cache = Cache(cfg.resolve_cache_db_url())
    ns = cfg.namespace

    store = cfg.permanent_store / "artifacts"
    store.mkdir(parents=True, exist_ok=True)

    # ── Video ────────────────────────────────────────────────────────
    # Use a deterministic synthetic id; we own the namespace.
    video_id = "a" * 64
    video_path = store / video_id[:2] / f"{video_id}.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(_placeholder_video_bytes())

    video = Video(
        id=video_id,
        path=video_path,
        derived_from=(),
        produced_by=None,
        namespace=ns,
        created_at=datetime.now(UTC),
        metadata={
            "source": "screenshot-fixture://keynote-2026-05-18.mp4",
            "duration": 1842.5,
            "codec": "h264",
            "fps": 30.0,
            "width": 1920,
            "height": 1080,
            "size_bytes": 312_447_899,
        },
    )
    cache.upsert_artifact(video)

    # ── Transcript (derived from video) ──────────────────────────────
    transcript_id = "b" * 64
    transcript_path = store / transcript_id[:2] / f"{transcript_id}.json"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    segments = [
        {
            "start": 0.0,
            "end": 4.2,
            "text": "Welcome to the keynote. Today we're shipping framepulse v0.6.",
            "speaker_id": "Speaker_0001",
            "speaker_name": "Alex Rivera",
        },
        {
            "start": 4.2,
            "end": 9.6,
            "text": "Six transports, thirty-four capability-named ops, and a brand new Web UI.",
            "speaker_id": "Speaker_0001",
            "speaker_name": "Alex Rivera",
        },
        {
            "start": 9.6,
            "end": 15.1,
            "text": "Let's start by acquiring a video and watching the DAG light up.",
            "speaker_id": "Speaker_0002",
            "speaker_name": "Sam Park",
        },
        {
            "start": 15.1,
            "end": 21.8,
            "text": (
                "The transcript surfaces inline; speaker identity is fuzzy-matched "
                "against our roster."
            ),
            "speaker_id": "Speaker_0002",
            "speaker_name": "Sam Park",
        },
    ]
    transcript_path.write_text(json.dumps({"segments": segments}, indent=2))
    transcript = Transcript(
        id=transcript_id,
        path=transcript_path,
        derived_from=(video_id,),
        produced_by="audio.transcribe_diarized@1.0:mlx-whisper@0.4",
        namespace=ns,
        created_at=datetime.now(UTC),
        metadata={
            "segments": segments,
            "language": "en",
            "model": "whisper-large-v3-mlx",
            "speaker_names": {
                "Speaker_0001": "Alex Rivera",
                "Speaker_0002": "Sam Park",
            },
        },
    )
    cache.upsert_artifact(transcript)

    # ── Diarization (derived from video, sibling of transcript) ──────
    diarization_id = "c" * 64
    diarization_path = store / diarization_id[:2] / f"{diarization_id}.json"
    diarization_path.parent.mkdir(parents=True, exist_ok=True)
    diarization_segments = [
        {"start": s["start"], "end": s["end"], "speaker_id": s["speaker_id"]}
        for s in segments
    ]
    diarization_path.write_text(json.dumps({"segments": diarization_segments}, indent=2))
    diarization = Diarization(
        id=diarization_id,
        path=diarization_path,
        derived_from=(video_id,),
        produced_by="audio.diarize@1.0:pyannote@3.1",
        namespace=ns,
        created_at=datetime.now(UTC),
        metadata={
            "segments": diarization_segments,
            "num_speakers": 2,
        },
    )
    cache.upsert_artifact(diarization)

    # ── SessionAnalysis (derived from transcript) ────────────────────
    analysis_id = "d" * 64
    analysis_path = store / analysis_id[:2] / f"{analysis_id}.json"
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_payload = {
        "summary": "A short keynote walkthrough of framepulse v0.6's Web UI.",
        "topics": ["Web UI", "DAG execution", "Speaker identity"],
        "entities": ["framepulse", "Apple Silicon", "SvelteKit"],
        "claims": [
            "All thirty-four operations are reachable from the browser.",
            "The dist tree ships in the wheel — no Node toolchain required.",
        ],
        "sentiment": {"polarity": 0.4, "confidence": 0.8},
        "questions": ["When does Phase 7 (acoustic speaker identity) land?"],
    }
    analysis_path.write_text(json.dumps(analysis_payload, indent=2))
    analysis = SessionAnalysis(
        id=analysis_id,
        path=analysis_path,
        derived_from=(transcript_id,),
        produced_by="intelligence.analyze@1.1:gemini@2.0",
        namespace=ns,
        created_at=datetime.now(UTC),
        metadata={"payload": analysis_payload, "model": "gemini-2.0-pro"},
    )
    cache.upsert_artifact(analysis)

    print(f"[fixtures] seeded 4 artifacts under namespace={ns!r}")
    print(f"  Video:           {video_id}")
    print(f"  Transcript:      {transcript_id}")
    print(f"  Diarization:     {diarization_id}")
    print(f"  SessionAnalysis: {analysis_id}")


if __name__ == "__main__":
    main()
