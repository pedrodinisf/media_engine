# Phase 7 — acoustic speaker identity

Adds the **acoustic** speaker path on top of Phase 5's name-based
`speakers.identify`. Bumps the engine to **v0.8.0**.

Phase 5 resolves diarization clusters to names by fuzzy-matching transcript
text against a CSV — it needs someone to *say* their name and its `SPEAKER_00`
ids are per-recording. Phase 7 derives a voice fingerprint per diarization turn,
clusters fingerprints across recordings so the same voice gets one **stable**
`Speaker_<sha8>` id with no name database, and matches new voices against a saved
fingerprint DB by cosine similarity.

Three new capability-named ops (name-based `speakers.identify` is unchanged and
stays reachable):

- **`speakers.embed_voice`** — (Audio + Diarization) → `SpeakerEmbedding`. One
  voice vector per diarization turn (pyannote embedding model), packaged as a
  single per-recording artifact.
- **`speakers.cluster`** — `SpeakerEmbedding…` → `SpeakerProfile…`. HDBSCAN over
  L2-normed vectors; each cluster is reconciled to a stable id (reuse a saved
  voice at cosine ≥ `reconcile_threshold` via a running-mean centroid, else mint
  from the centroid hash).
- **`speakers.match`** — `SpeakerEmbedding` → `Analysis`. Ranks saved
  `SpeakerProfile`s by best-turn cosine similarity ("whose saved voice does this
  sound like?").

New artifact kinds: `SpeakerEmbedding`, `SpeakerProfile`.

## Identity model

Two ids are kept separate on purpose:

- **`speaker_id`** (`Speaker_<sha8>`) — a *stable, mutable-state label*. Minted
  once for a new voice, then frozen while its stored centroid keeps evolving.
- **artifact `id`** (sha256) — content-addressed per run. A profile re-emitted
  after its centroid updates gets a new artifact id but the same `speaker_id`.

`speakers.cluster` reconciles each new cluster against the persisted profiles
(greedy, one-to-one): cosine ≥ threshold → reuse that id and fold the new
vectors into the stored centroid as a running mean; below → mint. With storage
disabled the reconcile step is skipped and ids are minted deterministically from
the centroid hash — so re-running on the same inputs is a cache hit.

## Privacy (opt-in per namespace)

Voice fingerprints are biometric, so both gates default **off**:

- `speaker_storage_enabled` (`MEDIA_ENGINE_SPEAKER_STORAGE_ENABLED`) — persisting
  `SpeakerProfile` centroids to the fingerprint DB, and therefore reconciliation.
  Ops still run and return profiles; they just don't write.
- `speaker_export_enabled` (`MEDIA_ENGINE_SPEAKER_EXPORT_ENABLED`) — the
  `speakers.*` acoustic ops over REST `/run` (403 when off). MCP already hides
  them (they're not in the read-only default allow-set).
- `med speakers purge [--namespace NS] --yes` — hard-delete a namespace's
  artifacts, runs, and voice fingerprints (`Cache.purge_namespace`).

## Storage

The fingerprint store is a self-managed SQLite sidecar at
`permanent_store/speakers/fingerprints.db` (mirrors the `search/semantic.db`
precedent) — but with a `namespace` column so per-namespace purge works. When
`MEDIA_ENGINE_SPEAKER_DB_URL` points at Postgres, `speakers.cluster` also mirrors
profiles to a `speaker_profiles_pgv` table that the `pgvector` match backend
reads.

## Dependencies

- New extra `cluster = ["hdbscan", "scikit-learn", "numpy"]`.
- `speakers.embed_voice` reuses the existing `diarize` extra (pyannote 4.x).

## Shipped (commits, newest first)

| Commit | Surface |
|---|---|
| `feat(speakers): docs, example profile, version bump` | C4 — `docs/phase-7.md`, `profiles/examples/speaker-id.yaml`, CLAUDE.md/CHANGELOG/README, v0.8.0. |
| `feat(speakers): privacy + med speakers CLI` | C4 — config gates, `Cache.purge_namespace`, REST 403 gate, MCP hidden-by-default test, `med speakers` group (embed-voice/cluster/match/purge). |
| `feat(speakers): speakers.cluster + speakers.match ops & backends` | C3 — HDBSCAN clustering with reconcile-to-stable-id, sqlite+pgvector match backends, `cluster` extra. |
| `feat(speakers): speakers.embed_voice op + pyannote backend` | C2 — per-turn voice embedding, model-pool + MPS. |
| `feat(speakers): Phase-7 foundations` | C1 — new Kinds/artifacts, shared `backends/_vec.py`, fingerprint store, `stable_speaker_id`/`running_mean`/`reconcile`. |

## Tests

- `tests/test_speakers_fingerprint.py` — pure id-determinism, running-mean,
  reconcile reuse/mint/one-to-one, store namespace isolation + purge.
- `tests/test_op_speakers_embed_voice.py` + `tests/test_backend_embed_voice_pyannote.py`
  — op contract + `needs_pyannote` real embedding path.
- `tests/test_op_speakers_cluster.py` — real HDBSCAN, cache-hit-with-storage-off,
  persist-and-reconcile-reuse across two recordings.
- `tests/test_op_speakers_match.py` — ranking, empty-DB, namespace scoping,
  cluster→match end-to-end.
- `tests/test_speakers_privacy.py` — purge isolation, REST 403 gate, MCP hidden.
- `tests/test_cli_speakers.py` — `med speakers` group smoke.

Markers: `needs_hdbscan` (clustering real-lib), `needs_pyannote` (embedding),
`needs_postgres` (pgvector path).
