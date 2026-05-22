# Deployment

This doc covers running media_engine outside of a developer's laptop:
container images, environment variables, storage volumes, health
probes, and scaling considerations. The IaaC scaffolding lives in
`infra/` — start there if you just need a working compose / chart /
module.

---

## Environment variables

Every setting honors a `MEDIA_ENGINE_*` env var. The ones that matter
most in a container:

| Variable | Default | Purpose |
|---|---|---|
| `MEDIA_ENGINE_PERMANENT_STORE` | `/Volumes/UNIVERSE_V/MEDIA/media_engine` | Where artifacts + cache.db live. Override with a bind-mount or PVC. |
| `MEDIA_ENGINE_DB_URL` | derived from `permanent_store` (`sqlite+pysqlite:///…/cache.db`) | Postgres URL to use instead of SQLite. |
| `MEDIA_ENGINE_LOG_FORMAT` | `text` | Set to `json` for one-JSON-record-per-line stdout. |
| `MEDIA_ENGINE_MIN_FREE_GB` | `20` | Disk-guard threshold. Lower in CI / dev containers. |
| `MEDIA_ENGINE_GC_INTERVAL` | `3600` | Seconds between daemon GC sweeps. |
| `MEDIA_ENGINE_SEMANTIC_DB_URL` | — | Optional override for the `pgvector` backend. |
| `MEDIA_ENGINE_FULLTEXT_DB_URL` | — | Optional override for the `postgres-tsvector` backend. |
| `MEDIA_ENGINE_MAX_UPLOAD_MB` | `2048` | Web UI upload size cap (`POST /acquire/upload`). Larger bodies abort with 413 before the engine sees them. |
| `MEDIA_ENGINE_CORS_ORIGINS` | — | Comma-separated allow-list for browser dev servers. Empty = same-origin only. |
| `MEDIA_ENGINE_NO_BROWSER` | — | Non-empty disables `med web start --open` auto-launch (Docker, CI). |

The full surface lives in `media_engine.config:EngineConfig`. Anything
on the Pydantic model is settable via env with the `MEDIA_ENGINE_`
prefix.

---

## Volumes

| Path | Contents | Lifetime |
|---|---|---|
| `MEDIA_ENGINE_PERMANENT_STORE` (default `/var/lib/media_engine` in container) | sha256-keyed artifact files, `cache.db` (sqlite), `logs/`, `server-state/` | persistent (PVC, bind-mount, or S3 future) |
| `/tmp/media_engine` (default `MEDIA_ENGINE_WORKDIR`) | per-job temp dirs, GC'd after 24 h on failure | ephemeral |

If you mount only the permanent_store and let workdir be the
container's writable layer, GC still runs but you don't get
checkpointable workdirs across restarts — that's fine for production.

---

## Probes

The REST surface exposes two **un-authenticated** endpoints:

- `GET /health` — always 200 if the process is alive. Kubelet should
  restart the pod on failure.
- `GET /ready` — 200 when every dependency (cache, permanent_store,
  optional daemon socket) is `ok`; 503 when any is `down`. Kubelet
  should remove from rotation on failure.

The bundled Helm chart wires both into Deployment probes; the
Dockerfile includes a `HEALTHCHECK` that hits `/ready`.

---

## Scaling

The engine is single-process; the REST surface is async and shares
**one** `Engine.open_session()` per container. To scale horizontally:

1. Move the cache to Postgres (`MEDIA_ENGINE_DB_URL=postgresql+psycopg://…`).
   Multiple replicas need a shared cache or the content-addressing
   guarantee weakens (the same artifact might exist with two different
   ids across replicas).
2. Move the artifact store to a network filesystem (NFS / EFS) or
   wait for the S3 storage backend (deferred, see the plan §15).
   ReadWriteMany volumes are the easiest path today.
3. Set `replicaCount > 1` in the Helm chart.

Resource semaphores in `runtime/dag.py` are per-process; with multiple
replicas each replica enforces its own `apple_neural_engine: 1` limit.

## Namespacing & multi-tenancy

The cache is namespace-aware (`MEDIA_ENGINE_NAMESPACE` env or
`med --namespace`), and bearer tokens carry a namespace too. **The
contract is strict**: a token whose namespace doesn't match the API
process's namespace is rejected with 403 — running both halves
mismatched would silently scatter artifacts across namespaces while
reads filtered by the token returned empty.

The deployment model is **one API process per namespace**:

```sh
# Tenant A
MEDIA_ENGINE_NAMESPACE=tenant-a \
    med api start --port 8001
# In another shell:
MEDIA_ENGINE_NAMESPACE=tenant-a med api token create

# Tenant B (separate process, separate port)
MEDIA_ENGINE_NAMESPACE=tenant-b \
    med api start --port 8002
MEDIA_ENGINE_NAMESPACE=tenant-b med api token create
```

Both processes can share the same Postgres / permanent_store; the
cache's `namespace` column keeps them isolated. The CLI's
`med --namespace` flag plus `MEDIA_ENGINE_NAMESPACE` env are
equivalent — the env is the simpler choice for long-running
services.

