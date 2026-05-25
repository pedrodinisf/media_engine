"""Operation.delegates_to declarations on composite ops.

The Settings UI's Secrets impact computation walks delegates_to to
report "if you set GEMINI_API_KEY, intelligence.summarize lights up
too". This test pins the contract so a refactor that moves a composite
to a different sub-op doesn't silently shift the unblock counts the
operator sees.

Also a partial regression guard for B-009 (doctor reporting embedded
composites green when their delegates are unavailable) — the same
attribute is the substrate for a future doctor enhancement.
"""

from __future__ import annotations

from media_engine import bootstrap
from media_engine.ops import OpRegistry


def setup_module() -> None:
    bootstrap.register_all()


def test_operation_base_has_default_empty_delegates() -> None:
    """Non-composite ops inherit ``delegates_to = ()`` so introspection
    doesn't have to guard with hasattr() at every call site."""
    from media_engine.ops._base import Operation

    assert Operation.delegates_to == ()


def test_intelligence_composites_delegate_to_extract() -> None:
    for name in (
        "intelligence.summarize",
        "intelligence.classify",
        "intelligence.analyze",
    ):
        op = OpRegistry.get(name)
        assert op.delegates_to == ("intelligence.extract",), (
            f"{name} should delegate to intelligence.extract"
        )


def test_audio_transcribe_diarized_delegates_to_both() -> None:
    op = OpRegistry.get("audio.transcribe_diarized")
    assert set(op.delegates_to) == {"audio.transcribe", "audio.diarize"}


def test_search_hybrid_delegates_to_both() -> None:
    op = OpRegistry.get("search.hybrid")
    assert set(op.delegates_to) == {"search.semantic", "search.fulltext"}


def test_non_composite_ops_have_empty_delegates() -> None:
    """Ops with a Backend layer or pure passthrough composites should
    not declare delegates_to — the field is for ops that route through
    another *registered* op."""
    for name in (
        "audio.transcribe",
        "acquire.upload",
        "video.trim",
        "video.extract_audio",
        "frames.subsample",
        "transcript.parse",
    ):
        op = OpRegistry.get(name)
        assert op.delegates_to == (), (
            f"{name} should not declare delegates_to"
        )
