# Hetzner Deployment Handbook

> Single-VPS, Docker-Compose deployment of `media_engine` on Hetzner
> Cloud. Caddy + Let's Encrypt for TLS, Postgres + pgvector for the
> cache, the full Linux-viable backend extras superset baked into the
> engine image, and operator-managed secrets that the Web UI can
> rotate without a restart. Bootstrap is one command.

---

## 0. Before you run bootstrap.sh — base hardening

This deploy is the **application layer**. The **base-OS layer** (SSH
ciphers, sysctl drop-in, persistent journald, auditd, off-site
restic backups, Tailscale to close public SSH) lives in a separate
generic guide:

> [`hetzner_vpns/Hardened Hetzner Ubuntu VPS Handbook.md`](../../hetzner_vpns/Hardened%20Hetzner%20Ubuntu%20VPS%20Handbook.md)
> &nbsp;— a 15-phase walkthrough that turns a brand-new Hetzner
> Ubuntu VPS into a hardened public-facing host.

**Recommended order for a production deploy:**

| Phase | What | When to skip |
|---|---|---|
| 0–10 | Local SSH key, provisioning choices, non-root user, UFW, fail2ban, unattended-upgrades, sysctl, audit logging, Caddy install, Docker install | Never. The bootstrap script defensively re-asserts UFW/fail2ban/unattended-upgrades/Docker, but the SSH cipher hardening + sysctl drop-in are not negotiable on a public-Internet box. |
| 11 (dev tooling) | zsh / starship / mise / modern CLI | Skip for an app-only host. |
| 12 (Claude Code) | Native or APT install | Skip unless you SSH into this VPS to develop. |
| 13 (Tailscale) | Closes public SSH entirely | **Highly recommended.** See §8 below for the short version. |
| 14 (restic backups) | Off-site backup to a *different* vendor | **Highly recommended.** See §7.3 below. |

**The bootstrap script is idempotent against pre-applied
hardening.** It checks each requirement before touching it: UFW rules
that already exist aren't re-added, fail2ban jails already present
aren't re-installed, etc. Running the generic handbook first and then
`bash deploy/hetzner/bootstrap.sh` produces the same end state as
running bootstrap alone — the only difference is that the generic
handbook adds deeper hardening (sysctl drop-in via
`/etc/sysctl.d/99-hardening.conf`, Mozilla "modern" SSH cipher
profile, persistent journald, auditd, Lynis cadence) that bootstrap
doesn't.

If you skip the generic handbook, `bootstrap.sh` step 10
(`step_sysctl_hardening`) still installs `99-hardening.conf` from
`deploy/hetzner/sysctl-hardening.conf` — verbatim from the generic
handbook's Appendix B — so the minimum sysctl floor is always in
place.

---

## 1. What you're about to deploy

```
                                    Internet
                                       │
                            ┌──────────┴──────────┐
                            │ Hetzner Cloud FW    │  (edge: allow 22, 80, 443)
                            └──────────┬──────────┘
                                       │
                            ┌──────────┴──────────┐
                            │ UFW on the VPS      │  (host: allow 22, 80, 443)
                            └──────────┬──────────┘
                                       │
                  ┌────────────────────┴────────────────────┐
                  │ Docker compose network                  │
                  │                                         │
                  │ ┌───────┐    ┌─────────┐    ┌────────┐  │
                  │ │ caddy │───▶│ engine  │───▶│postgres│  │
                  │ │ 80/443│    │ :8000   │    │ pgvect │  │
                  │ └───┬───┘    └────┬────┘    └───┬────┘  │
                  └─────┼─────────────┼─────────────┼───────┘
                        │             │             │
                  ┌─────┴───┐  ┌──────┴─────┐  ┌────┴────┐
                  │ caddy-  │  │ engine-    │  │postgres-│
                  │ data    │  │ store      │  │ data    │
                  │ (certs) │  │ (artifacts │  │         │
                  │         │  │  + HF cache)│ │         │
                  └─────────┘  └─────────────┘ └─────────┘
```

Three containers, all `restart: unless-stopped`:

- **`caddy`** terminates TLS (auto Let's Encrypt via the HTTP-01
  challenge) and reverse-proxies everything to the engine. SSE
  endpoints are routed through a buffer-free path; the rest gets
  compression.
- **`engine`** is the `media_engine` REST API + the SvelteKit Web UI
  mounted at `/ui`, with the full Linux extras superset baked in
  (`api postgres search acquire-url diarize embed chunk vlm-cloud
  vlm-local mcp ocr classify document`) plus Playwright chromium
  pre-installed for `acquire.url`.
- **`postgres`** is `pgvector/pgvector:pg16` — the cache DB, token
  store, cost ledger, and pgvector backing for semantic search.

The public surface is **80 + 443 only**. The engine's `:8000` is
deliberately *not* published — Caddy reaches it over the compose
user-defined network. This dodges the well-known
Docker-bypasses-UFW-iptables gotcha.

## 2. Prerequisites

