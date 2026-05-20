"""Smokes for the bundled ``profiles/examples/*.yaml`` files.

These don't *execute* the profiles (the URL profile needs network +
mlx + a cloud LLM); they just assert the YAML loads, validates, and
compiles to a runnable DAG against the registered op catalog. If the
catalog drifts away from what an example demonstrates, this test
fails first — the example doc stays honest.
"""

from __future__ import annotations

from pathlib import Path

from media_engine.profiles.loader import load_profile
from media_engine.profiles.pipeline import compile_pipeline_profile
from media_engine.profiles.schema import PipelineProfile

EXAMPLES_DIR = Path(__file__).parent.parent / "profiles" / "examples"


def test_url_to_summary_loads() -> None:
    p = EXAMPLES_DIR / "url-to-summary.yaml"
    assert p.exists(), f"missing bundled example: {p}"
    profile = load_profile(p)
    assert isinstance(profile, PipelineProfile)
    assert profile.name == "url-to-summary"
    # Four-node DAG: acquire.url → extract_audio → transcribe → summarize.
    node_ops = [n.op for n in profile.graph]
    assert node_ops == [
        "acquire.url",
        "video.extract_audio",
        "audio.transcribe",
        "intelligence.summarize",
    ]
    assert profile.outputs == ["summary"]


def test_url_to_summary_compiles_with_no_sources() -> None:
    p = EXAMPLES_DIR / "url-to-summary.yaml"
    profile = load_profile(p)
    assert isinstance(profile, PipelineProfile)
    pipeline = compile_pipeline_profile(profile, sources={})
    assert pipeline.name == "url-to-summary"
    # The compiled DAG references the four declared nodes plus zero
    # sources — exactly what the reanalysis recipe walks through.
    assert [n.id for n in pipeline.nodes] == [
        "video", "audio", "transcript", "summary"
    ]


def test_transcribe_and_diarize_still_loads() -> None:
    p = EXAMPLES_DIR / "transcribe-and-diarize.yaml"
    profile = load_profile(p)
    assert isinstance(profile, PipelineProfile)
    assert profile.name == "transcribe-and-diarize"
