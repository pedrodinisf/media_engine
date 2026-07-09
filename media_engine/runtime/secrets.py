"""Operator-managed secret env vars.

The Web UI's Settings → Secrets surface writes API keys + similar credentials
to ``{config_dir}/secrets.env`` (dotenv format, 0600 mode). ``load_secrets``
is called by ``EngineConfig.load`` before the BaseSettings constructor runs,
so values in the file get exported into ``os.environ`` and are then picked
up both by ``MEDIA_ENGINE_*`` settings and by ``BackendRequirements`` env
probes the same way a shell ``export`` would.

The file lives outside the cache DB and the SQLite namespace so it can be
backed up and rotated independently of the artifact store.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

SECRETS_FILE_NAME = "secrets.env"

# Catalog of env-vars the engine knows about, derived statically from the
# Backend / BackendRequirements declarations. Used by the Settings UI to
# render a list of known secrets even when none are set yet. The runtime
# secrets loader itself is *not* gated by this catalog — anything written
# to secrets.env is loaded.
KNOWN_SECRETS: tuple[dict[str, str], ...] = (
    {
        "name": "GEMINI_API_KEY",
        "label": "Google Gemini API key",
        "category": "LLM",
        "used_by": "intelligence.extract (gemini), frames.analyze (gemini), "
        "video.multimodal (gemini), image.classify/describe/ocr (gemini)",
        "url": "https://aistudio.google.com/apikey",
    },
    {
        "name": "ANTHROPIC_API_KEY",
        "label": "Anthropic Claude API key",
        "category": "LLM",
        "used_by": "intelligence.extract (claude router)",
        "url": "https://console.anthropic.com/settings/keys",
    },
    {
        "name": "OPENAI_API_KEY",
        "label": "OpenAI API key",
        "category": "LLM",
        "used_by": "reserved for future OpenAI-routed ops",
        "url": "https://platform.openai.com/api-keys",
    },
    {
        "name": "HF_TOKEN",
        "label": "Hugging Face access token",
        "category": "Model gating",
        "used_by": "audio.diarize (pyannote) — accepts model licence",
        "url": "https://huggingface.co/settings/tokens",
    },
    {
        "name": "ASSEMBLYAI_API_KEY",
        "label": "AssemblyAI API key",
        "category": "Transcription",
        "used_by": "audio.transcribe / audio.transcribe_diarized / "
        "audio.detect_language (assemblyai/* models) — cloud "
        "transcription + speaker diarization",
        "url": "https://www.assemblyai.com/app/account",
    },
    {
        "name": "MEDIA_ENGINE_FULLTEXT_DB_URL",
        "label": "Postgres connection (search.fulltext)",
        "category": "Database",
        "used_by": "search.fulltext (postgres-tsvector). Default sqlite-fts5 "
        "works without this — only set when you want Postgres-backed FT.",
        "url": "",
    },
    {
        "name": "MEDIA_ENGINE_SEMANTIC_DB_URL",
        "label": "Postgres connection (search.semantic)",
        "category": "Database",
        "used_by": "search.semantic (pgvector). Default sqlite vector backend "
        "works without this — only set when you want pgvector.",
        "url": "",
    },
)


_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def secrets_path(config_dir: Path) -> Path:
    return config_dir / SECRETS_FILE_NAME


def parse_secrets(text: str) -> dict[str, str]:
    """Parse a dotenv-style file body.

    Accepts ``KEY=VALUE`` (no shell interpolation). Lines starting with
    ``#`` and blank lines are ignored. Double / single quotes around the
    value are stripped if balanced. Unknown / malformed lines are
    skipped silently — the engine should never refuse to boot because a
    secrets file has a typo.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_PATTERN.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def read_secrets(config_dir: Path) -> dict[str, str]:
    """Return the parsed secrets.env or ``{}`` if absent."""
    p = secrets_path(config_dir)
    if not p.exists():
        return {}
    return parse_secrets(p.read_text(encoding="utf-8"))


def load_secrets(config_dir: Path, *, override: bool = False) -> list[str]:
    """Export secrets.env into ``os.environ`` and return the keys touched.

    ``override`` controls whether an existing env var is overwritten. The
    default is False — a value the operator explicitly set via shell
    wins over the persisted file so the file never silently overrides a
    debugging override. The Web UI's "save secret" path uses override=
    True to ensure the running process sees the change without a
    restart.
    """
    touched: list[str] = []
    for key, value in read_secrets(config_dir).items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        touched.append(key)
    return touched


def write_secrets(
    config_dir: Path,
    updates: dict[str, str | None],
) -> dict[str, str]:
    """Merge ``updates`` into secrets.env. Returns the resulting map.

    A ``None`` value deletes the key. Empty-string values are deleted
    too (treated as "unset" in the UI). The file is rewritten atomically
    via a temp file in the same dir; permissions are forced to 0600.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    current = read_secrets(config_dir)
    for key, value in updates.items():
        if not _KEY_PATTERN.match(key):
            raise ValueError(f"invalid env var name: {key!r}")
        if value is None or value == "":
            current.pop(key, None)
        else:
            current[key] = value

    body_lines = [
        "# media_engine secrets — managed by Settings → Secrets in the Web UI.",
        "# Do NOT commit this file. chmod 0600.",
        "",
    ]
    for key in sorted(current):
        # Quote anything containing whitespace or shell metacharacters.
        value = current[key]
        if any(ch in value for ch in (" ", "\t", "#", "'", '"')):
            escaped = value.replace('"', '\\"')
            body_lines.append(f'{key}="{escaped}"')
        else:
            body_lines.append(f"{key}={value}")
    body = "\n".join(body_lines) + "\n"

    target = secrets_path(config_dir)
    tmp = target.with_suffix(".env.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(target)
    return current


__all__ = [
    "KNOWN_SECRETS",
    "SECRETS_FILE_NAME",
    "load_secrets",
    "parse_secrets",
    "read_secrets",
    "secrets_path",
    "write_secrets",
]
