"""Single place that registers every op + backend.

Every transport (CLI, daemon entry, MCP server, tests) calls
``register_all()`` at startup. Without it a process only knows about the
op/backend modules it happened to import — e.g. ``med ops`` would show
two ops instead of the full catalog.

Registration is **explicit and idempotent** rather than import-side-effect
driven: ``@register_op`` / ``@register_backend`` decorators only run once
per process (Python caches module imports), so a test that clears a
registry could never get the catalog back via re-import alone. Calling
``register_all(force=True)`` re-asserts every class into the registries
(``register`` is a no-op when the same class is already present).

Optional-dep backends (pyannote, sentence-transformers, pyscenedetect)
are registered only when their module imports cleanly; the ML dependency
itself is only needed at ``execute()`` time, not registration time.
"""

from __future__ import annotations

from media_engine.backends import BackendRegistry
from media_engine.ops import OpRegistry

_done = False


def _op_classes() -> list[type]:
    from media_engine.ops.acquire.livestream import AcquireLivestream
    from media_engine.ops.acquire.upload import AcquireUpload
    from media_engine.ops.acquire.url import AcquireURL
    from media_engine.ops.audio.detect_language import AudioDetectLanguage
    from media_engine.ops.audio.diarize import AudioDiarize
    from media_engine.ops.audio.transcribe import AudioTranscribe
    from media_engine.ops.audio.transcribe_diarized import (
        AudioTranscribeDiarized,
    )
    from media_engine.ops.chunk.semantic import ChunkSemantic
    from media_engine.ops.document.parse import DocumentParse
    from media_engine.ops.embed.text import EmbedText
    from media_engine.ops.frames.analyze import FramesAnalyze
    from media_engine.ops.frames.compare import FramesCompare
    from media_engine.ops.frames.subsample import FramesSubsample
    from media_engine.ops.image.classify import ImageClassify
    from media_engine.ops.image.describe import ImageDescribe
    from media_engine.ops.image.ocr import ImageOCR
    from media_engine.ops.intelligence.analyze import IntelligenceAnalyze
    from media_engine.ops.intelligence.classify import IntelligenceClassify
    from media_engine.ops.intelligence.extract import IntelligenceExtract
    from media_engine.ops.intelligence.summarize import IntelligenceSummarize
    from media_engine.ops.metadata.scrape_page import MetadataScrapePage
    from media_engine.ops.report.session import ReportSession
    from media_engine.ops.report.zeitgeist import ReportZeitgeist
    from media_engine.ops.search.fulltext import SearchFulltext
    from media_engine.ops.search.hybrid import SearchHybrid
    from media_engine.ops.search.semantic import SearchSemantic
    from media_engine.ops.speakers.embed_voice import SpeakersEmbedVoice
    from media_engine.ops.speakers.identify import SpeakersIdentify
    from media_engine.ops.transcript.merge import TranscriptMerge
    from media_engine.ops.transcript.parse import TranscriptParse
    from media_engine.ops.video.comprehend import VideoComprehend
    from media_engine.ops.video.extract_audio import VideoExtractAudio
    from media_engine.ops.video.multimodal import VideoMultimodal
    from media_engine.ops.video.sample_frames import VideoSampleFrames
    from media_engine.ops.video.trim import VideoTrim
    from media_engine.ops.web.fetch import WebFetch

    return [
        AcquireLivestream,
        AcquireUpload,
        AcquireURL,
        AudioDetectLanguage,
        AudioDiarize,
        AudioTranscribe,
        AudioTranscribeDiarized,
        ChunkSemantic,
        DocumentParse,
        EmbedText,
        FramesAnalyze,
        FramesCompare,
        FramesSubsample,
        ImageClassify,
        ImageDescribe,
        ImageOCR,
        IntelligenceAnalyze,
        IntelligenceClassify,
        IntelligenceExtract,
        IntelligenceSummarize,
        MetadataScrapePage,
        ReportSession,
        ReportZeitgeist,
        SearchFulltext,
        SearchHybrid,
        SearchSemantic,
        SpeakersEmbedVoice,
        SpeakersIdentify,
        TranscriptMerge,
        TranscriptParse,
        VideoComprehend,
        VideoExtractAudio,
        VideoMultimodal,
        VideoSampleFrames,
        VideoTrim,
        WebFetch,
    ]


