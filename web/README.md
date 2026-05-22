# media_engine — web UI (Phase 6)

Local-first SvelteKit SPA bundled into the `media_engine` Python wheel.
Served by FastAPI `StaticFiles` under `/ui` (see `media_engine/api/app.py`
+ `media_engine/cli/web.py`).

## Quick start

```bash
# from repo root
pnpm -C web install --frozen-lockfile
pnpm -C web build              # populates ../media_engine/web/dist/
uv run med web start --open    # launches http://localhost:8000/ui
```

## Tooling

| Concern | Tool |
|---|---|
| Framework | SvelteKit 2 + Svelte 5 + `@sveltejs/adapter-static` |
| Language | TypeScript strict (mirrors engine's pyright strict) |
| Styling | Tailwind v4 + Clean-NASA tokens (`src/app.css` + `src/lib/theme/tokens.ts`) |
| Forms | Custom Svelte renderer over JSON Schema (commit 42) |
| Data | `@tanstack/svelte-query` + native `EventSource` (commit 42-43) |
| DAG / lineage | `@xyflow/svelte` (commit 45-47) |
| YAML editor | CodeMirror 6 (commit 47) |
| Charts | Layer Chart / LayerCake (commit 46) |
| Tests | Vitest (`tests/unit/`) + Playwright (`tests/e2e/`) |

## Scripts

```bash
pnpm dev          # vite dev (proxies /run, /jobs, /artifacts, etc. to localhost:8000)
pnpm build        # static build to ../media_engine/web/dist/
pnpm typecheck    # svelte-check --fail-on-warnings
pnpm test         # vitest run
pnpm e2e          # playwright
```

## Layout

See plan §4 in `~/.claude/plans/you-are-resuming-goofy-spark.md`.
