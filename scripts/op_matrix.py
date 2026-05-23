"""Runtime op matrix — walk every registered op, try to execute it.

Companion to ``med doctor``. Doctor surfaces the *declared* dep
contract; this script attempts the *real* execution path through
``Engine.run`` against synthetic fixture artifacts and classifies each
op as:

    ✓  ran successfully (real result or cache hit)
    ⊘  skipped (doctor reports unavailable, or we can't synthesize
       its required params — e.g. live URL, prompt-only LLM)
    ✗  failed (an exception escaped Engine.run)

Output: ``tests/e2e_op_matrix_report.md``. Operator-invoked
(``uv run python scripts/op_matrix.py``); not part of the pytest gate.
Idempotent — boots a temp permanent_store + namespace and tears it
down on exit so the operator's real artifacts are never touched.

Usage:
    uv run python scripts/op_matrix.py [--keep-store] [--filter audio.]

When a ✓ op flips to ✗ between runs, that's a regression worth
investigating.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # so the script works from anywhere

from media_engine.artifacts import Kind  # noqa: E402
from media_engine.artifacts.analysis import (  # noqa: E402
    Analysis,
    Embedding,
    SessionAnalysis,
)
from media_engine.artifacts.media import Audio, FrameSet, Image, Video  # noqa: E402
from media_engine.artifacts.text import (  # noqa: E402
    Chunks,
    Diarization,
    Document,
    MarkdownArtifact,
    Transcript,
    WebPage,
)
from media_engine.bootstrap import register_all  # noqa: E402
from media_engine.config import EngineConfig  # noqa: E402
from media_engine.ops import Operation, OpRegistry  # noqa: E402
from media_engine.runtime.cache import Cache  # noqa: E402
from media_engine.runtime.doctor import diagnose  # noqa: E402
from media_engine.runtime.engine import Engine  # noqa: E402

Status = Literal["ok", "skipped", "failed"]


@dataclass
class OpResult:
    op_name: str
    op_version: str
    status: Status
    reason: str = ""
    elapsed_ms: float = 0.0
    output_ids: list[str] = field(default_factory=lambda: [])  # noqa: PIE807


@dataclass
class MatrixReport:
    started_at: str
    finished_at: str
    summary: dict[str, int]
    results: list[OpResult]


# ─────────────────────────────────────────────────────────────────
# Fixture seeding — one artifact per Kind, stored under namespace.
# ─────────────────────────────────────────────────────────────────


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def seed_fixtures(cache: Cache, store_root: Path, namespace: str) -> dict[Kind, str]:
    """Seed one artifact per Kind into the cache. Returns ``{kind → id}``.

    Uses the vendored ``tests/fixtures/`` media where applicable and falls
    back to synthetic stubs (minimal valid bytes) for kinds without a
    vendored sample. Every artifact is namespace-scoped so it doesn't
    leak into the operator's real namespace.
    """
    fx_dir = REPO_ROOT / "tests" / "fixtures"
    artifacts_root = store_root / "artifacts"

    def now() -> datetime:
        return datetime.now(UTC)
    seeded: dict[Kind, str] = {}

    def _seed(
        artifact_cls: type[Any],
        kind: Kind,
        sha_seed: str,
        source: Path | None,
        ext: str,
        metadata: dict[str, Any],
    ) -> None:
        # Deterministic synthetic id keyed by the kind so reruns are stable.
        art_id = (sha_seed * 64)[:64]
        path = artifacts_root / art_id[:2] / f"{art_id}.{ext}"
        data = (
            source.read_bytes()
            if source is not None and source.exists()
            else b"synthetic-stub"
        )
        _write_file(path, data)
        a = artifact_cls(
            id=art_id,
            path=path,
            derived_from=(),
            produced_by=None,
            namespace=namespace,
            created_at=now(),
            metadata=metadata,
        )
        cache.upsert_artifact(a)
        seeded[kind] = art_id

    # Video — vendored mp4
    _seed(
        Video,
        Kind.Video,
        "a",
        fx_dir / "sample.mp4",
        "mp4",
        {
            "duration": 5.0,
            "codec": "h264",
            "fps": 30.0,
            "width": 320,
            "height": 240,
            "source": "fixture://sample.mp4",
        },
    )

    # Audio — vendored wav
    _seed(
        Audio,
        Kind.Audio,
        "b",
        fx_dir / "sample_speech.wav",
        "wav",
        {
            "duration": 3.0,
            "sample_rate": 16000,
            "channels": 1,
            "codec": "pcm_s16le",
        },
    )

    # Image — vendored png
    _seed(
        Image,
        Kind.Image,
        "c",
        fx_dir / "sample.png",
        "png",
        {"width": 64, "height": 64, "format": "png"},
    )

    # FrameSet — synthetic; ships its frame_ids in metadata (pointing
    # at the Image we just seeded so frames.* can resolve them).
    image_id = seeded[Kind.Image]
    _seed(
        FrameSet,
        Kind.FrameSet,
        "d",
        None,
        "json",
        {
            "frame_ids": [image_id],
            "fps": 1.0,
            "frame_paths": [str(artifacts_root / image_id[:2] / f"{image_id}.png")],
        },
    )

    # Transcript — synthetic; one short segment.
    transcript_segments = [
        {
            "start": 0.0,
            "end": 1.5,
            "text": "hello world this is a transcript fixture",
            "speaker_id": "Speaker_0001",
        },
        {
            "start": 1.5,
            "end": 3.0,
            "text": "the second segment continues the conversation",
            "speaker_id": "Speaker_0001",
        },
    ]
    _seed(
        Transcript,
        Kind.Transcript,
        "e",
        None,
        "json",
        {
            "segments": transcript_segments,
            "language": "en",
            "text": " ".join(s["text"] for s in transcript_segments),
        },
    )

    # Diarization
    _seed(
        Diarization,
        Kind.Diarization,
        "f",
        None,
        "json",
        {
            "segments": [
                {"start": s["start"], "end": s["end"], "speaker_id": s["speaker_id"]}
                for s in transcript_segments
            ],
            "num_speakers": 1,
        },
    )

    # OCRText
    from media_engine.artifacts.text import OCRText
    _seed(
        OCRText,
        Kind.OCRText,
        "1",
        None,
        "txt",
        {"text": "fixture OCR text", "boxes": []},
    )

    # Chunks
    _seed(
        Chunks,
        Kind.Chunks,
        "2",
        None,
        "json",
        {
            "chunks": [
                {"text": "first chunk content", "start": 0, "end": 19},
                {"text": "second chunk content", "start": 19, "end": 39},
            ]
        },
    )

    # Markdown
    _seed(
        MarkdownArtifact,
        Kind.MarkdownArtifact,
        "3",
        None,
        "md",
        {"text": "# Heading\n\nA short markdown fixture for op matrix runs.\n"},
    )

    # Document (pdf-ish; vendored tiny.pdf if available)
    _seed(
        Document,
        Kind.Document,
        "4",
        fx_dir / "tiny.pdf",
        "pdf",
        {"text": "fixture document body text", "page_count": 1},
    )

    # WebPage
    _seed(
        WebPage,
        Kind.WebPage,
        "5",
        None,
        "html",
        {
            "url": "https://example.com/fixture",
            "title": "Fixture",
            "text": "fixture webpage text body",
            "html": "<html><body>fixture</body></html>",
        },
    )

    # Analysis
    _seed(
        Analysis,
        Kind.Analysis,
        "6",
        None,
        "json",
        {"payload": {"summary": "fixture analysis", "topics": ["foo"]}},
    )

    # SessionAnalysis
    _seed(
        SessionAnalysis,
        Kind.SessionAnalysis,
        "7",
        None,
        "json",
        {
            "payload": {
                "summary": "fixture session",
                "topics": ["session"],
                "windows": [],
            },
            "speaker_names": {"Speaker_0001": "Test Speaker"},
        },
    )

    # Embedding — shape matches what embed.text writes (``vector`` singular).
    _seed(
        Embedding,
        Kind.Embedding,
        "8",
        None,
        "json",
        {
            "vector": [0.1, 0.2, 0.3],
            "dimensions": 3,
            "model": "fixture-embedder",
            "source_id": ("e" * 64),  # the transcript fixture
        },
    )

    return seeded


# ─────────────────────────────────────────────────────────────────
# Param synthesis per op
# ─────────────────────────────────────────────────────────────────


def _schema_def_fixture(tmp: Path) -> Path:
    """A tiny JSON schema for intelligence.extract/analyze."""
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }
    p = tmp / "fixture_schema.json"
    p.write_text(json.dumps(schema))
    return p


def _template_fixture(tmp: Path) -> Path:
    """Minimal Jinja2 template — uses ``session`` (report.session) or
    ``analyses`` (report.zeitgeist) defensively so the same file fits both."""
    p = tmp / "fixture_report.md.j2"
    p.write_text(
        "# Report\n\n"
        "{{ title or 'Untitled' }}\n\n"
        "{% for s in segments or [] %}- {{ s }}\n{% endfor %}\n"
    )
    return p


def _speakers_csv_fixture(tmp: Path) -> Path:
    p = tmp / "fixture_speakers.csv"
    p.write_text("speaker_id,name\nSpeaker_0001,Test Speaker\n")
    return p


def build_op_invocation(
    op: type[Operation],
    fixtures: dict[Kind, str],
    fixture_files: dict[str, Path],
    tmp: Path,
) -> tuple[list[str] | None, dict[str, Any] | None, str | None]:
    """Choose inputs + params for an op.

    Returns ``(inputs, params, skip_reason)``. When ``skip_reason`` is
    non-None, the op is skipped without invocation.
    """
    name = op.name

    # ── Ops that require external URLs or live network ────────────
    if name in {
        "acquire.url",
        "acquire.livestream",
        "metadata.scrape_page",
        "web.fetch",
    }:
        return None, None, "needs live URL / network"

    # ── Acquire upload — use the vendored mp4 ─────────────────────
    if name == "acquire.upload":
        src = fixture_files["sample_mp4"]
        return [], {"source_path": str(src), "link_mode": "copy"}, None

    # ── Document parse — vendored tiny.pdf ────────────────────────
    if name == "document.parse":
        src = fixture_files["tiny_pdf"]
        return [], {"source_path": str(src)}, None

    # ── Transcript parse — synthesize an SRT file ─────────────────
    if name == "transcript.parse":
        srt = tmp / "fixture.srt"
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:01,500\nhello world\n\n"
            "2\n00:00:01,500 --> 00:00:03,000\nsecond line\n"
        )
        return [], {"source_path": str(srt), "format": "srt"}, None

    # ── Generic: pick inputs by kind, build params ───────────────
    # Variadic ops accept >=1 input each of which is one of input_kinds.
    # For the matrix we pick a single representative input — the first
    # declared kind that has a fixture.
    inputs: list[str] = []
    if op.variadic_inputs:
        chosen = None
        for input_kind in op.input_kinds:
            if input_kind in fixtures:
                chosen = fixtures[input_kind]
                break
        if chosen is None:
            return (
                None,
                None,
                f"no fixture for any variadic input kind in {[k.value for k in op.input_kinds]}",
            )
        inputs.append(chosen)
    else:
        for input_kind in op.input_kinds:
            if input_kind not in fixtures:
                return (
                    None,
                    None,
                    f"no fixture available for input kind {input_kind.value}",
                )
            inputs.append(fixtures[input_kind])

    # Per-op param synthesis
    params: dict[str, Any] = {}
    if name == "intelligence.extract":
        params = {
            "prompt": "Summarize this in one sentence.",
            "schema_def": str(_schema_def_fixture(tmp)),
        }
    elif name == "intelligence.analyze":
        params = {
            "schema_def": str(_schema_def_fixture(tmp)),
            "prompt": "Analyze each window in one sentence.",
        }
    elif name == "intelligence.classify":
        params = {"labels": ["positive", "negative"]}
    elif name == "intelligence.summarize":
        params = {}
    elif name == "frames.analyze":
        params = {"prompt": "Describe these frames."}
    elif name == "image.classify":
        params = {"labels": ["cat", "dog", "other"]}
    elif name == "video.multimodal":
        params = {"prompt": "Describe this video."}
    elif name == "report.session" or name == "report.zeitgeist":
        params = {"template": str(_template_fixture(tmp))}
    elif name in {"search.fulltext", "search.hybrid"}:
        params = {"query": "fixture"}
    elif name == "speakers.identify":
        params = {"speaker_db": str(_speakers_csv_fixture(tmp))}

    return inputs, params, None


# ─────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────


# Failures matching these patterns are reclassified as skipped — they
# indicate a missing optional dep at runtime that ``BackendRequirements``
# didn't declare (a doctor gap, not an engine bug). ✗ rows should mean
# "the engine itself broke for this op."
_RUNTIME_DEP_PATTERNS = (
    "is not installed",
    "env var not set",
    "available, need",  # hardware/RAM check
    "API key",
    "HF_TOKEN",
)


async def run_one(
    engine: Engine,
    op: type[Operation],
    inputs: list[str],
    params: dict[str, Any],
) -> tuple[Status, str, list[str]]:
    try:
        outputs = await engine.run(op.name, inputs=inputs, **params)
        return "ok", "", [a.id for a in outputs]
    except Exception as exc:  # noqa: BLE001 -- classifier path
        msg = f"{type(exc).__name__}: {exc}"
        if any(p in str(exc) for p in _RUNTIME_DEP_PATTERNS):
            return "skipped", f"runtime dep gap: {msg}", []
        return "failed", msg, []


async def run_matrix(
    *,
    op_filter: str | None,
    keep_store: bool,
) -> MatrixReport:
    register_all()

    started = datetime.now(UTC).isoformat()
    tmpdir = Path(tempfile.mkdtemp(prefix="op_matrix_"))
    store_root = tmpdir / "store"
    fixtures_dir = tmpdir / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    namespace = "op-matrix"

    os.environ["MEDIA_ENGINE_PERMANENT_STORE"] = str(store_root)
    os.environ["MEDIA_ENGINE_NAMESPACE"] = namespace
    os.environ["MEDIA_ENGINE_CACHE_DB_URL"] = (
        f"sqlite+pysqlite:///{tmpdir / 'cache.db'}"
    )
    os.environ.setdefault("MEDIA_ENGINE_MIN_FREE_GB", "0")

    # Vendored fixture references the matrix script needs to know about.
    fixture_files = {
        "sample_mp4": REPO_ROOT / "tests" / "fixtures" / "sample.mp4",
        "tiny_pdf": REPO_ROOT / "tests" / "fixtures" / "tiny.pdf",
    }

    cfg = EngineConfig()
    # Cache() creates its own schema via SQLAlchemy metadata.create_all
    # — no alembic needed when starting from a fresh sqlite.
    cache = Cache(cfg.resolve_cache_db_url())
    fixtures = seed_fixtures(cache, store_root, namespace)

    # Open the engine (sync constructor)
    engine = Engine.open_session(cfg)

    # Pre-compute doctor report to gate ops that have no working backend
    doc = diagnose()
    unavailable_ops = {op.op_name for op in doc.ops if op.overall == "unavailable"}
    # Map of op_name → first working backend name, used as a backend=
    # override when the static default is unavailable but an alternative
    # works (router ops). This exercises the engine through a viable
    # path; the bug in the default routing is surfaced separately by
    # doctor's per-backend status.
    backend_override: dict[str, str] = {}
    for op_report in doc.ops:
        if (
            op_report.default_backend_status == "unavailable"
            and op_report.overall == "ok"
        ):
            for b in op_report.backends:
                if b.overall == "ok":
                    backend_override[op_report.op_name] = b.backend_name
                    break

    ops = OpRegistry.list_all()
    if op_filter is not None:
        ops = [op for op in ops if op.name == op_filter or op.name.startswith(op_filter)]

    results: list[OpResult] = []
    for op_cls in ops:
        # Skip doctor-unavailable ops up front
        if op_cls.name in unavailable_ops:
            results.append(
                OpResult(
                    op_name=op_cls.name,
                    op_version=op_cls.version,
                    status="skipped",
                    reason="doctor: no working backend",
                )
            )
            continue

        inputs, params, skip = build_op_invocation(
            op_cls, fixtures, fixture_files, fixtures_dir
        )
        if skip is not None:
            results.append(
                OpResult(
                    op_name=op_cls.name,
                    op_version=op_cls.version,
                    status="skipped",
                    reason=skip,
                )
            )
            continue

        assert inputs is not None and params is not None
        # If doctor flagged the static default as broken but an
        # alternative works, steer through the alternative — the matrix
        # is testing "does this op run end-to-end at all", not "does
        # the default route work" (doctor covers that).
        if op_cls.name in backend_override:
            params = {**params, "backend": backend_override[op_cls.name]}
        t0 = datetime.now(UTC)
        status, reason, output_ids = await run_one(engine, op_cls, inputs, params)
        elapsed_ms = (datetime.now(UTC) - t0).total_seconds() * 1000
        results.append(
            OpResult(
                op_name=op_cls.name,
                op_version=op_cls.version,
                status=status,
                reason=reason,
                elapsed_ms=elapsed_ms,
                output_ids=output_ids,
            )
        )

    engine.close()

    finished = datetime.now(UTC).isoformat()
    summary = {"ok": 0, "skipped": 0, "failed": 0}
    for r in results:
        summary[r.status] += 1

    if not keep_store:
        shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        print(f"[op_matrix] kept tmp store at {tmpdir}")

    return MatrixReport(
        started_at=started,
        finished_at=finished,
        summary=summary,
        results=results,
    )


# ─────────────────────────────────────────────────────────────────
# Markdown emission
# ─────────────────────────────────────────────────────────────────


_STATUS_GLYPH = {"ok": "✓", "skipped": "⊘", "failed": "✗"}


def emit_markdown(report: MatrixReport, *, out_path: Path) -> None:
    s = report.summary
    lines = [
        "# Op matrix report",
        "",
        f"Generated by `scripts/op_matrix.py` at {report.finished_at}.",
        "",
        f"**Summary** — ✓ {s.get('ok', 0)} · ⊘ {s.get('skipped', 0)} · ✗ {s.get('failed', 0)}"
        f" (of {len(report.results)} ops)",
        "",
        "| Status | Op | Version | Reason / output | Elapsed |",
        "| ------ | -- | ------- | --------------- | ------- |",
    ]
    for r in sorted(report.results, key=lambda r: (r.status != "failed", r.op_name)):
        glyph = _STATUS_GLYPH[r.status]
        detail = ""
        if r.status == "ok":
            ids = ", ".join(i[:12] for i in r.output_ids) or "—"
            detail = f"outputs: {ids}"
        else:
            detail = r.reason or "—"
        elapsed = f"{r.elapsed_ms:.0f}ms" if r.elapsed_ms else "—"
        lines.append(
            f"| {glyph} {r.status} | `{r.op_name}` | {r.op_version} | {detail} | {elapsed} |"
        )
    lines.append("")
    lines.append("> ✗ rows are real regressions. ⊘ rows are intentional skips ")
    lines.append("> (no working backend per `med doctor`, or no synthesizable params/inputs). ")
    lines.append(
        "> ✓ rows ran through the full `Engine.run` lifecycle "
        "(cache key, lineage, events, cost ledger)."
    )
    out_path.write_text("\n".join(lines))


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--filter", default=None, help="Only run ops matching this prefix")
    parser.add_argument("--keep-store", action="store_true", help="Keep the temp store after run")
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "tests" / "e2e_op_matrix_report.md"),
        help="Output markdown path",
    )
    parser.add_argument(
        "--json", dest="json_out", default=None, help="Also write structured JSON to this path"
    )
    args = parser.parse_args()

    report = asyncio.run(
        run_matrix(op_filter=args.filter, keep_store=args.keep_store)
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    emit_markdown(report, out_path=out_path)

    if args.json_out is not None:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "started_at": report.started_at,
                    "finished_at": report.finished_at,
                    "summary": report.summary,
                    "results": [asdict(r) for r in report.results],
                },
                indent=2,
            )
        )

    print(
        f"\nOp matrix: ✓ {report.summary.get('ok', 0)}  "
        f"⊘ {report.summary.get('skipped', 0)}  "
        f"✗ {report.summary.get('failed', 0)}   →  {out_path}"
    )
    # Exit non-zero on any ✗
    return 1 if report.summary.get("failed", 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
