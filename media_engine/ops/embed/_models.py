"""Curated local embedding model ids, surfaced as a JSON Schema enum.

Same pattern as the sibling ``_models.py`` files: the ``model`` field stays
typed ``str`` (off-list ids still accepted) but ``json_schema_extra`` injects
this set so the Web UI renders a ``<select>``. All entries are
``sentence-transformers/*`` — local, no API key — so the client's
``classifyModelProvider`` tags them "local".

Caveat surfaced in the field description, not enforced here: switching the
embedding model changes the vector dimensionality, so an existing semantic
index built with a different model won't be comparable — re-embed after a
change.
"""

from __future__ import annotations

# Local sentence-transformers models. Order = smallest/fastest → highest
# quality. all-MiniLM-L6-v2 (384-dim) is the default; mpnet (768-dim) trades
# speed for retrieval quality; the multilingual variant covers non-English.
EMBED_TEXT_MODELS: tuple[str, ...] = (
    "sentence-transformers/all-MiniLM-L6-v2",
    "sentence-transformers/all-MiniLM-L12-v2",
    "sentence-transformers/all-mpnet-base-v2",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)


__all__ = ["EMBED_TEXT_MODELS"]
