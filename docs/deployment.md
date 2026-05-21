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
For multi-tenant fairness, namespacing (`med --namespace …`) gives you
quota at the cache level; load balancing across replicas gives you
parallel CPU.

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
