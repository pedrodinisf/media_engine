"""MCP stdio server — in-memory transport tests.

Uses ``mcp.shared.memory.create_connected_server_and_client_session``
so we drive the server end-to-end without spawning a subprocess and
without filesystem sockets.

Covers the three protocol pieces that matter for production use:
- ``tools/list`` honors the allow-list (default = read-only).
- ``tools/call`` rejects denied ops with a clear error.
- ``resources/list`` + ``resources/read`` round-trip artifact ids.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from media_engine.config import EngineConfig
from media_engine.mcp.server import (
    DEFAULT_ALLOWED_OPS,
    MCPSecurityConfig,
    _parse_resource_uri,
    _resource_uri,
    _tool_name_to_op_name,
    build_mcp_server,
)
from media_engine.runtime.engine import Engine


@pytest.fixture
def engine(tmp_path: Path):
    cfg = EngineConfig(
        permanent_store=tmp_path / "store",
        workdir=tmp_path / "work",
        config_dir=tmp_path / "config",
        cache_db_url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
        min_free_gb=0,
    )
    with Engine.open_quick(cfg) as e:
        yield e


def test_uri_helpers_round_trip() -> None:
    uri = _resource_uri("video", "abc" * 21 + "x")
    kind, aid = _parse_resource_uri(str(uri))
    assert kind == "video"
    assert aid == "abc" * 21 + "x"


def test_tool_name_round_trip() -> None:
    assert _tool_name_to_op_name("audio__transcribe") == "audio.transcribe"


def test_security_default_is_read_only() -> None:
    sec = MCPSecurityConfig()
    assert sec.is_allowed("search.semantic")
    assert not sec.is_allowed("acquire.upload")
    assert not sec.is_allowed("audio.transcribe")


def test_security_explicit_allow_overrides_default() -> None:
    sec = MCPSecurityConfig(allowed_ops=frozenset({"acquire.upload"}))
    assert sec.is_allowed("acquire.upload")
    assert not sec.is_allowed("search.semantic")


def test_security_deny_wins() -> None:
    sec = MCPSecurityConfig(
        allowed_ops=frozenset({"acquire.upload"}),
        deny_ops=frozenset({"acquire.upload"}),
    )
    assert not sec.is_allowed("acquire.upload")


def test_security_none_means_expose_everything() -> None:
    sec = MCPSecurityConfig(allowed_ops=None)
    assert sec.is_allowed("audio.transcribe")
    assert sec.is_allowed("acquire.upload")


# ─────────────────────────────────────────────────────────────────
# Live transport (in-memory)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tools_list_default_allow_list(engine: Engine) -> None:
    server = build_mcp_server(engine)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()
    exposed = {tool.name for tool in result.tools}
    # Default read-only set surfaces search.* but no writeful ops.
    assert exposed == {
        "search__semantic",
        "search__fulltext",
        "search__hybrid",
    }
    assert {
        "search.semantic",
        "search.fulltext",
        "search.hybrid",
    } == DEFAULT_ALLOWED_OPS


@pytest.mark.asyncio
async def test_tools_list_expanded_allow_list(engine: Engine) -> None:
    sec = MCPSecurityConfig(allowed_ops=None, deny_ops=frozenset())
    server = build_mcp_server(engine, security=sec)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()
    names = {tool.name for tool in result.tools}
    # 31 ops = at least the writeful ones plus search.*.
    assert "acquire__upload" in names
    assert "audio__transcribe" in names


@pytest.mark.asyncio
async def test_call_denied_op_raises_clear_error(engine: Engine) -> None:
    """A denied op must be rejected — and not just silently ignored."""
    server = build_mcp_server(engine)  # default = read-only
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "acquire__upload",
            arguments={"source_path": "/tmp/does-not-matter"},
        )
    # The SDK surfaces server-side exceptions as a CallToolResult with
    # isError=True. We expect that flag and an error class hint in the
    # message text.
    assert result.isError
    assert any(
        "allow-list" in block.text.lower() or "permission" in block.text.lower()
        for block in result.content
        if hasattr(block, "text")
    )


@pytest.mark.asyncio
async def test_resources_list_returns_persisted_artifacts(
    engine: Engine, tmp_path: Path
) -> None:
    # Plant a tiny artifact via acquire.upload so the cache has something.
    src = tmp_path / "blob.bin"
    src.write_bytes(b"\x00\x01\x02\x03")
    # We can't classify a random blob — instead build a synthetic Image-
    # like artifact directly via the engine cache. The MCP layer reads
    # what the cache has; the actual op doesn't matter here.
    from datetime import UTC, datetime

    from media_engine.artifacts import Kind
    from media_engine.artifacts.text import MarkdownArtifact

    fake_id = "a" * 64
    artifact = MarkdownArtifact(
        id=fake_id,
        kind=Kind.MarkdownArtifact,
        path=str(src),  # type: ignore[arg-type]
        metadata={"title": "demo"},
        derived_from=(),
        produced_by=None,
        created_at=datetime.now(UTC),
    )
    engine.cache.upsert_artifact(artifact)

    server = build_mcp_server(engine)
    async with create_connected_server_and_client_session(server) as client:
        listed = await client.list_resources()

    uris = [str(r.uri) for r in listed.resources]
    assert any(fake_id in uri for uri in uris)


@pytest.mark.asyncio
async def test_resources_read_returns_json_payload(
    engine: Engine, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    from media_engine.artifacts import Kind
    from media_engine.artifacts.text import MarkdownArtifact

    fake_id = "b" * 64
    src = tmp_path / "blob.md"
    src.write_text("# hi", encoding="utf-8")
    engine.cache.upsert_artifact(
        MarkdownArtifact(
            id=fake_id,
            kind=Kind.MarkdownArtifact,
            path=str(src),  # type: ignore[arg-type]
            metadata={"title": "demo"},
            derived_from=(),
            produced_by=None,
            created_at=datetime.now(UTC),
        )
    )

    server = build_mcp_server(engine)
    async with create_connected_server_and_client_session(server) as client:
        # Pydantic AnyUrl rejects custom schemes via the URL field type
        # ``ReadResourceRequest`` uses, so we send the string form which
        # the client model converts. The SDK wraps it for us.
        result = await client.read_resource(
            uri=f"media://markdown/{fake_id}"  # type: ignore[arg-type]
        )
    assert result.contents
    first = result.contents[0]
    assert hasattr(first, "text")
    payload = json.loads(first.text)  # type: ignore[union-attr]
    assert payload["id"] == fake_id
    assert payload["kind"] == "markdown"
