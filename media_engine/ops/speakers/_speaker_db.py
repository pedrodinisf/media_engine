"""Pure-function helpers for ``speakers.identify``.

Three small steps, each unit-testable in isolation:
  * ``load_speaker_db`` — read the CSV into typed entries (canonical name,
    candidate strings = canonical + aliases, pass-through extra columns).
  * ``text_per_cluster`` — collect each diarization cluster's first
    ``intro_window_seconds`` of speech text. That's the window the speaker
    is most likely to introduce themselves in.
  * ``identify_speakers`` — rapidfuzz ``partial_ratio`` over the candidate
    list per cluster; require ``best_score >= min_confidence * 100`` *and*
    the next-best entry to trail by at least ``tie_band`` points. Ties
    return ``None`` (ambiguous → no confident match).

The op (``speakers.identify``) glues these together and emits a Transcript
with each segment carrying a resolved ``speaker_name``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process


def _empty_extra() -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class SpeakerEntry:
    """One row of the speaker database after candidate-string flattening."""

    canonical: str
    candidates: tuple[str, ...]  # canonical + aliases (used for fuzzy match)
    extra: dict[str, str] = field(default_factory=_empty_extra)


@dataclass(frozen=True)
class MatchResult:
    canonical: str
    score: float            # 0..100 (rapidfuzz partial_ratio)
    matched_candidate: str  # which exact alias / canonical scored
    runner_up_score: float


def load_speaker_db(
    path: Path,
    *,
    name_field: str = "name",
    alias_field: str = "aliases",
) -> list[SpeakerEntry]:
    """Read a CSV into a list of ``SpeakerEntry``.

    The CSV must have at least one column named ``name_field``. The
    ``alias_field`` column is optional; when present, aliases are
    comma-separated inside the cell (use quoting for embedded commas).
    Any other columns are passed through to ``entry.extra`` verbatim —
    reports can render them next to the resolved name.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"speaker_db not found: {p}")
    entries: list[SpeakerEntry] = []
    with p.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or name_field not in reader.fieldnames:
            raise ValueError(
                f"speaker_db {p} missing required column {name_field!r}; "
                f"got columns: {reader.fieldnames}"
            )
        has_aliases = alias_field in reader.fieldnames
        for row in reader:
            canonical = (row.get(name_field) or "").strip()
            if not canonical:
                continue
            aliases: list[str] = []
            if has_aliases:
                raw = row.get(alias_field) or ""
                aliases = [a.strip() for a in raw.split(",") if a.strip()]
            candidates = (canonical, *aliases)
            extra = {
                k: (v or "")
                for k, v in row.items()
                if k not in {name_field, alias_field}
            }
            entries.append(
                SpeakerEntry(
                    canonical=canonical, candidates=candidates, extra=extra
                )
            )
    return entries


def text_per_cluster(
    segments: list[dict[str, Any]],
    *,
    intro_window_seconds: float = 30.0,
) -> dict[str, str]:
    """Collect each ``speaker_id`` cluster's first ``intro_window_seconds``
    of speech text. Returns ``{cluster_id: text}``.

    The introductory window is where speakers typically state their own
    name (and the moderator's). If a cluster's first contiguous run of
    segments doesn't reach the window length, we still emit whatever
    text we have. ``UNKNOWN`` / empty cluster ids are skipped.
    """
    by_cluster: dict[str, list[dict[str, Any]]] = {}
    for s in segments:
        cid = str(s.get("speaker_id") or "")
        if not cid or cid == "UNKNOWN":
            continue
        by_cluster.setdefault(cid, []).append(s)

    out: dict[str, str] = {}
    for cid, segs in by_cluster.items():
        segs_sorted = sorted(
            segs, key=lambda s: float(s.get("start") or 0.0)
        )
        first_start = float(segs_sorted[0].get("start") or 0.0)
        cutoff = first_start + intro_window_seconds
        chunks: list[str] = []
        for s in segs_sorted:
            t = str(s.get("text") or "")
            if t:
                chunks.append(t)
            end = s.get("end")
            if end is not None and float(end) >= cutoff:
                break
        out[cid] = " ".join(chunks).strip()
    return out


def identify_speakers(
    cluster_texts: dict[str, str],
    db: list[SpeakerEntry],
    *,
    min_confidence: float = 0.7,
    tie_band: float = 5.0,
) -> dict[str, MatchResult | None]:
    """Per cluster, return the highest-scoring ``MatchResult`` if it clears
    ``min_confidence`` *and* beats the next-best different-canonical
    candidate by at least ``tie_band`` points. Otherwise ``None``.
    """
    out: dict[str, MatchResult | None] = {}
    if not db:
        return {cid: None for cid in cluster_texts}

    candidates: list[str] = []
    entry_for: list[SpeakerEntry] = []
    for entry in db:
        for cand in entry.candidates:
            candidates.append(cand)
            entry_for.append(entry)

    for cid, text in cluster_texts.items():
        if not text:
            out[cid] = None
            continue
        # Pull more than 2 matches so we can skip same-canonical aliases
        # when picking the runner-up for tie-band detection.
        results = process.extract(
            text,
            candidates,
            scorer=fuzz.partial_ratio,
            limit=min(8, len(candidates)),
        )
        if not results:
            out[cid] = None
            continue
        best = results[0]
        best_cand, best_score, best_idx = (
            str(best[0]), float(best[1]), int(best[2])
        )
        best_canon = entry_for[best_idx].canonical

        runner_up_score = 0.0
        ambiguous = False
        for r in results[1:]:
            r_score, r_idx = float(r[1]), int(r[2])
            if entry_for[r_idx].canonical == best_canon:
                # Another alias for the same person — not an ambiguity.
                continue
            runner_up_score = r_score
            if best_score - r_score < tie_band:
                ambiguous = True
            break

        if best_score / 100.0 < min_confidence or ambiguous:
            out[cid] = None
        else:
            out[cid] = MatchResult(
                canonical=best_canon,
                score=best_score,
                matched_candidate=best_cand,
                runner_up_score=runner_up_score,
            )
    return out


__all__ = [
    "MatchResult",
    "SpeakerEntry",
    "identify_speakers",
    "load_speaker_db",
    "text_per_cluster",
]
