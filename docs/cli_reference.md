# CLI Reference — `med`

This is the exhaustive reference for the `med` command-line. Every
subcommand listed by `med --help` is documented here with synopsis,
options, and a short example. Run `med <subcommand> --help` for the
authoritative flag list (the CLI is Typer-driven and always
self-describing); this doc exists to give you a one-page survey.

> **Global options** (`med [OPTIONS] COMMAND ...`):
> `--config PATH` — point at a specific `config.toml`.
> `--namespace TEXT` — multi-tenant namespace override (cache is
> namespace-scoped; same artifact id in two namespaces lives twice).
> `--json` — emit machine-readable JSON instead of Rich tables.
> `--dry-run` — print a cost preview; don't actually run the op.
> `--verbose` / `--quiet` — log level overrides.

## Catalog & inspection

| Command         | Purpose                                              |
| --------------- | ---------------------------------------------------- |
| `med ops`       | List all 38 registered ops, kinds, default backends. |
| `med config`    | Print the effective `EngineConfig` (post env-var merge). |
| `med ls`        | List artifacts in the cache (paginated; filter by kind / namespace). |
| `med show <id>` | Pretty-print one artifact's metadata + lineage refs. |
| `med lineage <id> [--depth N]` | Render the upstream lineage tree.       |

```bash
uv run med ops
uv run med lineage 7f3a2c... --depth 4
```

## Ingestion

| Command                                    | Purpose                                         |
| ------------------------------------------ | ----------------------------------------------- |
| `med acquire <local-file>`                  | Ingest a local file (`acquire.upload`).        |
| `med acquire-url <url> [--quality] [--backend]` | Fetch a remote video (`acquire.url`).      |
| `med acquire-live <url> [--max-duration N] [--segment-seconds N] [--hotkey ...]` | Record an HLS stream (`acquire.livestream`). |
| `med extract-audio <video-id>`             | Shortcut for `video.extract_audio`.            |

```bash
uv run med acquire ~/Movies/talk.mp4
uv run med acquire-url 'https://www.youtube.com/watch?v=...' --quality 480p
uv run med acquire-live 'https://stream.example.com/index.m3u8' \
    --max-duration 3600 --segment-seconds 900
```

## Single-op execution

| Command  | Purpose                                              |
| -------- | ---------------------------------------------------- |
| `med run <op> [--input ID] [--param K=V] [--backend B] [--schema P] [--yes]` | Run any registered op. Prints a cost preview first; `--yes` skips the prompt. |
| `med batch <file> [--op] [--input-arg] [--param]` | Fan one op over a list of inputs through the DAG executor. |

```bash
uv run med run intelligence.summarize --input t-2c4d... \
    --param model=mlx-community/Qwen2.5-7B-Instruct-4bit --yes
uv run med batch ids.txt --op embed.text --input-arg artifact

# Phase 6.7 — full video comprehension in one node (Apple Silicon
# defaults; swap vlm_model to a gemini-* id on Linux):
uv run med run video.comprehend \
    --input <video-id> \
    --param fps=1.0 \
    --param synth_model=gemini-2.5-flash \
    --param style=lecture \
    --param output_kind=structured
```

## Profiles

| Command  | Purpose                                                |
| -------- | ------------------------------------------------------ |
| `med profile ls`          | List discovered profiles (config dir + repo dir + extras). |
| `med profile show <name>` | Print the parsed profile (graph / frontmatter).         |
| `med profile run <name> --input <id> [--param k=v] [--backend b]` | Execute a profile through the DAG executor. |

```bash
uv run med profile ls
uv run med profile run analysis-full --input v-91a3...
uv run med profile run general-custom --input v-91a3... \
    --param system_prompt="Summarize as a recipe card."
```

## Search

| Command  | Purpose                                                |
| -------- | ------------------------------------------------------ |
| `med search "<query>" [--mode fulltext|semantic|hybrid] [--top-k N] [--kind K] [--refresh]` | Query the catalog. `hybrid` blends BM25 with embeddings. |

```bash
uv run med search "speaker discussing logistics" --mode hybrid --top-k 10
uv run med search "Acme Corp" --mode fulltext --refresh
```

