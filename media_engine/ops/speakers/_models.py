"""Curated voice-embedding model ids surfaced as JSON Schema enums.

Same rationale as ``ops/audio/_models.py``: the ``model`` field stays a plain
``str`` (off-list HuggingFace ids still accepted) while
``Annotated[str, Field(json_schema_extra={"enum": ...})]`` gives the Web UI a
``<select>``. Edit the tuple when pyannote ships new embedding variants.
"""

from __future__ import annotations

# pyannote voice-embedding models. ``pyannote/embedding`` is the canonical
# x-vector-style speaker embedding shipped with pyannote.audio; ``wespeaker-*``
# mirrors are pulled in by pyannote 4.x for its own pipeline and work here too.
EMBED_VOICE_MODELS: tuple[str, ...] = (
    "pyannote/embedding",
    "pyannote/wespeaker-voxceleb-resnet34-LM",
)


__all__ = ["EMBED_VOICE_MODELS"]
