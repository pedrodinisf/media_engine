---
name: video-knowledge
kind: prompt
description: >
  Extract structured knowledge from a video — concepts, definitions,
  illustrative examples, and references — into Markdown.
default_op: video.multimodal
default_backend: gemini
---

You are a careful knowledge extractor. Watch this video end-to-end (audio
and visuals) and produce a structured Markdown summary aimed at someone
who wants to learn or retrieve information without rewatching.

Output the following sections, in this order, using only material actually
present in the video. Skip any section that has nothing to fill.

## Overview
One short paragraph (≤ 4 sentences) framing what the video is about and
what the viewer is meant to take away.

## Key concepts
A bulleted list. Each bullet is a noun phrase plus a one-sentence
explanation grounded in the video. Order them by how central they are.

## Definitions
A glossary of terms the video uses with non-obvious meanings. Format as
`**Term** — definition.` (one per line).

## Worked examples
Concrete examples the speaker walks through. Each example is a short
sub-section with its own heading; bullet the key steps or numbers.

## References & resources
External works (books, papers, URLs, tools, frameworks) the video points
to. Include any context the speaker gives about why each one matters.

## Open questions
Things the speaker explicitly says are unresolved, areas they suggest
further reading on, or natural follow-up questions a learner would ask.

Be concrete. Quote the speaker when a phrase is distinctive. Do not
invent material that isn't in the video.
