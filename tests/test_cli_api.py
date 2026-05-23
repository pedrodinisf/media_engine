"""CLI surface for ``med api`` — token CRUD.

``med api start`` boots uvicorn and we don't exercise that here (it's
covered indirectly by ``test_api.py`` through ``build_app``). The token
subcommands talk to the cache directly so the first token can be
minted before any server is running.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from media_engine.cli import app


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEDIA_ENGINE_PERMANENT_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("MEDIA_ENGINE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv(
        "MEDIA_ENGINE_CACHE_DB_URL",
        f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
    )
    monkeypatch.setenv("MEDIA_ENGINE_MIN_FREE_GB", "0")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_token_create_then_list(runner: CliRunner, cli_env: Path) -> None:
    create = runner.invoke(
        app, ["api", "token", "create", "--label", "ci", "--json"]
    )
    assert create.exit_code == 0, create.stdout
    payload = json.loads(create.stdout)
    assert payload["label"] == "ci"
    assert payload["secret"]
    token_id = payload["token_id"]

    listed = runner.invoke(app, ["api", "token", "ls", "--json"])
    assert listed.exit_code == 0
    rows = json.loads(listed.stdout)
    assert any(r["id"] == token_id for r in rows)


def test_token_revoke(runner: CliRunner, cli_env: Path) -> None:
    payload = json.loads(
        runner.invoke(
            app, ["api", "token", "create", "--json"]
        ).stdout
    )
    revoke = runner.invoke(app, ["api", "token", "revoke", payload["token_id"]])
    assert revoke.exit_code == 0
    # The list should show no live tokens now (default excludes revoked).
    listed = runner.invoke(app, ["api", "token", "ls", "--json"])
    assert listed.exit_code == 0
    assert json.loads(listed.stdout) == []


def test_token_revoke_unknown_returns_nonzero(
    runner: CliRunner, cli_env: Path
) -> None:
    r = runner.invoke(app, ["api", "token", "revoke", "00000000"])
    assert r.exit_code != 0


def test_token_create_defaults_to_engine_namespace(
    runner: CliRunner, cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-003 regression: ``--namespace`` defaults to MEDIA_ENGINE_NAMESPACE.

    Before the fix, ``med api token create`` hard-coded ``"default"`` as
    the namespace default, so a token minted under a non-default engine
    config 403'd on every authed endpoint (require_token requires
    token-ns == engine-ns).
    """
    monkeypatch.setenv("MEDIA_ENGINE_NAMESPACE", "team-acme")
    create = runner.invoke(app, ["api", "token", "create", "--json"])
    assert create.exit_code == 0, create.stdout
    payload = json.loads(create.stdout)
    assert payload["namespace"] == "team-acme"


def test_token_create_explicit_namespace_wins(
    runner: CliRunner, cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-003: explicit ``--namespace`` still overrides the engine default."""
    monkeypatch.setenv("MEDIA_ENGINE_NAMESPACE", "team-acme")
    create = runner.invoke(
        app, ["api", "token", "create", "--namespace", "team-beta", "--json"]
    )
    assert create.exit_code == 0
    assert json.loads(create.stdout)["namespace"] == "team-beta"


def test_api_help(runner: CliRunner, cli_env: Path) -> None:
    r = runner.invoke(app, ["api", "--help"])
    assert r.exit_code == 0
    assert "token" in r.stdout
    assert "start" in r.stdout


def test_token_create_stdout_is_just_the_secret(
    runner: CliRunner, cli_env: Path
) -> None:
    """End-of-phase-4 gate expects ``TOKEN=$(med api token create)``.

    The default text mode must put the secret — and only the secret —
    on stdout; context lines go to stderr.
    """
    r = runner.invoke(app, ["api", "token", "create", "--label", "shell"])
    assert r.exit_code == 0
    secret = r.stdout.strip()
    assert secret, "stdout must carry the token secret"
    # The secret is a urlsafe_b64 string — pure ASCII, no whitespace,
    # no rich markup.
    assert "\n" not in secret
    assert " " not in secret
    assert "[" not in secret  # no Rich markup escaped onto stdout

    # Authenticate using the captured secret to prove it round-trips.
    from fastapi.testclient import TestClient

    from media_engine.api.app import build_app
    from media_engine.config import EngineConfig
    from media_engine.runtime.engine import Engine

    cfg = EngineConfig.load()
    cfg.validate_storage()
    with Engine.open_quick(cfg) as eng:
        app_ = build_app(engine=eng)
        with TestClient(app_) as c:
            ok = c.get(
                "/operations", headers={"Authorization": f"Bearer {secret}"}
            )
            assert ok.status_code == 200