- A **Hetzner Cloud project** and an Ubuntu **22.04** or **24.04**
  VPS, **CX22 or larger** (8 GB RAM minimum), **x86_64**. The script
  refuses to run on other arches or distros.
- A **non-root user** on the VPS with **passwordless sudo**.
- An **SSH key** uploaded to Hetzner Console, and key-based login
  confirmed. (The script will harden SSH only if your
  `~/.ssh/authorized_keys` is non-empty — protection against
  lockout.)
- A **DNS A record** for the subdomain you'll serve from (e.g.
  `engine.example.com`) pointing at the VPS public IP. **This must
  resolve before bootstrap runs** — Let's Encrypt's HTTP-01
  challenge needs port 80 reachable from public ACME servers.
- (Strongly recommended) a **Hetzner Cloud Volume**, 50 GB+,
  attached to the VPS at provisioning time. HF model cache alone
  can hit 15 GB; artifact storage grows unbounded.
- (Optional) **API keys** for cloud backends you want enabled:
  Anthropic (`intelligence.extract`), Google Gemini (vlm + image
  ops), OpenAI (reserved), Hugging Face (gates pyannote/diarize
  models). Enter them at the bootstrap prompts or leave blank — the
  Web UI's Settings → Secrets tab lets you add them later without
  restarts.

## 3. One-shot deploy

```bash
ssh ubuntu@<vps-ip>
git clone https://github.com/<you>/media_engine.git
cd media_engine
bash deploy/hetzner/bootstrap.sh
```

The script is interactive on first run and idempotent on repeat
runs. Prompts you'll see:

| Prompt | Notes |
|---|---|
| `Public domain` | Your subdomain. Must already DNS-resolve to this VPS. |
| `Let's Encrypt notification email` | Used for cert-expiry warnings. |
| `Google Gemini API key` | Blank = `vlm.gemini` + `image.*` Gemini backends disabled. |
| `Anthropic Claude API key` | Blank = `intelligence.extract` (claude) disabled. |
| `OpenAI API key` | Blank = future OpenAI-routed ops disabled. |
| `Hugging Face token` | Blank = `audio.diarize` (pyannote) disabled. |

`POSTGRES_PASSWORD` is auto-generated with `openssl rand -base64 24`
the first time. The bootstrap bearer token is printed once at the
end — **save it; it cannot be recovered.**

If `docker info` fails immediately after the script adds you to the
docker group, the script exits cleanly and prints `newgrp docker` or
re-login as the next step. Re-run; the second pass continues from
where the first left off.

## 3.1. TLS — HTTP-01 single-cert (default) vs. DNS-01 wildcard

The default path uses **HTTP-01**: Caddy provisions one Let's Encrypt
cert for `${MEDIA_ENGINE_DOMAIN}` on first boot, using port 80 for
the ACME challenge. Simple, zero config beyond the prompt for the
domain + ACME email, and no third-party account required.

When to switch to **DNS-01 wildcard**:

- You're going to host more than one subdomain on this VPS (e.g.
  `engine.example.com` + `staging.engine.example.com` +
  `grafana.example.com`). HTTP-01 issues one cert per name, hitting
  Let's Encrypt's 5-certs-per-domain-per-week rate limit fast when
  you're iterating on staging.
- You want cert issuance to work without port 80 being reachable
  (e.g. you've moved SSH + the engine behind Tailscale, or the host
  is behind a strict firewall).
- You're already on Cloudflare DNS for the zone.

**Opt-in switch** (Cloudflare DNS provider — other providers ship
their own plugins, see <https://caddyserver.com/docs/modules/>):

1. Create a Cloudflare API token at
   <https://dash.cloudflare.com/profile/api-tokens> →
   **Create Token** → **Custom token** → grant **Zone:DNS:Edit**
   on the **specific zone** serving `${MEDIA_ENGINE_DOMAIN}`. Do
   *not* use the global API key.
2. Add the token to `deploy/hetzner/secrets.env`:
   ```bash
   CLOUDFLARE_API_TOKEN=<paste-token-here>
   ```
3. Activate the wildcard compose override:
   ```bash
   docker compose \
       --env-file deploy/hetzner/.env \
       -f infra/docker/docker-compose.yaml \
       -f deploy/hetzner/docker-compose.override.yaml \
       -f deploy/hetzner/docker-compose.wildcard.yaml \
       up -d --build caddy
   ```
   The first build takes ~2 min (xcaddy + caddy-dns/cloudflare).
   Subsequent rebuilds are cached.
4. Make it persistent: edit `deploy/hetzner/_lib.sh` and add
   `-f "${REPO_ROOT}/deploy/hetzner/docker-compose.wildcard.yaml"`
   to the `dc()` function's `-f` chain. Every wrapper (`update.sh`,
   `backup.sh`, `restore.sh`, …) then picks it up automatically.

Files involved:

- `deploy/hetzner/Dockerfile.caddy` — `caddy:2-builder` + xcaddy +
  `caddy-dns/cloudflare`, copied into a slim `caddy:2-alpine` runtime.
