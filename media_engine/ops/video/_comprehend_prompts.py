"""System prompts + default schema for `video.comprehend`.

Lives next to the op so the prompt strings travel with the code that
uses them. The op picks the entry by `style:` param.
"""

from __future__ import annotations

from typing import Any

# ── System prompts ────────────────────────────────────────────────────────

_GENERAL = (
    "You are a careful video analyst. You are given a time-sorted timeline "
    "that interleaves frame descriptions (visual) with diarized transcript "
    "lines (audio). Synthesize the two modalities — never describe one in "
    "isolation. Be specific about what is shown vs. said, who is speaking, "
    "and the chronology. Stay grounded in the timeline; do not invent "
    "details that aren't present."
)

_EXPLAINER = (
    "You are reviewing an explainer or tutorial video. Identify the topic "
    "being explained, the audience it assumes, the visual aids used "
    "(diagrams, code, demos), and the order in which concepts are "
    "introduced. Each section should map cleanly to one explained "
    "concept. Quote the speaker for any key terminology."
)

_LECTURE = (
    "You are reviewing an academic lecture. Identify the field, the "
    "thesis, the supporting points, and the citations / references the "
    "lecturer makes. Sections should track the lecture's outline rather "
    "than minute boundaries. Capture any whiteboard / slide content that "
    "the frame descriptions surface."
)

_INTERVIEW = (
    "You are reviewing an interview. Identify the interviewer and "
    "interviewee(s), the line of questioning, and the substantive "
    "answers. Each section should align with a topic of questioning. "
    "Speaker roles in the `speakers` array matter — say who is the "
    "interviewer vs. the subject."
)

_TUTORIAL = (
    "You are reviewing a how-to / tutorial video. Identify the goal, the "
    "prerequisites, and the ordered steps. Each section should map to "
    "one step in the procedure; `key_visuals` should capture the "
    "specific UI elements, commands, or outputs shown."
)

_MEETING = (
    "You are reviewing a recorded business meeting (Teams / Zoom / Meet). "
    "The audio captures multiple participants discussing topics; the "
    "frame descriptions capture both faces-on-camera moments and "
    "screen-share content (slides, code, dashboards, documents). "
    "Synthesize both — when the screen-share shows context (a slide, a "
    "doc, a chart), tie it to what was being discussed at that "
    "timestamp.\n\n"
    "Extract aggressively into the structured fields:\n"
    "  * `decisions[]` — every concrete agreement or choice the group "
    "made (or rejected). Include the timestamp.\n"
    "  * `action_items[]` — every assignment, follow-up, or task the "
    "group committed to. Capture WHO owns it (use the speaker_id when "
    "the assignee isn't named), WHAT they're doing, and WHEN (if a "
    "due date was mentioned). If ownership is ambiguous, set "
    "`owner_speaker_id` and leave `owner_name` null.\n"
    "  * `key_moments[]` — significant pivots, disagreements, "
    "questions that changed direction, important screen-share "
    "content.\n"
    "  * `topics[]` — the agenda items as they came up.\n\n"
    "Sections should chapter the meeting by topic, not by minute. "
    "Be terse — operators read this to catch up on a meeting they "
    "missed, not to relive it."
)

SYSTEM_PROMPTS: dict[str, str] = {
    "general": _GENERAL,
    "explainer": _EXPLAINER,
    "lecture": _LECTURE,
    "interview": _INTERVIEW,
    "tutorial": _TUTORIAL,
    "meeting": _MEETING,
}

# ── User prompts (paired with each style) ─────────────────────────────────

_USER_BASE = (
    "Below is a time-sorted multimodal timeline of a video. "
    "Lines beginning `[t=MM:SS.s] FRAME:` are descriptions of one "
    "sampled frame at that timestamp. Lines beginning "
    "`[t=MM:SS.s] <speaker_id>:` are transcript turns from a speaker "
    "diarization run.\n\n"
    "Produce a single structured analysis covering the whole video. "
    "Anchor every claim in the timeline content."
)

USER_PROMPTS: dict[str, str] = {
    "general": _USER_BASE,
    "explainer": _USER_BASE + " Treat this as an explainer/tutorial video.",
    "lecture": _USER_BASE + " Treat this as an academic lecture.",
    "interview": _USER_BASE + " Treat this as an interview.",
    "tutorial": _USER_BASE + " Treat this as a how-to tutorial.",
    "meeting": (
        _USER_BASE
        + " Treat this as a recorded business meeting. Populate "
        "`decisions[]` and `action_items[]` aggressively — they are "
        "the primary deliverable. Treat anonymous speaker_ids as "
        "owners when the assignee isn't named explicitly."
    ),
}

# ── Default structured-output schema ──────────────────────────────────────

DEFAULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "video_type": {"type": "string"},
        "duration_seconds": {"type": "number"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "speakers": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "key_visuals": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "start_seconds",
                    "end_seconds",
                    "title",
                    "summary",
                ],
            },
        },
        "topics": {"type": "array", "items": {"type": "string"}},
        "speakers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "speaking_time_seconds": {"type": "number"},
                    "role": {"type": "string"},
                },
                "required": ["id", "speaking_time_seconds"],
            },
        },
        "key_moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "t_seconds": {"type": "number"},
                    "description": {"type": "string"},
                    "importance": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                },
                "required": ["t_seconds", "description"],
            },
        },
        # Optional — populated heavily for style="meeting", emitted as
        # empty arrays (or omitted) for the other styles. The synth
        # model fills them when the timeline content supports it.
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "t_seconds": {"type": "number"},
                    "decision": {"type": "string"},
                    # Which speaker_ids supported / pushed for it (helps
                    # downstream tooling attribute decisions).
                    "speaker_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "context": {"type": "string"},
                },
                "required": ["t_seconds", "decision"],
            },
        },
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "t_seconds": {"type": "number"},
                    "task": {"type": "string"},
                    # owner_name when the assignee was addressed by name
                    # ("Alice, can you..."); owner_speaker_id as fallback
                    # for anonymous diarized labels.
                    "owner_name": {"type": "string"},
                    "owner_speaker_id": {"type": "string"},
                    "due": {"type": "string"},
                },
                "required": ["t_seconds", "task"],
            },
        },
    },
    "required": [
        "title",
        "summary",
        "video_type",
        "sections",
        "topics",
        "speakers",
    ],
}


__all__ = ["DEFAULT_SCHEMA", "SYSTEM_PROMPTS", "USER_PROMPTS"]
