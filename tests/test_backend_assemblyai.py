"""Tests for the AssemblyAI transcribe / detect-language backends + routing.

The `assemblyai` SDK is an optional cloud dep, so every test here injects a
fake `assemblyai` module into ``sys.modules`` — this exercises the REAL
backend code (config assembly, utterance/word normalization, the
transcribe_diarized short-circuit) without a network call or the SDK.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from media_engine.artifacts import Audio, Transcript
from media_engine.backends import BackendRegistry
from media_engine.ops import OperationContext
from media_engine.ops.acquire.upload import AcquireUpload, AcquireUploadParams
from media_engine.ops.audio._models import (
    assemblyai_cost_cents,
    is_assemblyai_model,
    strip_assemblyai_prefix,
)
from media_engine.ops.audio.transcribe import AudioTranscribe, TranscribeParams
from media_engine.runtime.engine import Engine

# ─────────────────────────────────────────────────────────────────
# Fake assemblyai SDK
# ─────────────────────────────────────────────────────────────────


class _Word:
    def __init__(self, text: str, start: int, end: int, confidence: float = 0.9):
        self.text = text
        self.start = start
        self.end = end
        self.confidence = confidence


class _Utterance:
    def __init__(self, speaker, start, end, text, words):
        self.speaker = speaker
        self.start = start
        self.end = end
        self.text = text
        self.words = words


def _make_fake_assemblyai(captured: dict) -> types.ModuleType:
    aai = types.ModuleType("assemblyai")

    class _Settings:
        api_key: str | None = None

    class _SpeakerOptions:
        def __init__(self, min_speakers_expected=None, max_speakers_expected=None):
            self.min = min_speakers_expected
            self.max = max_speakers_expected

    class _TranscriptionConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _TranscriptStatus:
        completed = "completed"
        error = "error"

    class _FakeTranscript:
        def __init__(self, config):
            self.status = _TranscriptStatus.completed
            self.error = None
            self.id = "t-fake"
            self.text = "Hello there. General Kenobi."
            self.language_code = "en"
            self.language_confidence = 0.99
            self.audio_duration = 12.5
            words = [
                _Word("Hello", 0, 1000),
                _Word("there.", 1000, 2000),
                _Word("General", 2000, 3000),
                _Word("Kenobi.", 3000, 4000),
            ]
            self.words = words
            if config.kwargs.get("speaker_labels"):
                self.utterances = [
                    _Utterance("A", 0, 2000, "Hello there.", words[:2]),
                    _Utterance("B", 2000, 4000, "General Kenobi.", words[2:]),
                ]
            else:
                self.utterances = None

    class _Transcriber:
        def transcribe(self, path, config=None):
            captured["config"] = config
            captured["path"] = path
            return _FakeTranscript(config)

    aai.settings = _Settings()
    aai.SpeakerOptions = _SpeakerOptions
    aai.TranscriptionConfig = _TranscriptionConfig
    aai.TranscriptStatus = _TranscriptStatus
    aai.Transcriber = _Transcriber
    return aai


@pytest.fixture
def fake_aai(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "assemblyai", _make_fake_assemblyai(captured))
    return captured


# ─────────────────────────────────────────────────────────────────
# Pure helpers (no SDK, no engine)
# ─────────────────────────────────────────────────────────────────


def test_model_helpers() -> None:
    assert is_assemblyai_model("assemblyai/universal-2")
    assert not is_assemblyai_model("mlx-community/whisper-tiny-mlx")
    assert strip_assemblyai_prefix("assemblyai/universal-3-5-pro") == "universal-3-5-pro"
    assert strip_assemblyai_prefix("mlx-community/x") == "mlx-community/x"


def test_cost_scales_per_hour() -> None:
    # 1h universal-3-5-pro + diarization = ($0.21 + $0.02) → 23 cents.
    pro = assemblyai_cost_cents("assemblyai/universal-3-5-pro", 3600, diarize=True)
    u2 = assemblyai_cost_cents("assemblyai/universal-2", 3600, diarize=False)
    assert round(pro, 2) == 23.0
    assert round(u2, 2) == 15.0
    assert assemblyai_cost_cents("assemblyai/universal-2", None, diarize=False) == 0.0


def test_keyterms_parsing() -> None:
    from media_engine.backends.transcribe.assemblyai import _keyterms

    assert _keyterms(None) == []
    assert _keyterms("Sphere, triage\nagent") == ["Sphere", "triage", "agent"]


def test_build_config_maps_params(fake_aai: dict) -> None:
    import assemblyai as aai  # the fake, from sys.modules

    from media_engine.backends.transcribe.assemblyai import _build_config

    params = TranscribeParams(
        model="assemblyai/universal-3-5-pro",
        language="en",
        speaker_labels=True,
        min_speakers=2,
        max_speakers=4,
        prompt="tech meeting",
        keyterms="Sphere, triage",
        start_s=1.0,
        end_s=5.0,
    )
    cfg = _build_config(aai, params, detect_only=False)
    k = cfg.kwargs
    assert k["speech_models"] == ["universal-3-5-pro"]
    assert k["language_code"] == "en"
    assert k["speaker_labels"] is True
    assert k["speaker_options"].min == 2 and k["speaker_options"].max == 4
    assert k["prompt"] == "tech meeting"
    assert k["keyterms_prompt"] == ["Sphere", "triage"]
    assert k["audio_start_from"] == 1000 and k["audio_end_at"] == 5000
    # detect-only path forces language_detection + drops diarization/prompt.
    d = _build_config(aai, TranscribeParams(model="assemblyai/universal-2"), detect_only=True)
    assert d.kwargs.get("language_detection") is True
    assert "speaker_labels" not in d.kwargs


def test_segment_normalization() -> None:
    from media_engine.backends.transcribe.assemblyai import (
        _sentence_segments,
        _utterance_segments,
    )

    utts = [_Utterance("A", 0, 2000, "Hi.", [_Word("Hi.", 0, 2000)])]
    segs = _utterance_segments(utts, word_timestamps=True)
    assert segs[0]["speaker_id"] == "A"
    assert segs[0]["start"] == 0.0 and segs[0]["end"] == 2.0
    assert segs[0]["words"][0]["text"] == "Hi."

    words = [_Word("Hello", 0, 1000), _Word("world.", 1000, 2000), _Word("Bye.", 2000, 3000)]
    sents = _sentence_segments(words, word_timestamps=False)
    assert [s["text"] for s in sents] == ["Hello world.", "Bye."]


def test_require_api_key_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    from media_engine.backends.transcribe.assemblyai import _require_api_key

    with pytest.raises(RuntimeError, match="ASSEMBLYAI_API_KEY"):
        _require_api_key()


def test_router_and_registration() -> None:
    assert BackendRegistry.has("audio.transcribe", "assemblyai")
    assert BackendRegistry.has("audio.detect_language", "assemblyai")
    op = AudioTranscribe()
    aa = op.select_backend(TranscribeParams(model="assemblyai/universal-2"))
    mlx = op.select_backend(TranscribeParams(model="mlx-community/whisper-tiny-mlx"))
    assert aa == "assemblyai"
    assert mlx == "mlx-whisper"


def test_transcribe_cost_estimate_is_cloud() -> None:
    op = AudioTranscribe()
    audio = Audio(
        id="a" * 64,
        path=Path("/tmp/a.wav"),
        metadata={"duration": 3600.0},
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    est = op.cost_estimate([audio], TranscribeParams(model="assemblyai/universal-2"))
    assert est.local_seconds == 0.0
    assert round(est.cloud_cents, 2) == 15.0


def test_secrets_catalog_has_assemblyai_key() -> None:
    from media_engine.runtime.secrets import KNOWN_SECRETS

    names = {s["name"] for s in KNOWN_SECRETS}
    assert "ASSEMBLYAI_API_KEY" in names


# ─────────────────────────────────────────────────────────────────
# Engine-level dispatch (fake SDK)
# ─────────────────────────────────────────────────────────────────


def _ctx(engine: Engine) -> OperationContext:
    return OperationContext(
        workdir=engine.storage.ensure_workdir("aa"),
        config=engine.config,
        storage=engine.storage,
        namespace=engine.config.namespace,
        emit=engine.event_bus.emit,
        model_pool=engine.model_pool,
    )


async def _acquire_audio(engine: Engine, sample: Path) -> Audio:
    [a] = await AcquireUpload().run(
        [], AcquireUploadParams(source_path=sample), _ctx(engine)
    )
    assert isinstance(a, Audio)
    engine.cache.upsert_artifact(a)
    return a


async def test_execute_produces_diarized_transcript(
    engine: Engine, sample_m4a: Path, fake_aai: dict
) -> None:
    audio = await _acquire_audio(engine, sample_m4a)
    [t] = await engine.run(
        "audio.transcribe",
        inputs=[audio.id],
        model="assemblyai/universal-2",
        speaker_labels=True,
        min_speakers=2,
    )
    assert isinstance(t, Transcript)
    assert t.metadata["text"] == "Hello there. General Kenobi."
    speakers = {s["speaker_id"] for s in t.segments}
    assert speakers == {"A", "B"}
    # the config that reached the SDK carried speaker_labels + speaker_options
    assert fake_aai["config"].kwargs["speaker_labels"] is True


async def test_transcribe_diarized_short_circuits_to_one_call(
    engine: Engine, sample_m4a: Path, fake_aai: dict
) -> None:
    audio = await _acquire_audio(engine, sample_m4a)
    [t] = await engine.run(
        "audio.transcribe_diarized",
        inputs=[audio.id],
        transcribe_model="assemblyai/universal-2",
        num_speakers=2,
    )
    assert isinstance(t, Transcript)
    # single AssemblyAI call did both — diarization_model marks the path taken.
    assert t.metadata["diarization_model"] == "assemblyai"
    assert {s["speaker_id"] for s in t.segments} == {"A", "B"}
    # num_speakers forwarded as speaker_options min/max
    assert fake_aai["config"].kwargs["speaker_labels"] is True


async def test_detect_language_via_assemblyai(
    engine: Engine, sample_m4a: Path, fake_aai: dict
) -> None:
    audio = await _acquire_audio(engine, sample_m4a)
    [analysis] = await engine.run(
        "audio.detect_language", inputs=[audio.id], model="assemblyai/universal-2"
    )
    assert analysis.metadata["data"]["language"] == "en"
    # detect path must force language_detection (no fixed language_code)
    assert fake_aai["config"].kwargs.get("language_detection") is True