- `deploy/hetzner/Caddyfile.wildcard` — same SSE-scoping as the
  default Caddyfile but with `acme_dns cloudflare {env.…}` at the
  global level. Add extra subdomain blocks at the bottom — they all
  share the wildcard cert with no extra issuance.
- `deploy/hetzner/docker-compose.wildcard.yaml` — swaps the
  `caddy:2-alpine` image for `media_engine-caddy:wildcard`, mounts
  `Caddyfile.wildcard`, sources `secrets.env` for
  `CLOUDFLARE_API_TOKEN`.

Reverting is one `docker compose up -d caddy` away — drop the extra
`-f` line and the stock `caddy:2-alpine` image takes over again
(certs in the `caddy-data` volume are preserved across either
direction).

## 4. What the script did, in order

| # | Step | Reversal |
|---|---|---|
| 1 | Pre-flight: refuse root, check OS + arch + repo layout | n/a |
| 2 | Add Docker's official apt repo, install `docker-ce` + plugin, `ufw fail2ban jq curl git unattended-upgrades` | `apt remove` |
| 3 | Install `daemon.json` (log rotation, live-restore, userland-proxy off), restart docker | `sudo rm /etc/docker/daemon.json && systemctl restart docker` |
| 4 | Add user to docker group | `sudo gpasswd -d $USER docker` |
| 5 | UFW: deny incoming, allow 22/80/443, enable | `sudo ufw disable` |
| 6 | fail2ban: ssh jail (3 retries / 1h ban) | `sudo rm /etc/fail2ban/jail.d/sshd-local.conf && systemctl restart fail2ban` |
| 7 | SSH hardening config (drop-in 99-) — *only if authorized_keys non-empty* | `sudo rm /etc/ssh/sshd_config.d/99-media-engine-hardening.conf && systemctl reload ssh` |
| 8 | Unattended security upgrades | `sudo rm /etc/apt/apt.conf.d/52unattended-upgrades-local` |
| 9 | Swap: 4 GB `/swapfile`, `vm.swappiness=10` | `sudo swapoff /swapfile && rm /swapfile`, edit `/etc/fstab` |
| 10 | Install `/etc/sysctl.d/99-hardening.conf` (RFC 3704 + SYN cookies + BBR + kptr_restrict + fs protections) | `sudo rm /etc/sysctl.d/99-hardening.conf && sysctl --system` |
| 11 | Hetzner Volume detection (informational) | n/a |
| 12 | Refuse if <20 GB free at `/var/lib` | n/a |
| 13 | Materialize `.env` (interactive prompts) | edit / delete `deploy/hetzner/.env` |
| 14 | Materialize `secrets.env` (chmod 600, chown to engine uid) | edit / delete `deploy/hetzner/secrets.env` |
| 15 | Build `media_engine:hetzner` image | `docker image rm media_engine:hetzner` |
| 16 | Bring Postgres up, wait healthy | `dc down postgres` |
| 17 | Bring engine + Caddy up | `dc down engine caddy` |
| 18 | Poll `https://$DOMAIN/ready` up to 5 min (covers Let's Encrypt issuance) | n/a |
| 19 | `med db migrate` | n/a (idempotent) |
| 20 | Mint a bootstrap bearer token | `med api token revoke <id>` |
| 21 | Print `med doctor` matrix with color coding | n/a |
| 22 | Print final summary | n/a |

## 5. Verifying the deploy

Run these in order; each takes seconds:

1. **Ready check:**
   `curl -fsS https://$DOMAIN/ready` → 200 with
   `{"alive": true, "ready": true, "checks": [...], ...}`.
2. **Web UI:** open `https://$DOMAIN/ui` — the SvelteKit SPA loads,
   browser TLS lock is green.
3. **Doctor matrix:** `bash deploy/hetzner/doctor.sh`.
4. **Auth probe:**
   `curl -H "Authorization: Bearer $TOKEN" https://$DOMAIN/ops`
   returns the op catalog. (Note: bare paths — no `/api/v1` prefix.)
5. **SSE smoke test:**
   `curl -N "https://$DOMAIN/events/stream?token=$TOKEN"` stays
   open and emits comment heartbeats. Confirms Caddy's SSE scoping
   end-to-end.
6. **Container state:** `dc ps` shows all three services `running`,
   engine `healthy`.
7. **Firewall sanity:** `sudo ufw status` shows only 22/80/443;
   `sudo iptables -L DOCKER-USER` shows no public exposure of :8000.
8. **pgvector:** `dc exec postgres psql -U media_engine -d media_engine -c '\dx'`
   shows `vector` in the extension list.

## 6. The doctor matrix you should see

`med doctor` shows every op × backend with a `status` of
**ok / degraded / unavailable**. On a fresh Hetzner deploy:

| State | Cause | Fix |
|---|---|---|
| **`audio.transcribe` ⛔ unavailable** | The only registered backend is `mlx-whisper` — Apple-Silicon only, won't install on Linux. | Out of scope for this deploy. Workaround: run transcription on a Mac and ingest the resulting `Transcript` artifact, or add an OpenAI/Deepgram/AssemblyAI backend (a separate engineering task, not part of this handbook). |
| **`audio.diarize` ⚠ degraded → ⛔ unavailable** | `pyannote.audio` models are gated behind a Hugging Face licence. | Set `HF_TOKEN` in `secrets.env` (Settings → Secrets in the UI, or edit the file), then re-run doctor. |
| **`audio.transcribe_diarized` ⛔ unavailable** | Composite — delegates to `audio.transcribe` (broken). Doctor traverses `delegates_to` (v0.6.2+), so the red status propagates from the delegate. | Same as above; not fixable on this VPS without a cloud transcribe backend. |
| **`intelligence.extract` (gemini / claude) ⛔ unavailable** | Missing `GEMINI_API_KEY` or `ANTHROPIC_API_KEY`. | Set in `secrets.env`. |
| **`vlm.*` (gemini) ⛔ unavailable** | Missing `GEMINI_API_KEY`. | Set in `secrets.env`. |
| Everything else ✅ ok | | |

### Composites and their delegates

Five ops are composites — they call other ops internally via
`Operation.delegates_to`. As of v0.6.2 the doctor walker traverses
this chain, so a composite's `overall` status correctly reflects
its weakest delegate:

- `intelligence.summarize` → `intelligence.extract`
- `intelligence.classify` → `intelligence.extract`
- `intelligence.analyze` → `intelligence.extract`
- `search.hybrid` → `search.semantic`, `search.fulltext`
- `audio.transcribe_diarized` → `audio.transcribe`, `audio.diarize`

Consequence: `audio.transcribe_diarized` will always be red on this
deploy because it depends on the Apple-only `audio.transcribe`. To
inspect any composite's chain explicitly:

```bash
bash deploy/hetzner/doctor.sh --op intelligence.extract
bash deploy/hetzner/doctor.sh --op audio.transcribe
```

## 7. Day-2 operations

### Updating

```bash
bash deploy/hetzner/update.sh
```

`git pull --ff-only`, rebuild only the engine image, recreate only
the engine container (`--no-deps`), wait for `/ready`, run
`med db migrate`. Postgres + Caddy untouched — no DB downtime, no
cert reissue. Total time: a few minutes (no apt, no chromium
re-download — both layers cache).

### Logs

```bash
bash deploy/hetzner/logs.sh                  # all services
bash deploy/hetzner/logs.sh engine           # engine only
bash deploy/hetzner/logs.sh caddy postgres   # multi
```

Or directly for one-offs:

```bash
docker logs -f $(docker ps -q -f name=engine)
journalctl -u docker --since '15 minutes ago'
```

### Backups (§7.2.1 — local snapshot)

```bash
bash deploy/hetzner/backup.sh
```

Writes `deploy/hetzner/backups/<UTC-timestamp>/` containing:
- `postgres.sql.gz` (pg_dump of the cache DB)
- `engine-store.tar.gz` (the artifact + HF cache volume)
- `.env` (chmod 600)
- `secrets.env` (chmod 600)
- `engine-version.txt`

**This is local-only.** Hetzner snapshots (configured at instance
creation in §1's prerequisites) cover most "I broke it myself"
recovery, but they live in Hetzner's control plane — they don't
survive an account compromise or region loss. Add an **off-site**
copy to a *different vendor*.

### §7.2.2 — Off-site backup with restic + Backblaze B2

Adopt the recipe from the generic handbook
([`hetzner_vpns/Hardened Hetzner Ubuntu VPS Handbook.md`](../../hetzner_vpns/Hardened%20Hetzner%20Ubuntu%20VPS%20Handbook.md)
§14) verbatim. Restic does encrypted, deduplicated, incremental
snapshots — typical bandwidth + storage cost is "a couple of euros
a year" for a single-VPS workload.

**Why Backblaze B2 instead of Hetzner Storage Box:** Storage Box is
cheaper but it's still Hetzner. An off-site copy must live with a
*different vendor* than the VPS to defend against vendor-side
incidents. B2 is S3-compatible, ~$6/TB/month, independent of
Hetzner.

**One-time setup, on the VPS as your sudo user:**

```bash
sudo apt-get install -y restic

# Write the env file. Lives in /root because the systemd unit runs as root.
sudo install -m 0600 /dev/stdin /root/.restic.env <<'EOF'
export RESTIC_REPOSITORY="b2:<your-bucket-name>:/media_engine"
export B2_ACCOUNT_ID="<key-id>"
export B2_ACCOUNT_KEY="<application-key>"
# CRITICAL: store this in your password manager too. Lose it → backups unrecoverable.
export RESTIC_PASSWORD="<long-random-passphrase>"
EOF

# Initialize the repo (one-time).
sudo bash -c 'source /root/.restic.env && restic init'
```

**Backup script** (writes the local snapshot via `backup.sh` first,
then pushes it + the engine-store + Postgres into restic):

```bash
sudo install -m 0700 /dev/stdin /usr/local/bin/media_engine-restic.sh <<'EOF'
#!/bin/bash
set -euo pipefail
source /root/.restic.env

# Take a fresh local backup first — this captures pg_dump + tarballs
# atomically, so restic's snapshot sees a consistent state.
cd /home/deploy/media_engine
sudo -u deploy bash deploy/hetzner/backup.sh

# Push the latest backup dir + a few host paths that aren't in the
# compose volumes (caddy state, fail2ban DB, etc.).
LATEST="$(ls -td deploy/hetzner/backups/* | head -1)"

restic backup \
    --exclude-caches \
    --exclude '/var/lib/docker/overlay2' \
    --exclude '/var/lib/docker/buildkit' \
    --exclude '/var/cache' \
    --exclude '/tmp' \
    "$LATEST" \
    /etc/caddy \
    /var/lib/caddy 2>/dev/null || true

# Retention: 7 daily, 4 weekly, 12 monthly. Tune to taste.
restic forget --prune \
    --keep-daily 7 \
    --keep-weekly 4 \
    --keep-monthly 12
EOF
```

**Systemd timer** — daily at 03:30 UTC with a 30-min jitter so a
fleet of VPSes doesn't hit B2 simultaneously:

```bash
sudo install -m 0644 /dev/stdin /etc/systemd/system/media_engine-restic.service <<'EOF'
[Unit]
Description=media_engine off-site restic backup
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/media_engine-restic.sh
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
EOF

sudo install -m 0644 /dev/stdin /etc/systemd/system/media_engine-restic.timer <<'EOF'
[Unit]
Description=Daily media_engine restic backup

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now media_engine-restic.timer
sudo systemctl list-timers --no-pager | grep media_engine
```

**Restore a single file (test this monthly):**

```bash
sudo bash -c 'source /root/.restic.env && \
    restic snapshots && \
    restic restore latest --target /tmp/restore-test --include /etc/caddy/Caddyfile'
diff -u /tmp/restore-test/etc/caddy/Caddyfile /etc/caddy/Caddyfile
```

If the diff is empty, the off-site path works. **Schedule a
quarterly full-restore test on your calendar** — an untested backup
has a coin-flip success rate when you actually need it.

### Restoring

```bash
bash deploy/hetzner/restore.sh deploy/hetzner/backups/<timestamp>
```

Prompts for an interactive `restore` confirmation, then drops + reloads
the cache DB and wipes + repopulates the `engine-store` volume from
the tarballs. Caddy + certs untouched. Always finishes with
`med db migrate` so a backup taken before a schema bump still works.

### Mint / list / revoke API tokens

```bash
bash deploy/hetzner/shell.sh -c 'med api token create --label ci-pipeline'
bash deploy/hetzner/shell.sh -c 'med api token ls'
bash deploy/hetzner/shell.sh -c 'med api token revoke <id>'
```

### Rotate or add a cloud API key after deploy

Two routes — pick whichever is convenient:

**(a)** Browser to `https://$DOMAIN/ui` → **Settings → Secrets** →
edit the field → Save. The Web UI writes through to the mounted
`secrets.env` (same file, same chmod). The engine picks the new
value up on the next op invocation; no container restart needed.

**(b)** Edit `deploy/hetzner/secrets.env` on the host directly,
then either wait for the next op invocation or force-reload with
`dc kill -s HUP engine` (graceful — engine re-reads secrets on next
config load).

Either way, confirm with:

```bash
bash deploy/hetzner/doctor.sh --op intelligence.extract
```

## 8. Security model (what's protecting you)

| Layer | What | Why |
|---|---|---|
| Edge | **Hetzner Cloud Firewall** (allow 22, 80, 443) | Drops malicious traffic before it reaches the VPS NIC. Latency-free DDoS mitigation. Configure in Hetzner Console → Firewalls, or `hcloud firewall create` + `apply-to-resource`. |
| Host | **UFW** (allow 22, 80, 443) | Defense in depth in case cloud rules drift. |
| Network | **Engine `:8000` not published** | Caddy proxies internally over the compose network. Even if a bearer token leaks via `http://` URL somewhere, the API is unreachable unencrypted. Also sidesteps the Docker-vs-UFW iptables bypass: Docker writes its own PREROUTING rules that UFW doesn't see, so a `-p 8000:8000` would have been publicly reachable regardless of `ufw deny 8000`. |
| TLS | **Caddy + auto Let's Encrypt** | Cert issuance + renewal handled automatically. `/data` volume persistence means renewals never trip Let's Encrypt rate limits (5/domain/week). |
| SSH | **Key-only, no root, AllowUsers $USER** | Drop-in at `/etc/ssh/sshd_config.d/99-media-engine-hardening.conf`. Script refuses to install this if `authorized_keys` is empty (lockout protection). |
| SSH bot floor | **fail2ban** with sshd jail | Still useful in 2026 even with key-only auth — drops automated probe noise from logs. |
| Patches | **unattended-upgrades** (security pocket only, no auto-reboot) | Kernel updates land on disk; solo operator picks the reboot window. |
| API | **Bearer tokens** on every non-public endpoint | `/health` + `/ready` + `/ui/*` open; everything else 401 without a valid token. Tokens stored as sha256 in the cache DB; secrets never recoverable. |
| Docker daemon | **`daemon.json`**: log rotation (10 MB × 3), `live-restore`, `userland-proxy: false` | Prevents log-disk-fill runaway; container survives daemon restart; cleaner iptables. |
| Secrets | **`secrets.env`** chmod 0600, chowned to engine uid, mounted read-write | Auto-loaded into `os.environ` by `EngineConfig.load`. Settings UI rotates in place. Independently backuppable. |
| Kernel | **`/etc/sysctl.d/99-hardening.conf`** (RFC 3704 reverse-path filter, SYN cookies, BBR, kptr_restrict, fs protections) | Installed by `bootstrap.sh` step 10. Verbatim from the generic handbook's Appendix B. |
| SSH attack surface | **Tailscale** (strongly recommended — see §8.1) | Closes public-port-22 entirely. Brute-force attempts go to zero; future OpenSSH CVEs stop mattering for this host. |

### 8.1. Tailscale — close public SSH entirely (strongly recommended)

Defense-in-depth tops out at "make the front door not exist." This
step takes ~3 minutes and is the single biggest reduction in attack
surface in the whole stack. Skip only if you absolutely need to SSH
from networks you can't put on a tailnet.

**On the VPS, as your sudo user:**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```

The second command prints a URL — open it on your Mac, authenticate
against your tailnet (create one free at <https://tailscale.com> if
you don't have one), and the VPS joins. `--ssh` enables Tailscale
SSH, which authenticates against your tailnet identity in addition
to (or instead of) your SSH key.

**On your Mac:** install the Tailscale app from
<https://tailscale.com/download/mac>, sign into the same tailnet.
Then edit `~/.ssh/config`:

```sshconfig
Host hetzner
    HostName <vps-tailscale-name>     # e.g. media-engine.tail-XXXX.ts.net
    User <YOUR-USERNAME>
    IdentityFile ~/.ssh/id_ed25519_hetzner
```

Confirm `ssh hetzner` works over the tailnet, then close the public
SSH path on both layers:

```bash
# On the VPS:
sudo ufw delete allow 22/tcp
sudo ufw status

# Also remove the SSH rule from the Hetzner Cloud Firewall in the
# Console (Firewalls → your rule set → delete the TCP/22 inbound).

# Add the Tailscale CGNAT range to fail2ban's ignoreip so your own
# clients don't get jailed by a fumbled retry:
sudo sed -i 's|^ignoreip = .*|ignoreip = 127.0.0.1/8 ::1 100.64.0.0/10|' \
    /etc/fail2ban/jail.d/sshd-local.conf 2>/dev/null || \
sudo install -m 0644 /dev/stdin /etc/fail2ban/jail.d/sshd-local.conf <<'EOF'
[sshd]
enabled  = true
port     = ssh
backend  = systemd
maxretry = 3
findtime = 10m
bantime  = 1h
ignoreip = 127.0.0.1/8 ::1 100.64.0.0/10
EOF
sudo systemctl restart fail2ban
```

Result: `nmap -Pn -p- <SERVER-IP>` from anywhere outside your
tailnet shows only `80/tcp` and `443/tcp` open. SSH bruteforce
attempts go from thousands per hour to zero.

For the full version with all caveats (subnet routing, exit nodes,
`tailscale ip -4` to find the assigned address, etc.) see Phase 13
of [`hetzner_vpns/Hardened Hetzner Ubuntu VPS Handbook.md`](../../hetzner_vpns/Hardened%20Hetzner%20Ubuntu%20VPS%20Handbook.md).

### Setting the Hetzner Cloud Firewall (manual or CLI)

The script can't configure the cloud firewall (it lives in Hetzner's
control plane, not on the VPS). Either:

**In the Console:** Firewalls → Create → inbound rules: TCP 22, 80,
443 from `0.0.0.0/0, ::/0`. Apply to your server.

**Via `hcloud`** (run from your laptop, not the VPS):

```bash
hcloud firewall create --name media-engine
hcloud firewall add-rule media-engine \
    --direction in --protocol tcp --port 22  --source-ips 0.0.0.0/0 --source-ips ::/0
hcloud firewall add-rule media-engine \
    --direction in --protocol tcp --port 80  --source-ips 0.0.0.0/0 --source-ips ::/0
hcloud firewall add-rule media-engine \
    --direction in --protocol tcp --port 443 --source-ips 0.0.0.0/0 --source-ips ::/0
hcloud firewall apply-to-resource media-engine \
    --type server --server <your-server-name>
```

## 9. Sizing, storage, swap

### Disk planner

| Tenant | Typical | Heavy |
|---|---|---|
| HF model cache (`engine-store/models/huggingface/`) | 5 GB | 15 GB |
| Postgres data (cache, tokens, costs, pgvector) | 1 GB | 10 GB |
| Artifacts (videos, audio, transcripts, frames) | a few GB | unbounded |
| Engine image (chromium + extras) | ~3 GB | ~3 GB |
| **Suggested Hetzner Volume** | **50 GB** | **100 GB+** |

Cap the artifact tree's growth with:

```toml
# In deploy/hetzner/.env (or via Settings → Storage in the UI)
# Activates LRU eviction with a per-bytes cap.
MEDIA_ENGINE_EVICTION_ENABLED=true
MEDIA_ENGINE_EVICTION_MAX_GB=200
```

### Mounting a Hetzner Volume at `/var/lib/media_engine`

When you attach a Volume in Hetzner Console, it shows up at
`/mnt/HC_Volume_<id>` and is pre-formatted ext4. To put the engine
artifact volume on it:

```bash
# Stop the stack first.
cd ~/media_engine && bash deploy/hetzner/logs.sh ^C  # exit logs
docker compose --project-directory . \
    -f infra/docker/docker-compose.yaml \
    -f deploy/hetzner/docker-compose.override.yaml down

# Move the existing named volume's contents onto the Hetzner Volume.
VOL=$(docker volume inspect media_engine_engine-store -f '{{.Mountpoint}}')
sudo mkdir -p /mnt/HC_Volume_<id>/engine-store
sudo rsync -aHAX "$VOL/" /mnt/HC_Volume_<id>/engine-store/

# Add an override that bind-mounts the named volume onto the Hetzner path.
cat > deploy/hetzner/docker-compose.volume.yaml <<EOF
volumes:
  engine-store:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /mnt/HC_Volume_<id>/engine-store
EOF
```

Then add `-f deploy/hetzner/docker-compose.volume.yaml` to the `dc()`
function (edit `_lib.sh`). Bring the stack back up; verify with
`docker volume inspect`.

Make the Volume mount permanent in `/etc/fstab` with `nofail` so a
detached volume can't brick boot.

### Swap

Bootstrap creates a 4 GB `/swapfile` with `vm.swappiness=10`. For
this workload (pyannote + sentence-transformers + open_clip + CLIP
+ Postgres + Caddy on 8 GB RAM), this is the floor — without it,
the first cold model-load can OOM-kill Postgres. `swappiness=10`
keeps things in RAM unless they're truly inactive.

## 10. Monitoring

You don't need Prometheus or Loki for a solo deploy. The minimum
viable setup is:

- **Built-in:** `/ready` returns 200/503 with a per-check breakdown.
  `dc ps` shows container state. Docker's `HEALTHCHECK` is recorded
  in `journalctl -u docker`.
- **UptimeRobot HTTP check on `/ready`** — free tier, 5-minute
  interval, email/Slack alerts when it goes red.
- **UptimeRobot keyword check on `/health`** — `/health` carries a
  version banner. Configure the check to alert if the version
  string doesn't match the tag you just deployed; catches stale
  containers after a botched `update.sh`.
- **Settings → Storage** in the Web UI surfaces a low-space banner
  when free disk drops under `MEDIA_ENGINE_MIN_FREE_GB`. Eyeball
  before kicking off a large batch.

Add Prometheus/Loki when you grow past one VPS or hit a real SLO.
Until then, `journalctl` + `dc logs --since 1h` covers 95% of
debugging.

## 11. Known gaps / FAQ

**Q: `audio.transcribe` is red. Why?**
The only registered backend is `mlx-whisper`, which only runs on
Apple Silicon. The repo doesn't ship a cloud or CPU-x86
transcription backend. Workarounds:
1. Run transcription on a Mac and ingest the resulting `Transcript`
   artifact (the engine's content-addressed cache will recognize
   it).
2. Add a backend (`OpenAI Whisper API`, `Deepgram`,
   `AssemblyAI`, `whisper.cpp` local) — that's a code change and is
   not part of this deploy.

**Q: `audio.diarize` is red.**
Set `HF_TOKEN` in `secrets.env` (Settings → Secrets, or the file)
and accept the pyannote model licence at
https://huggingface.co/pyannote/speaker-diarization-3.1.

**Q: First op is slow.**
Models lazy-download on first invocation. Cache lands in the
`engine-store` volume so it survives container restarts. Pre-warm
optionally by running each op once via the UI or a small CI job
after a fresh deploy.

**Q: SSE works in Chrome but a custom client hangs.**
Sanity-check that your client isn't piped through a buffering
proxy. Caddy is configured with `flush_interval -1` on the SSE
paths and `encode` is scoped away from them — if a frame reaches
Caddy it reaches the client unbuffered.

**Q: `dc ps` says `engine` is `unhealthy` shortly after `update.sh`.**
The healthcheck `start-period` is 60s but cold-start (HF caches
empty, all extras importing) can occasionally cross that. Wait
another 30 s and re-check; if still unhealthy, `dc logs engine`
will show the actual failure.

**Q: I rebuilt the wrong service / Postgres is empty / something is broken.**
Restore from the latest backup (see §7) — non-destructive for
Caddy + certs, destructive for cache DB + artifacts, takes ~5
minutes for typical sizes.

## 12. Disaster recovery

In rough order of escalation:

1. **App-level:** restore the most recent `backup.sh` output via
   `restore.sh`. Caddy + certs preserved. ~5 min for typical data.
2. **Container-level:** `dc down && dc up -d`. Re-runs init SQL on
   fresh Postgres, re-reads secrets.env, re-mounts volumes. Use
   when state is wedged but data is intact.
3. **Host-level:** rebuild from a Hetzner Snapshot of the VPS (or
   a fresh VPS + reattach the same Hetzner Volume). Re-run
   `bash deploy/hetzner/bootstrap.sh` — it's idempotent against
   existing config and data.
4. **From-zero:** new VPS, `git clone`, `bash deploy/hetzner/bootstrap.sh`,
   then `restore.sh` from the most recent offsite backup. End-to-end
   ~30 min including model re-download.

## 13. Decommissioning

```bash
# Stop everything, delete named volumes (destructive).
cd ~/media_engine
docker compose --project-directory . \
    -f infra/docker/docker-compose.yaml \
    -f deploy/hetzner/docker-compose.override.yaml \
    down -v

# Remove the secrets + env files.
shred -u deploy/hetzner/.env deploy/hetzner/secrets.env

# Remove host hardening drop-ins (optional).
sudo rm /etc/docker/daemon.json \
        /etc/apt/apt.conf.d/52unattended-upgrades-local \
        /etc/ssh/sshd_config.d/99-media-engine-hardening.conf \
        /etc/fail2ban/jail.d/sshd-local.conf
sudo systemctl restart docker fail2ban
sudo systemctl reload ssh

# Remove the DNS A record + the Hetzner Cloud Firewall rule.
# Optionally detach + delete the Hetzner Volume + Snapshot the VPS.
```

## 14. Reference

### Files in `deploy/hetzner/`

| Path | Purpose |
|---|---|
| `bootstrap.sh` | One-shot idempotent provisioner. |
| `update.sh` | `git pull` + rebuild engine + recreate engine container only. |
| `backup.sh` | `pg_dump` + tar artifact volume → `backups/<timestamp>/`. |
| `restore.sh` | Inverse of `backup.sh`. Interactive destructive confirm. |
| `logs.sh` | `dc logs -f --tail=200`. |
| `doctor.sh` | `med doctor` passthrough. |
| `shell.sh` | `dc exec engine bash` (or `-c 'cmd'`). |
| `_lib.sh` | Shared `dc()`, `log()`, `require_env`. Sourced by the wrappers. |
| `Dockerfile.hetzner` | Override Dockerfile — full extras + chromium baked. |
| `docker-compose.override.yaml` | Caddy + secrets mount + drops public :8000. |
| `Caddyfile` | Default: SSE-scoped reverse proxy with HTTP-01 single-cert TLS. |
| `Caddyfile.wildcard` | Opt-in: same shape but DNS-01 wildcard via Cloudflare (see §3.1). |
| `Dockerfile.caddy` | Custom Caddy build (caddy:2-builder + xcaddy + caddy-dns/cloudflare). |
| `docker-compose.wildcard.yaml` | Opt-in override swapping Caddy to the wildcard build (§3.1). |
| `init/01-vector.sql` | `CREATE EXTENSION vector` — Postgres init step. |
| `daemon.json` | Docker daemon hardening. |
| `unattended-upgrades.conf` | Security-only auto-patching. |
| `sysctl-hardening.conf` | `/etc/sysctl.d/99-hardening.conf` drop-in installed by step 10. |
| `.env.example` / `.env` | Compose-level config (domain, ACME email, Postgres pw, upload cap). |
| `secrets.env.example` / `secrets.env` | API keys + tokens, mounted into the container. |
| `backups/<timestamp>/` | Created by `backup.sh`. |

### Environment variables

**`.env` (host-side, used by docker-compose):**

| Var | Default | Purpose |
|---|---|---|
| `MEDIA_ENGINE_DOMAIN` | *required* | Public domain (must DNS-resolve to the VPS). |
| `MEDIA_ENGINE_ACME_EMAIL` | *required* | Let's Encrypt notifications. |
| `POSTGRES_PASSWORD` | auto-generated | Compose's pg password. Mirrored into the engine's `MEDIA_ENGINE_DB_URL`. |
| `MEDIA_ENGINE_MAX_UPLOAD_MB` | `2048` | Cap on a single `POST /acquire/upload` body. |

**`secrets.env` (mounted into the container, auto-loaded by EngineConfig):**

| Var | Used by |
|---|---|
| `GEMINI_API_KEY` | `intelligence.extract` (gemini), `frames.analyze` (gemini), `video.multimodal` (gemini), `image.classify/describe/ocr` (gemini). **Canonical name — NOT `GOOGLE_API_KEY`.** |
| `ANTHROPIC_API_KEY` | `intelligence.extract` (claude router). |
| `OPENAI_API_KEY` | Reserved for future OpenAI-routed ops. |
| `HF_TOKEN` | `audio.diarize` (pyannote). |
| `MEDIA_ENGINE_FULLTEXT_DB_URL` | Alternate Postgres for `search.fulltext` (defaults to the main cache DB). |
| `MEDIA_ENGINE_SEMANTIC_DB_URL` | Alternate Postgres for `search.semantic`. |
