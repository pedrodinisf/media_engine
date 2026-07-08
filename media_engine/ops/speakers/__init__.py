"""``speakers.*`` ops — name-based and acoustic speaker identity.

Phase 5 ships the name-CSV variant (``speakers.identify``). Phase 7 adds the
acoustic path: ``speakers.embed_voice`` (voice fingerprints per diarization
turn), ``speakers.cluster`` (stable cross-recording ids), and
``speakers.match`` (cosine lookup vs a fingerprint DB).
"""

from .embed_voice import EmbedVoiceParams, SpeakersEmbedVoice
from .identify import IdentifyParams, SpeakersIdentify

__all__ = [
    "EmbedVoiceParams",
    "IdentifyParams",
    "SpeakersEmbedVoice",
    "SpeakersIdentify",
]
