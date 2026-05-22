"""``speakers.*`` ops — resolve diarization clusters to human-readable names.

Phase 5 ships the name-CSV variant (``speakers.identify``); acoustic
identity (``speakers.embed_voice`` / ``cluster`` / ``match``) is Phase 7.
"""

from .identify import IdentifyParams, SpeakersIdentify

__all__ = ["IdentifyParams", "SpeakersIdentify"]