## Cost ledger & events

| Command  | Purpose                                                |
| -------- | ------------------------------------------------------ |
| `med cost summary [--since DATE] [--until DATE] [--by-op|--by-backend|--by-namespace]` | Actuals from the `cost_log`. |
| `med cost ls [--limit N]` | Recent spend rows.                          |
| `med events tail [--filter PATTERN]` | Live event stream (Progress, LogLine, error). |
| `med events history --since DATE`    | Replay historical events.                 |

```bash
uv run med cost summary --since 2026-05-01 --by-op
uv run med events tail
```

## Daemon

| Command  | Purpose                                                |
| -------- | ------------------------------------------------------ |
| `med daemon start [--detach]` | Boot a long-running engine for fast reuse. |
| `med daemon status`           | Health + uptime + pid.                     |
| `med daemon stop`             | Graceful shutdown.                         |
| `med daemon logs [--tail N]`  | Daemon log tail.                           |

```bash
uv run med daemon start --detach
uv run med daemon status
```

When the daemon is running, `med` subcommands transparently RPC into it
instead of cold-starting an Engine — cuts startup from seconds to
milliseconds and reuses warm ML models.

## REST API

| Command  | Purpose                                                |
| -------- | ------------------------------------------------------ |
| `med api start [--host] [--port] [--reload]` | Boot the FastAPI app on `http://host:port` (headless — no `/ui` mount). |
| `med api token create [--name N] [--scopes ...]` | Mint a bearer token.       |
| `med api token ls`                  | List active tokens (id, name, last-used).   |
| `med api token revoke <id>`         | Invalidate a token.                         |

See [`docs/api_reference.md`](api_reference.md) for the endpoint surface.

```bash
uv run med api token create --name laptop
uv run med api start --host 0.0.0.0 --port 8000
```

## Web UI (Phase 6)

| Command  | Purpose                                                |
| -------- | ------------------------------------------------------ |
| `med web start [--host] [--port] [--open/--no-open]` | Boot the FastAPI app *with* the SvelteKit SPA mounted at `/ui`. Same uvicorn under the hood as `med api start`; the difference is the validated dist tree + the optional browser auto-open. |

`--open` auto-detects a display (`DISPLAY` / `WAYLAND_DISPLAY` on
Linux; default-true on macOS/Windows; `MEDIA_ENGINE_NO_BROWSER=1`
forces off). The SPA's built `media_engine/web/dist/` tree is shipped
inside the wheel (`hatch force-include`) — a `pip install
media_engine[api]` ships the UI for free. Developers from source
need a one-time `pnpm -C web install && pnpm -C web build`.

```bash
uv run med api token create --label web-ui   # paste into /ui/setup
uv run med web start --open                  # opens browser at /ui/setup
```

See [`docs/web_ui.md`](web_ui.md) for the panel-by-panel tour.

## MCP (LLM client integration)

| Command  | Purpose                                                |
| -------- | ------------------------------------------------------ |
| `med mcp tools-json`                | Emit the MCP tool schema (per op).         |
| `med mcp serve [--allow OP] [--deny OP]` | Run the stdio server. Default policy is read-only: only `search.*` exposed. |

```bash
# Read-only — safe to wire into any LLM client
uv run med mcp serve

# Expose specific write-capable ops:
uv run med mcp serve --allow audio.transcribe --allow intelligence.summarize
```

## Database

| Command  | Purpose                                                |
| -------- | ------------------------------------------------------ |
| `med db migrate [--db-url URL]`                   | `alembic upgrade head` against the configured cache (sqlite or postgres). |
| `med db dump-sqlite-to-postgres --to <url>`        | One-shot SQLite → Postgres copy with pre/post sha256 verification. |

```bash
export MEDIA_ENGINE_DB_URL=postgresql+psycopg://user:pw@host/db
uv run med db migrate
```

## Speakers (Phase 7)

Acoustic speaker identity — voice fingerprints → stable cross-recording ids.
Storage is opt-in per namespace (`speaker_storage_enabled`); the acoustic ops
are hidden from MCP and gated off REST unless `speaker_export_enabled`.

