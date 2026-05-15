"""Tests for profiles/{loader,schema,pipeline}.py."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from media_engine.artifacts import AnyArtifact, Video
from media_engine.profiles import (
    PipelineProfile,
    ProfileLoadError,
    PromptProfile,
    discover_profiles,
    load_profile,
)
from media_engine.profiles.pipeline import (
    ProfileCompileError,
    compile_pipeline_profile,
    compile_profile,
    compile_prompt_profile,
)
from media_engine.profiles.schema import GraphNodeSpec, InputSpec
from media_engine.runtime.dag import Pipeline
from media_engine.runtime.engine import Engine

TEST_YAML = """
profile_schema_version: "1.0"
name: tiny
kind: pipeline
description: minimal one-node graph
inputs:
  - { name: source, kind: video }
graph:
  - id: extract
    op: video.extract_audio
    inputs: { in: source }
    params: { sample_rate: 16000 }
outputs: [extract]
"""

TEST_YAML_LIST_INPUTS = """
profile_schema_version: "1.0"
name: tinylist
kind: pipeline
inputs:
  - { name: source, kind: video }
graph:
  - id: extract
    op: video.extract_audio
    inputs: [source]
"""


def _video(tmp_path: Path) -> Video:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"\x00")
    return Video(id="v" * 64, path=f, created_at=datetime.now(UTC))


# ─────────────────────────────────────────────────────────────────
# load_profile
# ─────────────────────────────────────────────────────────────────


def test_load_yaml_pipeline(tmp_path: Path) -> None:
    p = tmp_path / "p.yaml"
    p.write_text(TEST_YAML)
    profile = load_profile(p)
    assert isinstance(profile, PipelineProfile)
    assert profile.name == "tiny"
    assert profile.outputs == ["extract"]
    assert len(profile.graph) == 1


def test_load_yaml_list_inputs(tmp_path: Path) -> None:
    p = tmp_path / "p.yaml"
    p.write_text(TEST_YAML_LIST_INPUTS)
    profile = load_profile(p)
    assert isinstance(profile, PipelineProfile)
    assert profile.graph[0].inputs == ["source"]


def test_load_md_prompt_profile(tmp_path: Path) -> None:
    p = tmp_path / "tutorial.md"
    p.write_text(
        "---\n"
        "name: technical\n"
        "default_op: video.extract_audio\n"
        "---\n"
        "Extract the technical content from this video.\n"
    )
    profile = load_profile(p)
    assert isinstance(profile, PromptProfile)
    assert profile.name == "technical"
    assert profile.default_op == "video.extract_audio"
    assert "technical content" in profile.body


def test_load_md_uses_filename_for_name(tmp_path: Path) -> None:
    p = tmp_path / "named-from-stem.md"
    p.write_text("Body only.\n")
    profile = load_profile(p)
    assert profile.name == "named-from-stem"


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ProfileLoadError, match="not found"):
        load_profile(tmp_path / "nope.yaml")


def test_load_unknown_extension_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.txt"
    p.write_text("anything")
    with pytest.raises(ProfileLoadError, match="unknown profile extension"):
        load_profile(p)


def test_load_invalid_yaml_raises(tmp_path: Path) -> None:
    p = tmp_path / "broken.yaml"
    p.write_text(": :: bad ::\n")
    with pytest.raises(ProfileLoadError, match="YAML parse error"):
        load_profile(p)


def test_load_yaml_unknown_kind_raises(tmp_path: Path) -> None:
    p = tmp_path / "weird.yaml"
    p.write_text(
        "profile_schema_version: '1.0'\n"
        "name: weird\n"
        "kind: chocolate\n"
        "graph: []\n"
    )
    with pytest.raises(ProfileLoadError, match="unknown profile kind"):
        load_profile(p)


def test_load_yaml_empty_graph_rejected(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text(
        "profile_schema_version: '1.0'\n"
        "name: e\n"
        "kind: pipeline\n"
        "graph: []\n"
    )
    with pytest.raises(ProfileLoadError, match="schema validation failed"):
        load_profile(p)


# ─────────────────────────────────────────────────────────────────
# discover_profiles
# ─────────────────────────────────────────────────────────────────


def test_discover_combines_dirs(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg"
    repo_dir = tmp_path / "repo"
    cfg_dir.mkdir()
    repo_dir.mkdir()
    (cfg_dir / "a.yaml").write_text(TEST_YAML.replace("name: tiny", "name: a"))
    (repo_dir / "b.yaml").write_text(TEST_YAML.replace("name: tiny", "name: b"))

    out = discover_profiles(config_dir=cfg_dir, repo_dir=repo_dir)
    assert {"a", "b"}.issubset(out.keys())


def test_discover_later_dir_overrides(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "cfg"
    repo_dir = tmp_path / "repo"
    cfg_dir.mkdir()
    repo_dir.mkdir()
    (cfg_dir / "name.yaml").write_text(
        TEST_YAML.replace("description: minimal one-node graph", "description: from cfg")
    )
    (repo_dir / "name.yaml").write_text(
        TEST_YAML.replace("description: minimal one-node graph", "description: from repo")
    )
    out = discover_profiles(config_dir=cfg_dir, repo_dir=repo_dir)
    assert out["tiny"][1].description == "from repo"


def test_discover_skips_bad_files(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "good.yaml").write_text(TEST_YAML)
    (d / "broken.yaml").write_text(": :: bad")
    out = discover_profiles(profile_dirs=[d])
    assert "tiny" in out


# ─────────────────────────────────────────────────────────────────
# compile
# ─────────────────────────────────────────────────────────────────


def test_compile_pipeline_to_runtime_pipeline(tmp_path: Path) -> None:
    p = tmp_path / "p.yaml"
    p.write_text(TEST_YAML)
    profile = load_profile(p)
    assert isinstance(profile, PipelineProfile)
    sources: dict[str, AnyArtifact] = {"source": _video(tmp_path)}
    pipe = compile_pipeline_profile(profile, sources)
    assert isinstance(pipe, Pipeline)
    assert pipe.name == "tiny"
    assert len(pipe.nodes) == 1
    assert pipe.nodes[0].op_name == "video.extract_audio"
    assert pipe.nodes[0].input_node_ids == ["source"]
    assert pipe.nodes[0].params == {"sample_rate": 16000}
    assert pipe.outputs == ["extract"]


def test_compile_missing_input_raises(tmp_path: Path) -> None:
    p = tmp_path / "p.yaml"
    p.write_text(TEST_YAML)
    profile = load_profile(p)
    assert isinstance(profile, PipelineProfile)
    with pytest.raises(ProfileCompileError, match="missing"):
        compile_pipeline_profile(profile, sources={})


def test_compile_unknown_op_raises(tmp_path: Path) -> None:
    profile = PipelineProfile(
        name="bad",
        inputs=[InputSpec(name="source", kind="video")],
        graph=[GraphNodeSpec(id="x", op="never.heard", inputs=["source"])],
    )
    with pytest.raises(ProfileCompileError, match="unregistered op"):
        compile_pipeline_profile(profile, sources={"source": _video(tmp_path)})


def test_compile_unknown_backend_raises(tmp_path: Path) -> None:
    profile = PipelineProfile(
        name="bad-backend",
        inputs=[InputSpec(name="source", kind="audio")],
        graph=[
            GraphNodeSpec(
                id="t",
                op="audio.transcribe",
                inputs=["source"],
                backend="not-a-backend",
            )
        ],
    )
    with pytest.raises(ProfileCompileError, match="not registered for"):
        compile_pipeline_profile(profile, sources={"source": _video(tmp_path)})


def test_compile_default_outputs_picks_leaves(tmp_path: Path) -> None:
    """When `outputs:` is empty, leaf nodes (no one references them) are selected."""
    profile = PipelineProfile(
        name="leafy",
        inputs=[InputSpec(name="source", kind="video")],
        graph=[
            GraphNodeSpec(id="audio", op="video.extract_audio", inputs=["source"]),
        ],
    )
    pipe = compile_pipeline_profile(profile, sources={"source": _video(tmp_path)})
    assert pipe.outputs == ["audio"]


def test_compile_prompt_profile_one_node(tmp_path: Path) -> None:
    profile = PromptProfile(
        name="describe",
        default_op="video.extract_audio",  # any registered op for the test
        body="Extract pcm wav",
    )
    pipe = compile_prompt_profile(profile, sources={"src": _video(tmp_path)})
    assert len(pipe.nodes) == 1
    assert pipe.nodes[0].op_name == "video.extract_audio"
    assert pipe.nodes[0].params["system_prompt"] == "Extract pcm wav"


def test_compile_dispatch_via_compile_profile(tmp_path: Path) -> None:
    profile = PromptProfile(name="d", default_op="video.extract_audio", body="x")
    pipe = compile_profile(profile, sources={"src": _video(tmp_path)})
    assert isinstance(pipe, Pipeline)


# ─────────────────────────────────────────────────────────────────
# end-to-end: discover + compile + run via Engine
# ─────────────────────────────────────────────────────────────────


async def test_run_bundled_example_profile(
    engine: Engine, sample_mp4: Path
) -> None:
    """The bundled `transcribe-and-diarize` profile compiles and runs (with
    fake transcribe + diarize backends so we don't need real ML deps)."""

    from media_engine.backends import (
        Backend,
        BackendRegistry,
        BackendRequirements,
        register_backend,
    )
    from media_engine.ops import CostEstimate, OperationContext
    from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
    from media_engine.ops.audio.diarize import (
        DiarizeParams,
        build_diarization_artifact,
    )
    from media_engine.ops.audio.transcribe import (
        TranscribeParams,
        build_transcript_artifact,
    )

    BackendRegistry.unregister("audio.transcribe", "mlx-whisper")
    BackendRegistry.unregister("audio.diarize", "pyannote")

    @register_backend
    class _FW(Backend):
        op_name = "audio.transcribe"
        name = "mlx-whisper"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(self, inputs, params, ctx):
            assert isinstance(params, TranscribeParams)
            return [
                build_transcript_artifact(
                    audio=inputs[0], params=params,
                    backend_name=self.name, backend_version=self.version,
                    workdir_path=ctx.workdir, storage=ctx.storage,
                    text="hi", segments=[{"start": 0, "end": 1, "text": "hi"}],
                    language=params.language or "en", model=params.model,
                    duration=inputs[0].duration,
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate()

    @register_backend
    class _FP(Backend):
        op_name = "audio.diarize"
        name = "pyannote"
        version = "0.0.0-fake"
        requires = BackendRequirements()

        async def execute(self, inputs, params, ctx):
            assert isinstance(params, DiarizeParams)
            return [
                build_diarization_artifact(
                    audio=inputs[0], params=params,
                    backend_name=self.name, backend_version=self.version,
                    workdir_path=ctx.workdir, storage=ctx.storage,
                    segments=[{"start": 0, "end": 1, "speaker_id": "SPEAKER_00"}],
                    num_speakers=1, model=params.model,
                )
            ]

        def cost_estimate(self, inputs, params):
            return CostEstimate()

    op = AcquireUpload()
    ctx = OperationContext(
        workdir=engine.storage.ensure_workdir("t"),
        config=engine.config, storage=engine.storage,
        namespace=engine.config.namespace, emit=engine.event_bus.emit,
        server_manager=engine.server_manager, model_pool=engine.model_pool,
    )
    [video] = await op.run([], AcquireUploadParams(source_path=sample_mp4), ctx)
    engine.cache.upsert_artifact(video)

    repo_root = Path(__file__).resolve().parents[1]
    profile_path = repo_root / "profiles" / "examples" / "transcribe-and-diarize.yaml"
    assert profile_path.exists(), f"missing bundled profile at {profile_path}"
    profile = load_profile(profile_path)
    assert isinstance(profile, PipelineProfile)
    pipeline = compile_pipeline_profile(profile, sources={"source": video})
    result = await engine.run_pipeline(pipeline)
    assert result.all_succeeded, result.failures
    transcripts = result.outputs_for("transcript")
    assert len(transcripts) == 1


def test_cli_profile_ls_and_show(tmp_path: Path) -> None:
    """Smoke-test `med profile ls` + `show`."""
    from typer.testing import CliRunner

    from media_engine.cli import app

    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "tiny.yaml").write_text(TEST_YAML)

    runner = CliRunner()
    ls = runner.invoke(app, ["profile", "ls", "--profile-dir", str(profiles_dir)])
    assert ls.exit_code == 0, ls.stdout
    assert "tiny" in ls.stdout

    show = runner.invoke(
        app, ["profile", "show", "tiny", "--profile-dir", str(profiles_dir)]
    )
    assert show.exit_code == 0, show.stdout
    assert "extract" in show.stdout
