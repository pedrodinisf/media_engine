"""Tests for the Operation → MCP tool schema exporter."""

from __future__ import annotations

import json

from media_engine.backends import BackendRegistry
from media_engine.mcp import export_all_ops, export_op_as_mcp_tool
from media_engine.mcp.exporter import _input_schema, _mcp_tool_name
from media_engine.ops import OpRegistry

# Eagerly register the Phase 0 ops so the exporter has something to work with.
from media_engine.ops.acquire import upload as _upload_op  # noqa: F401
from media_engine.ops.acquire.upload import AcquireUpload
from media_engine.ops.video import extract_audio as _extract_op  # noqa: F401
from media_engine.ops.video.extract_audio import VideoExtractAudio

assert _upload_op
assert _extract_op


def test_mcp_tool_name_replaces_dots() -> None:
    assert _mcp_tool_name("video.extract_audio") == "video__extract_audio"
    assert _mcp_tool_name("acquire.upload") == "acquire__upload"


def test_export_acquire_upload_basic_shape() -> None:
    spec = export_op_as_mcp_tool(AcquireUpload)
    assert spec["name"] == "acquire__upload"
    assert "Run acquire.upload" in spec["description"] or "ingest" in spec["description"].lower()
    assert "acquire.upload" in spec["description"]
    schema = spec["inputSchema"]
    assert schema["type"] == "object"
    assert "source_path" in schema["properties"]
    # Nullary op → no input_artifact_ids in the schema.
    assert "input_artifact_ids" not in schema["properties"]


def test_export_video_extract_audio_includes_input_artifact_ids() -> None:
    spec = export_op_as_mcp_tool(VideoExtractAudio)
    schema = spec["inputSchema"]
    assert "input_artifact_ids" in schema["properties"]
    iai = schema["properties"]["input_artifact_ids"]
    assert iai["type"] == "array"
    assert iai["items"] == {"type": "string"}
    assert iai["minItems"] == 1
    assert iai["maxItems"] == 1
    assert "input_artifact_ids" in schema["required"]


def test_export_includes_backend_when_registered() -> None:
    """Register a fake backend → it should appear as an enum on backend prop."""
    from media_engine.backends import Backend, BackendRequirements
    from media_engine.ops import CostEstimate

    BackendRegistry.unregister("video.extract_audio", "ffmpeg-test")

    class _FakeBackend(Backend):
        op_name = "video.extract_audio"
        name = "ffmpeg-test"
        version = "1.0"
        requires = BackendRequirements()

        async def execute(self, inputs, params, ctx):
            return []

        def cost_estimate(self, inputs, params):
            return CostEstimate()

    BackendRegistry.register(_FakeBackend)
    try:
        spec = export_op_as_mcp_tool(VideoExtractAudio)
        backend_prop = spec["inputSchema"]["properties"].get("backend")
        assert backend_prop is not None
        assert backend_prop["type"] == "string"
        assert "ffmpeg-test" in backend_prop["enum"]
    finally:
        BackendRegistry.unregister("video.extract_audio", "ffmpeg-test")


def test_export_no_backend_property_when_none_registered() -> None:
    """video.extract_audio has no backend layer (logic embedded in the Op)."""
    spec = export_op_as_mcp_tool(VideoExtractAudio)
    assert "backend" not in spec["inputSchema"]["properties"]


def test_export_all_ops_returns_one_spec_per_registered() -> None:
    specs = export_all_ops()
    names = {s["name"] for s in specs}
    assert "acquire__upload" in names
    assert "video__extract_audio" in names
    assert len(specs) == len(OpRegistry.list_all())


def test_export_all_ops_is_serializable_json() -> None:
    specs = export_all_ops()
    out = json.dumps(specs, sort_keys=True)
    parsed = json.loads(out)
    assert parsed == specs


def test_input_schema_carries_pydantic_defs() -> None:
    """If a params model uses nested submodels, $defs should survive."""
    schema = _input_schema(VideoExtractAudio)
    # ExtractAudioParams uses literals + ints — no nested defs expected, but
    # the field should at least exist as a dict if present.
    if "$defs" in schema:
        assert isinstance(schema["$defs"], dict)


def test_cli_mcp_tools_json_command() -> None:
    """Smoke-test the CLI subcommand prints valid JSON."""
    from typer.testing import CliRunner

    from media_engine.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["mcp", "tools-json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    names = {s["name"] for s in payload}
    assert "acquire__upload" in names
    assert "video__extract_audio" in names