| Command  | Purpose                                                |
| -------- | ------------------------------------------------------ |
| `med speakers embed-voice <audio-id> --diarization <diar-id>` | Embed each diarization turn into a voice fingerprint (`speakers.embed_voice`). |
| `med speakers cluster <embedding-id...>` | Cluster fingerprints into stable `Speaker_<sha8>` profiles (`speakers.cluster`). |
| `med speakers match <embedding-id> [--top-k N]` | Rank saved voices by similarity to a query (`speakers.match`). |
| `med speakers purge [--namespace NS] --yes` | Hard-delete a namespace's artifacts, runs, and voice fingerprints. |

```bash
uv run med run audio.transcribe_diarized --input <audio-id>   # get a Diarization
uv run med speakers embed-voice <audio-id> --diarization <diar-id>
uv run med speakers cluster <embedding-id>
uv run med speakers match <embedding-id> --top-k 5
```

## Storage

| Command  | Purpose                                                |
| -------- | ------------------------------------------------------ |
| `med storage stats`                           | Bytes-by-kind + free space on the permanent store. |
| `med storage gc [--apply]`                    | Workdir sweep + LRU eviction. `--apply` actually deletes; the default is a preview. Eviction honored only when `eviction_enabled = true` in config. |
| `med storage migrate --from <a> --to <b>`     | Rewrite the `permanent_store` path prefix in the cache after moving files. |

```bash
uv run med storage stats
uv run med storage gc                           # preview
uv run med storage gc --apply
```

## Operational

| Command  | Purpose                                                |
| -------- | ------------------------------------------------------ |
| `med health`           | Liveness — always succeeds when the process is up. |
| `med ready`            | Readiness — non-zero exit when any dependency is down. |
| `med doctor [--op N] [--json]` | Walk every op + backend, evaluate `BackendRequirements` against the current env, print a green/red matrix. `--op` filters to one op or a prefix (`audio.`). `--json` emits the structured report. Exits non-zero when any op has no working backend (CI-gateable). |

Used by Kubernetes/Docker probes. The REST surface exposes the same as
`/health` and `/ready` (see API reference).

`med doctor` is the answer to "I tried to run X and got an opaque error":
it surfaces the op→backend→dep contract that `BackendRequirements`
declares but the engine doesn't actively enforce at startup. Run it after
`uv sync` or in a fresh container to see which ops are usable.

## Exit codes

| Code | Meaning                                  |
| ---- | ---------------------------------------- |
| `0`  | Success.                                  |
| `1`  | Generic error (bad args, op failure, etc.). |
| `2`  | Usage error from Typer.                   |
| `3`  | Cost-preview confirmation declined (`med run` without `--yes`). |
| `4`  | Readiness check failed (`med ready`).     |

## Config & env vars

The CLI loads `~/.config/media_engine/config.toml` (path overridable via
`--config`), then layers `MEDIA_ENGINE_*` env vars on top. The
authoritative key list is `med config` (which prints the merged result)
and the `EngineConfig` Pydantic model in `media_engine/config.py`.

Common keys (config.toml key ↔ `MEDIA_ENGINE_*` env var):

| Key | Purpose |
| --- | ------- |
| `permanent_store` | Where artifacts + `cache.db` live. Point this at a roomy volume to keep large files off a small system drive; the disk guard checks *this* filesystem. |
| `workdir` | Per-job scratch (extracted audio, sampled frames, HLS segments). Put it on the same big volume so a large job can't fill the system drive. |
| `min_free_gb` | Pre-op disk guard (default 20). The engine refuses to start an op when `permanent_store` has less than this free. |
| `speaker_storage_enabled` / `speaker_export_enabled` | Phase-7 biometric gates — persist voice fingerprints / expose acoustic `speakers.*` ops over REST. Both default off. |

> Model weights are cached wherever `HF_HOME` points (HuggingFace's own env
> var); if it's unset, the engine defaults it to `<models_dir>/huggingface`
> under `permanent_store`. Set `HF_HOME` to a large volume to keep multi-GB
> model downloads off the system drive.