---

## Web UI

The SvelteKit SPA at `/ui` is served by the same FastAPI process as the
REST API. Two deployment shapes:

- **`med web start`** — the local-first launcher. Same uvicorn as
  `med api start`, plus the `/ui` static mount + an optional
  `--open` browser launch. Use when you want one process per user.
- **`med api start`** — headless. The mount auto-activates when
  `media_engine/web/dist/index.html` is present; if it isn't, the
  process logs a warning and the API keeps working without `/ui`.
  Use for CI / production deployments that don't need a GUI.

**Wheel install ships the UI for free.** `pyproject.toml`'s
`hatch.build.targets.wheel.force-include` bundles
`media_engine/web/dist/` into the wheel. Both the PyPI install and the
Dockerfile ship the prebuilt assets. Developers from source run a
one-time `pnpm -C web install && pnpm -C web build` after `uv sync`.

**Security headers.** `media_engine/api/middleware.py` adds CSP
(`default-src 'self'; …'wasm-unsafe-eval'; …'unsafe-inline'`, the
last two scoped to support `pdf.js` and Svelte scoped styles
respectively), `X-Content-Type-Options: nosniff`, and
`Referrer-Policy: same-origin` to `/ui/*` responses only — REST
clients see no CSP. CORS is same-origin by default; set
`MEDIA_ENGINE_CORS_ORIGINS=https://dev.local:5173,https://…` to open
specific origins for dev-server scenarios.

**SSE auth.** `EventSource` cannot set custom headers, so
`GET /jobs/{id}/events` and `GET /events/stream` accept
`?token=...` as a fallback alongside `Authorization: Bearer …`.
Acceptable on loopback / private-network deploys; production HTTPS
exposure should keep the UI behind an authenticating reverse proxy
or wait for the v1.x job-scoped-nonce hardening path (catalogued in
`web_ui_deferred.md`).

**Upload cap.** `POST /acquire/upload` accumulates bytes in a tmp
file under the workdir; `MEDIA_ENGINE_MAX_UPLOAD_MB` (default 2048)
bounds it. Bigger uploads abort with 413 mid-stream.

See [`web_ui.md`](web_ui.md) for the panel-by-panel user guide.

---

## Build via Dockerfile

`infra/docker/Dockerfile` is a four-stage multi-stage build. The
default `docker build .` produces a Node-free runtime image with the
SvelteKit SPA already inside:

```bash
docker build -t media-engine:0.6.0 -f infra/docker/Dockerfile .
```

The stages, in order:

| Stage | Image | Purpose |
|---|---|---|
| `ui-build` | `node:22-bookworm-slim` | `corepack enable` + `pnpm -C web install --frozen-lockfile` + `pnpm -C web build`. The only stage with Node. |
| `builder` | `python:3.11-slim` | `uv sync` with the runtime extras (`api`, `postgres`, `acquire-url`, `search`). |
| `api-only` | `python:3.11-slim` | Headless Python runtime: ffmpeg + libpq5 + curl + ca-certs, copy from `builder`, drop privileges to `engine`, expose 8000, healthcheck on `/ready`. |
| `runtime` (default) | `api-only` + UI | `COPY --from=ui-build` the populated `media_engine/web/dist/` tree into place so FastAPI's StaticFiles mount activates. |

Opt-outs:

```bash
# Headless deployment (no /ui mount).  Skips the ui-build stage entirely
# when buildx prunes unused stages.
docker build --target api-only -t media-engine:0.6.0-api .

# Just the dist tree (e.g. to vendor it elsewhere).
docker build --target ui-build --output type=local,dest=./_dist .
```

No host-side `pnpm` is required for any of these — Node is confined
to the `ui-build` stage and never lands in the final image.
`tests/test_dockerfile.py` asserts this structural invariant
(`ui-build` stage exists, runtime stage doesn't `apt-get install
nodejs`, the default target is `runtime`).

The Helm chart at `infra/helm/media-engine/` references the same
image; pass `image.repository` + `image.tag` to point at a built
artifact. The compose stack at `infra/docker/docker-compose.yaml`
rebuilds from source on `docker compose up --build`.

---

## S3 storage (deferred)

The `StorageBackend` Protocol is formalized in `runtime/storage.py`
and ships with one implementation, `LocalFSStorage`. An `S3Storage`
implementation is intentionally **out of scope for v1** — async file
ops, range reads, multipart uploads, and signed URLs for
`GET /artifacts/{id}/file` all need careful design. The Protocol is
in place so the engine doesn't have to change to add it later; the
implementation lands post-v1.

---

## Smoke-test sequence

After bringing the chart up:

```sh
TOKEN=$(med api token create --json | jq -r .secret)
curl -fsS -H "Authorization: Bearer $TOKEN" http://media-engine/operations
curl -fsS -H "Authorization: Bearer $TOKEN" http://media-engine/ready
```

If `/ready` returns 200 and `/operations` lists 31+ ops, the deployment
is healthy.
