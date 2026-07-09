"""Static profile introspection — model fields, provider classification, digest."""

from __future__ import annotations

from media_engine.backends._base import BackendRequirements
from media_engine.profiles.introspect import (
    classify_model_provider,
    enrich_node,
    model_param_fields,
    profile_digest,
)
from media_engine.profiles.schema import GraphNodeSpec, PipelineProfile
from media_engine.runtime.doctor import classify_provider


def test_classify_model_provider_by_prefix() -> None:
    assert classify_model_provider("gemini-2.5-pro") == "cloud"
    assert classify_model_provider("claude-opus-4-7") == "cloud"
    assert classify_model_provider("mlx-community/Qwen2-VL-2B-Instruct-4bit") == "local"
    assert classify_model_provider("pyannote/speaker-diarization-3.1") == "local"
    assert classify_model_provider("sentence-transformers/all-MiniLM-L6-v2") == "local"
    assert classify_model_provider("some-unknown-id") == "unknown"


def test_classify_provider_is_key_suffix_sensitive() -> None:
    # cloud iff an *_API_KEY env is required
    assert classify_provider(BackendRequirements(env=["GEMINI_API_KEY"])) == "cloud"
    # HF_TOKEN is NOT an API key → the backend stays local
    assert (
        classify_provider(
            BackendRequirements(env=["HF_TOKEN"], services=["pyannote.audio"])
        )
        == "local"
    )
    assert (
        classify_provider(
            BackendRequirements(hardware=["apple_silicon"], min_memory_gb=12.0)
        )
        == "local"
    )
    assert classify_provider(BackendRequirements()) == "unknown"


def test_model_param_fields_matches_model_named_fields() -> None:
    from media_engine.ops.video.comprehend import ComprehendParams

    fields = set(model_param_fields(ComprehendParams))
    assert {"vlm_model", "transcribe_model", "diarize_model", "synth_model"} <= fields
    # style / output_kind carry enums but are NOT models
    assert "style" not in fields
    assert "output_kind" not in fields


def test_vlm_model_now_carries_enum() -> None:
    from media_engine.ops.video.comprehend import ComprehendParams

    schema = ComprehendParams.model_json_schema()
    assert "enum" in schema["properties"]["vlm_model"]


def test_enrich_node_router_backend_and_provider() -> None:
    # frames.analyze routes by model prefix: gemini → cloud gemini backend.
    got = enrich_node("frames.analyze", {"prompt": "x", "model": "gemini-2.5-pro"}, None)
    assert got["resolved_backend"] == "gemini"
    assert got["provider"] == "cloud"
    assert any(m["name"] == "model" and m["provider"] == "cloud" for m in got["models"])

    # mlx model routes to the local vllm-mlx backend.
    got_local = enrich_node(
        "frames.analyze",
        {"prompt": "x", "model": "mlx-community/Qwen2-VL-2B-Instruct-4bit"},
        None,
    )
    assert got_local["resolved_backend"] == "vllm-mlx"
    assert got_local["provider"] == "local"


def test_enrich_node_composite_is_marked_composite() -> None:
    got = enrich_node("video.comprehend", {}, None)
    assert got["provider"] == "composite"
    # per-model providers carry the detail
    providers = {m["name"]: m["provider"] for m in got["models"]}
    assert providers["synth_model"] == "cloud"  # default gemini-2.5-pro
    assert providers["vlm_model"] == "local"  # default mlx qwen


def test_enrich_node_partial_params_does_not_raise() -> None:
    # frames.analyze requires `prompt`; omitting it must NOT raise (so the live
    # validator keeps returning 200 mid-edit). The model field still surfaces
    # its default; the backend falls back to the op's default_backend.
    got = enrich_node("frames.analyze", {}, None)
    assert any(m["name"] == "model" for m in got["models"])
    assert got["resolved_backend"] == "gemini"  # default_backend fallback


def test_profile_digest_aggregates_distinct_models() -> None:
    profile = PipelineProfile(
        name="t",
        graph=[
            GraphNodeSpec(
                id="a",
                op="frames.analyze",
                params={"prompt": "x", "model": "gemini-2.5-pro"},
            ),
            GraphNodeSpec(
                id="b",
                op="frames.compare",
                params={"model": "mlx-community/Qwen2-VL-2B-Instruct-4bit"},
            ),
        ],
    )
    digest = profile_digest(profile)
    by_name = {m["name"]: m["provider"] for m in digest["models"]}
    assert by_name["gemini-2.5-pro"] == "cloud"
    assert by_name["mlx-community/Qwen2-VL-2B-Instruct-4bit"] == "local"
