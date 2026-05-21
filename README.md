# media_engine

Universal media-processing engine. Typed artifacts, composable operations,
pluggable backends, content-addressed caching, async DAG execution. Ships as
a Python package + CLI (`med`) + daemon + REST API + MCP stdio server.

## Quickstart

```bash
uv sync
uv run pytest -q                 # 702 passed, 29 skipped
uv run ruff check && uv run pyright media_engine
uv run med ops                   # 31 capability-named ops
```

## Architecture

See [`docs/architecture.md`](docs/architecture.md) (comprehensive as-built
reference) and the full implementation plan / roadmap at
`~/.claude/plans/goofy-gathering-beaver.md` (its §0 tracks status).
Deployment notes live in [`docs/deployment.md`](docs/deployment.md);
the bundled IaaC (Dockerfile, docker-compose, Helm chart, Terraform
module) is under [`infra/`](infra/).

## Status

**Phases 0–4 complete and green** (commits 1–34 + 3 audit-fix commits).
Phases 0–2 brought typed artifacts, content-addressed cache, DAG
executor, daemon, MCP exporter, profiles, the
audio/video/frames/image/chunk/embed/intelligence op families, and the
cost ledger + retry/events stack. **Phase 3** added acquisition
(`acquire.url` + `acquire.livestream`), web/document ingest
(`web.fetch` + `document.parse`), transcript ingest
(`transcript.parse` + `transcript.merge`), metadata scraping
(`metadata.scrape_page`), the search trio
(`search.semantic`/`fulltext`/`hybrid` + `med search`), and lineage
hardening with a reanalysis recipe. **Phase 4** added the FastAPI
REST surface + `Job` concept + bearer-token auth, the full MCP stdio
server with allow-list security, Postgres / pgvector /
postgres-tsvector backends + alembic migrations + `med db`, LRU
eviction + workdir GC + `med storage`, the IaaC bundle (Docker, Helm,
Terraform) + `/health` + `/ready` probes, and the `resources.yaml`
loader for declarative resource overrides. **Phase 5 next** (domain
profiles + speakers + reports + final polish, commits 35–38). See
`CLAUDE.md` for contributor orientation.
