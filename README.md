# media_engine

Universal media-processing engine. Typed artifacts, composable operations,
pluggable backends, content-addressed caching, async DAG execution. Ships as
a Python package + CLI (`med`) + daemon + REST + MCP.

## Quickstart

```bash
uv sync
uv run pytest -q                 # 503 passed, 19 skipped
uv run ruff check && uv run pyright media_engine
uv run med ops                   # 21 capability-named ops
```

## Architecture

See [`docs/architecture.md`](docs/architecture.md) (comprehensive as-built
reference) and the full implementation plan / roadmap at
`~/.claude/plans/goofy-gathering-beaver.md` (its §0 tracks status).

## Status

**Phases 0–2 complete and green** (commits 1–22 + audit hardening):
typed artifacts, content-addressed cache, DAG executor, daemon, MCP
exporter, profiles, the audio/video/frames/image/chunk/embed/
intelligence op families, cost ledger + retry/events. **Phase 3 next**
(acquisition + transcript ingest + non-video media + search, commits
23–28). See `CLAUDE.md` for contributor orientation.
