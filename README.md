# media_engine

Universal media-processing engine. Typed artifacts, capability-named
operations, pluggable backends, content-addressed caching, async DAG
execution. Ships as a Python package + CLI (`med`) + daemon + REST + MCP
stdio server.

```bash
uv sync
uv run med ops                                    # 34 ops registered
uv run med profile ls                             # 8+ bundled profiles
uv run med profile run analysis-full --input <video-id>
```

## What you get out of the box

- **34 capability-named ops** across `acquire.*`, `audio.*`, `video.*`,
  `image.*`, `frames.*`, `document.*`, `web.*`, `transcript.*`,
  `chunk.*`, `embed.*`, `intelligence.*`, `metadata.*`, `search.*`,
  `speakers.*`, and `report.*`.
- **30+ backends** behind those ops: mlx-whisper, pyannote, yt-dlp,
  playwright, gemini, claude, mlx-lm, sentence-transformers, sqlite-fts5,
  pgvector / postgres-tsvector, ffmpeg-uniform, vllm-mlx, open-clip,
  rapidocr, pymupdf, …
- **8 bundled profiles** (see `profiles/`): an `analysis-full` reference
  pipeline plus five `kind: prompt` lenses
  (`video-knowledge`, `technical-academic`, `diy-electronics`,
  `cooking-recipes`, `general-custom`) and two minimal examples.
- **Five transports** for every op: CLI, daemon, REST + SSE, MCP stdio,
  Python API. Adding an op or backend lights it up across all five
  automatically.

## Install

```bash
uv sync                              # core + dev
uv sync --extra api --extra postgres # serve REST against postgres
uv sync --extra llm-mlx              # local LLM (mlx-lm) on Apple Silicon
uv sync --all-extras                 # everything
```

Optional-dependency extras keep the import surface lean and gate ML
libraries behind explicit opt-in. See `pyproject.toml::optional-dependencies`
for the full matrix.

## 30-second tour

```bash
# What's available
uv run med ops                       # all 34 ops
uv run med profile ls                # 8 bundled profiles
uv run med config                    # effective config

# Ingest something
uv run med acquire <local-file>
uv run med acquire-url <youtube-or-direct-url> --quality 360p

# Run a profile end-to-end
uv run med profile run analysis-full --input <video-id>

# Run a single op
uv run med run audio.transcribe --input <audio-id> --param model=...

# Inspect what came out
uv run med ls                        # cache listing
uv run med lineage <artifact-id>     # upstream tree

# Operate
uv run med daemon start              # warm engine for fast reuse
uv run med api start                 # REST + SSE on :8000
uv run med mcp serve                 # MCP stdio for LLM clients
```

Use `--help` on any subcommand for the full flag set, or see
[`docs/cli_reference.md`](docs/cli_reference.md).

## Adding your own

- **Op:** [`docs/adding_an_operation.md`](docs/adding_an_operation.md)
- **Backend:** [`docs/adding_a_backend.md`](docs/adding_a_backend.md)
- **Profile:** [`docs/writing_a_profile.md`](docs/writing_a_profile.md)

A new op or backend is picked up by all five transports the moment you
add it to `media_engine/bootstrap.py::_op_classes()` /
`_backend_classes()`.

## Reference

- **Architecture (as-built):** [`docs/architecture.md`](docs/architecture.md)
- **CLI reference:** [`docs/cli_reference.md`](docs/cli_reference.md)
- **REST + MCP API reference:** [`docs/api_reference.md`](docs/api_reference.md)
- **Deployment (Docker / Helm / Terraform):** [`docs/deployment.md`](docs/deployment.md)
- **Bundled profile guide:** [`docs/profile_analysis_full.md`](docs/profile_analysis_full.md)
- **Contributor orientation:** [`CLAUDE.md`](CLAUDE.md)
- **Changelog:** [`CHANGELOG.md`](CHANGELOG.md)

## Status

**v0.5.0 — Phases 0–5 complete.** Suite 768 passed / 29 skipped (dep- and
API-key-gated). Ruff + pyright strict clean. 34 ops, 30+ backends, 14
artifact kinds, 50 commits across phases 0–5.

**Roadmap.** Phase 6 — local-first Web UI (SvelteKit SPA served by
`med web start`, full GUI parity with the CLI). Phase 7 — acoustic speaker
identity (`speakers.embed_voice` + `speakers.cluster` + `speakers.match`,
voice-fingerprint DB reusing the pgvector backend). Both are spec'd in
plan §12.5 / §12.6.

v1.0 lands when the REST surface freezes (after Phase 6). Before then
semver stays 0.x; backwards compatibility is best-effort but not
contractual.
