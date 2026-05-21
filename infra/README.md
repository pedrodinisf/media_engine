# Infrastructure as Code

Deployment scaffolding for media_engine. Three layouts ship in this tree:

- **`docker/`** — multi-arch `Dockerfile` and a `docker-compose.yaml`
  that brings up the engine + a `pgvector/pgvector:pg16` Postgres
  sidecar. Right tool for a single host (laptop, single VM).
- **`helm/media-engine/`** — Helm chart skeleton (Deployment, Service,
  PVC, Secret, Ingress). Right tool for Kubernetes. Provide
  `db.url` and (optionally) `ingress.*` values.
- **`terraform/modules/media-engine/`** — Terraform module that wraps
  the Helm chart so it can be composed with cluster + Postgres modules
  in a larger deployment. The bundled chart is referenced by relative
  path (`${path.module}/../../helm/media-engine`).

These are intentionally minimal — they declare the engine's shape
(image, volume, env, probes) without prescribing your platform's
networking, secrets, or backup story.

## Quick local boot

```
docker compose -f infra/docker/docker-compose.yaml up -d --build
curl -s http://localhost:8000/health
curl -s http://localhost:8000/ready
```

The first run downloads the pgvector image, builds the engine image,
and creates the `engine-store` + `postgres-data` volumes; subsequent
runs start in seconds.

## Things to provide yourself

| Concern | Where |
|---|---|
| TLS / ingress | `helm/values.yaml` `ingress.*`; Terraform `var.tls_*` shim |
| API tokens | `med api token create` after first boot |
| Cluster + node pools | upstream Terraform; this module assumes Helm-ready cluster |
| Postgres backups | the chart deploys the engine, not the database |
| Image registry | `image.repository` in `values.yaml` / `image_repository` in TF |

See `docs/deployment.md` for the longer narrative.
