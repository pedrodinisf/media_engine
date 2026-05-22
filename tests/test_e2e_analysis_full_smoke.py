"""End-to-end smoke for the analysis-full pipeline's *downstream* half.

The full pipeline starts with ``video.extract_audio`` + ``audio.transcribe_
diarized`` which require ffmpeg + mlx-whisper + pyannote — heavy
optional deps that we don't want to gate this smoke on. So we build a
synthetic ``Transcript`` (the shape ``audio.transcribe_diarized`` would
emit) and run the *downstream three* ops through ``engine.run`` end to
end:

    speakers.identify -> intelligence.analyze -> report.session

The ``intelligence.extract`` backend is faked (see ``tests/_intel.py``)
so the test is deterministic and runs in well under a second. The
result is a real ``MarkdownArtifact`` rendered through the bundled
``profiles/analysis-full/session_report.md.j2`` template.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from media_engine.artifacts import (
    Kind,
    MarkdownArtifact,
    Transcript,
    compute_derived_artifact_id,
)
from media_engine.runtime.engine import Engine
from tests._intel import register_fake_extract_backend, unregister_fake

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = REPO_ROOT / "profiles" / "analysis-full"


@pytest.fixture
def fake_extract() -> Any:
    register_fake_extract_backend()
    try:
        yield
    finally:
        unregister_fake()


def _build_diarized_transcript(engine: Engine) -> Transcript:
    segments: list[dict[str, Any]] = [
        {
            "start": 0.0,
            "end": 8.0,
            "text": "Hello everyone, my name is Alex Example and I host this show.",
            "speaker_id": "SPEAKER_00",
        },
        {
            "start": 8.0,
            "end": 25.0,
            "text": "Today we look at the engine's pipeline architecture in depth.",
            "speaker_id": "SPEAKER_00",
        },
        {
            "start": 25.0,
            "end": 38.0,
            "text": "Thanks for having me. I'm Sam Placeholder and I work on backends.",
            "speaker_id": "SPEAKER_01",
        },
        {
            "start": 38.0,
            "end": 60.0,
            "text": "We will cover content-addressed caching and DAG execution next.",
            "speaker_id": "SPEAKER_01",
        },
    ]
    payload = {
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "language": "en",
        "model": "synthetic",
        "diarization_model": "synthetic",
        "num_speakers": 2,
    }
    derived_id = compute_derived_artifact_id(
        kind=Kind.Transcript,
        op_name="test.e2e_synth",
        op_version="1",
        backend_name=None,
        backend_version=None,
        params={"salt": "e2e"},
        input_ids=[],
    )
    tmp = engine.storage.ensure_workdir("e2e") / "t.json"
    tmp.write_text(json.dumps(payload))
    dest = engine.storage.store_file(tmp, derived_id, ".json")
    t = Transcript(
        id=derived_id,
        path=dest,
        metadata=payload,
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(t)
    return t


async def test_downstream_pipeline_end_to_end(
    engine: Engine, fake_extract: Any
) -> None:
    """speakers.identify -> intelligence.analyze -> report.session, end to end."""
    t = _build_diarized_transcript(engine)

    # Step 1: resolve speaker names against the bundled CSV.
    [identified] = await engine.run(
        "speakers.identify",
        inputs=[t.id],
        speaker_db=PROFILE_DIR / "speakers.csv",
        min_confidence=0.7,
    )
    speaker_names = identified.metadata["speaker_names"]
    assert speaker_names["SPEAKER_00"] == "Alex Example"
    assert speaker_names["SPEAKER_01"] == "Sam Placeholder"

    # Step 2: run intelligence.analyze with the bundled prompt + schema.
    # The fake extract backend reads the schema and returns a minimal
    # JSON instance that validates, so we exercise the real plumbing
    # without a network call.
    [analyzed] = await engine.run(
        "intelligence.analyze",
        inputs=[identified.id],
        prompt_path=PROFILE_DIR / "analyze_prompt.md",
        schema_def=str(PROFILE_DIR / "analysis_schema.json"),
        model="gemini-2.5-flash",  # routes to the fake "gemini" extract backend
        window=2,
    )
    assert analyzed.kind == Kind.SessionAnalysis
    data: list[dict[str, Any]] = analyzed.metadata["data"]
    assert len(data) == 2  # 4 segments at window=2 -> 2 windows
    for window in data:
        assert "analysis" in window
        assert "summary" in window["analysis"]
        assert "sentiment" in window["analysis"]

    # Step 3: render the bundled jinja2 template into Markdown.
    [report] = await engine.run(
        "report.session",
        inputs=[analyzed.id],
        template=PROFILE_DIR / "session_report.md.j2",
        title="E2E smoke",
    )
    assert isinstance(report, MarkdownArtifact)
    text = Path(report.path).read_text(encoding="utf-8")
    assert "# E2E smoke" in text
    assert "Alex Example" in text or "SPEAKER_00" in text
    assert "Window 0" in text
    assert report.metadata["n_segments"] == 2


async def test_pipeline_caches_correctly_end_to_end(
    engine: Engine, fake_extract: Any
) -> None:
    """Running the same chain twice yields the same final report id —
    cache hit at every step."""
    t = _build_diarized_transcript(engine)

    async def _run() -> str:
        [identified] = await engine.run(
            "speakers.identify",
            inputs=[t.id],
            speaker_db=PROFILE_DIR / "speakers.csv",
        )
        [analyzed] = await engine.run(
            "intelligence.analyze",
            inputs=[identified.id],
            prompt_path=PROFILE_DIR / "analyze_prompt.md",
            schema_def=str(PROFILE_DIR / "analysis_schema.json"),
            model="gemini-2.5-flash",
            window=4,
        )
        [report] = await engine.run(
            "report.session",
            inputs=[analyzed.id],
            template=PROFILE_DIR / "session_report.md.j2",
            title="cache run",
        )
        return report.id

    a = await _run()
    b = await _run()
    assert a == b