def _backend_classes() -> list[type]:
    from media_engine.backends.acquire.ffmpeg_recorder import (
        FfmpegRecorderBackend,
    )
    from media_engine.backends.acquire.ytdlp import YtdlpAcquireBackend
    from media_engine.backends.chunk_semantic.default import (
        DefaultChunkSemanticBackend,
    )
    from media_engine.backends.frames_analyze.vllm_mlx import (
        VllmMlxFramesAnalyzeBackend,
    )
    from media_engine.backends.sample_frames.ffmpeg_uniform import (
        FfmpegUniformBackend,
    )
    from media_engine.backends.search.sqlite import SqliteSemanticBackend
    from media_engine.backends.search.sqlite_fts5 import SqliteFts5Backend
    from media_engine.backends.transcribe.mlx_whisper import (
        MlxWhisperDetectLanguageBackend,
        MlxWhisperTranscribeBackend,
    )
    from media_engine.backends.video_multimodal.vllm_mlx import (
        VllmMlxVideoMultimodalBackend,
    )
    from media_engine.backends.web.httpx import HttpxWebFetchBackend

    classes: list[type] = [
        YtdlpAcquireBackend,
        FfmpegRecorderBackend,
        HttpxWebFetchBackend,
        SqliteSemanticBackend,
        SqliteFts5Backend,
        MlxWhisperTranscribeBackend,
        MlxWhisperDetectLanguageBackend,
        FfmpegUniformBackend,
        DefaultChunkSemanticBackend,
        VllmMlxVideoMultimodalBackend,
        VllmMlxFramesAnalyzeBackend,
    ]

    # Optional-dep backends: import-clean even when the ML lib is absent,
    # so register them too — the dep is only needed at execute() time.
    try:
        from media_engine.backends.acquire.playwright_hls import (
            PlaywrightHlsAcquireBackend,
        )
        classes.append(PlaywrightHlsAcquireBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.document.pymupdf import PyMuPdfBackend
        classes.append(PyMuPdfBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.web.playwright import (
            PlaywrightWebFetchBackend,
        )
        classes.append(PlaywrightWebFetchBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.diarize.pyannote import (
            PyannoteDiarizeBackend,
        )
        classes.append(PyannoteDiarizeBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.embed_voice.pyannote import (
            PyannoteEmbedVoiceBackend,
        )
        classes.append(PyannoteEmbedVoiceBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.embed_text.sentence_transformers import (
            SentenceTransformersEmbedTextBackend,
        )
        classes.append(SentenceTransformersEmbedTextBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.sample_frames.pyscenedetect import (
            PySceneDetectBackend,
        )
        classes.append(PySceneDetectBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.video_multimodal.gemini import (
            GeminiVideoMultimodalBackend,
        )
        classes.append(GeminiVideoMultimodalBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.frames_analyze.gemini import (
            GeminiFramesAnalyzeBackend,
        )
        classes.append(GeminiFramesAnalyzeBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.frames_compare.gemini import (
            GeminiFramesCompareBackend,
        )
        classes.append(GeminiFramesCompareBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.image_describe.gemini import (
            GeminiImageDescribeBackend,
        )
        classes.append(GeminiImageDescribeBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.image_ocr.rapidocr import RapidOCRBackend
        classes.append(RapidOCRBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.image_ocr.gemini_vision import (
            GeminiVisionOCRBackend,
        )
        classes.append(GeminiVisionOCRBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.image_classify.open_clip import (
            OpenClipClassifyBackend,
        )
        classes.append(OpenClipClassifyBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.image_classify.gemini import (
            GeminiClassifyBackend,
        )
        classes.append(GeminiClassifyBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.intelligence_extract.gemini import (
            GeminiExtractBackend,
        )
        classes.append(GeminiExtractBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.intelligence_extract.claude import (
            ClaudeExtractBackend,
        )
        classes.append(ClaudeExtractBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.intelligence_extract.mlx_lm import (
            MlxLmExtractBackend,
        )
        classes.append(MlxLmExtractBackend)
    except ImportError:
        pass

    # Postgres-backed search backends (Phase 4 commit 31). Import-clean:
    # the modules themselves only need ``psycopg`` at execute-time. We
    # still gate by ImportError so deployments without psycopg installed
    # at all don't see the import fail.
    try:
        from media_engine.backends.search.pgvector import (
            PgVectorSemanticBackend,
        )
        classes.append(PgVectorSemanticBackend)
    except ImportError:
        pass
    try:
        from media_engine.backends.search.postgres_tsvector import (
            PostgresTsvectorBackend,
        )
        classes.append(PostgresTsvectorBackend)
    except ImportError:
        pass

    return classes


def register_all(*, force: bool = False) -> None:
    """Populate the op + backend registries with the full catalog.

    Idempotent. ``force=True`` re-asserts every class (used by the test
    suite's autouse fixture after registry-clearing tests)."""
    global _done
    if _done and not force:
        return

    for op_class in _op_classes():
        if not OpRegistry.has(op_class.name):  # type: ignore[attr-defined]
            OpRegistry.register(op_class)  # type: ignore[arg-type]

    for backend_class in _backend_classes():
        if not BackendRegistry.has(
            backend_class.op_name,  # type: ignore[attr-defined]
            backend_class.name,  # type: ignore[attr-defined]
        ):
            BackendRegistry.register(backend_class)  # type: ignore[arg-type]

    _done = True
