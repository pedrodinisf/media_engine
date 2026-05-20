# media_engine

Universal media-processing engine. Typed artifacts, composable operations,
pluggable backends, content-addressed caching, async DAG execution. Ships as
a Python package + CLI (`med`) + daemon + MCP exporter (REST in Phase 4).

## Quickstart

```bash
uv sync
uv run pytest -q                 # 621 passed, 23 skipped
uv run ruff check && uv run pyright media_engine
uv run med ops                   # 31 capability-named ops
```

## Architecture

See [`docs/architecture.md`](docs/architecture.md) (comprehensive as-built
reference) and the full implementation plan / roadmap at
`~/.claude/plans/goofy-gathering-beaver.md` (its §0 tracks status).

## Status

**Phases 0–3 complete and green** (commits 1–28 + 3 audit-fix commits).
Phase 0–2 brought typed artifacts, content-addressed cache, DAG executor,
daemon, MCP exporter, profiles, the audio/video/frames/image/chunk/embed/
intelligence op families, cost ledger + retry/events. **Phase 3** added
acquisition (`acquire.url` + `acquire.livestream`), web/document ingest
(`web.fetch` + `document.parse`), transcript ingest
(`transcript.parse` + `transcript.merge`), metadata scraping
(`metadata.scrape_page`), the search trio
(`search.semantic`/`fulltext`/`hybrid` + `med search`), and lineage
hardening with a reanalysis recipe. **Phase 4 next**
(REST + full MCP stdio server + Postgres + IaaC, commits 29–34). See
`CLAUDE.md` for contributor orientation.
