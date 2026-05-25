# `deploy/hetzner/` — single-VPS Hetzner deployment

One-shot, idempotent deployment of the entire `media_engine` stack on a
fresh Hetzner Ubuntu VPS: Caddy (TLS via Let's Encrypt) → engine
container (full Linux extras superset + Playwright chromium baked in) →
Postgres with pgvector. Bearer-token auth, mounted `secrets.env` for
post-deploy key rotation, UFW + fail2ban + unattended security
upgrades + Docker daemon hardening.

## Quick start

```bash
ssh ubuntu@<vps-ip>
git clone https://github.com/<you>/media_engine.git
cd media_engine
bash deploy/hetzner/bootstrap.sh
```

The script is interactive on first run (prompts for domain, ACME
email, Postgres password, optional API keys), idempotent on repeat
runs, and prints the bootstrap bearer token at the end — **save it
once; it cannot be recovered**.

## Full guide

See [**Hetzner Deployment Handbook**](../../docs/Hetzner_Deployment_Handbook.md)
for prerequisites, the 21-step bootstrap flow, verification, day-2
operations (update / backup / restore / key rotation), security model,
sizing/swap, monitoring, FAQ, disaster recovery, and decommissioning.

## Files in this directory

| File | Purpose |
|---|---|
| `bootstrap.sh` | One-shot provisioner — the whole stack, end to end. |
| `update.sh` | `git pull` + rebuild engine image + recreate engine container. |
| `backup.sh` | `pg_dump` + tar artifact volume to `backups/<timestamp>/`. |
| `restore.sh` | Inverse of `backup.sh`. |
| `logs.sh` | `docker compose logs -f` wrapper. |
| `doctor.sh` | `med doctor` passthrough into the running container. |
| `shell.sh` | `docker compose exec engine bash` for ad-hoc inspection. |
| `Dockerfile.hetzner` | Wrapper image — full extras superset + Playwright chromium baked in. |
| `docker-compose.override.yaml` | Adds Caddy, swaps engine image, drops public :8000, mounts secrets + pgvector init. |
| `Caddyfile` | SSE-safe reverse proxy with auto-TLS. |
| `init/01-vector.sql` | `CREATE EXTENSION vector` — Postgres init step. |
| `daemon.json` | Docker daemon hardening (log rotation, live-restore, userland-proxy off). |
| `unattended-upgrades.conf` | Security-only auto-patching, no auto-reboot. |
| `.env.example` | Compose-level config template (no secrets). |
| `secrets.env.example` | Operator secret template — mounted into the container. |

## What this does NOT touch

Nothing outside `deploy/hetzner/` is modified by these files. The
upstream `infra/docker/Dockerfile` and `infra/docker/docker-compose.yaml`
stay portable for dev, ARM, K8s; the Hetzner-specific bloat
(extras × 12 + chromium + apt deps) lives in `Dockerfile.hetzner`,
which is invoked only when the compose override is in play.
