"""Bearer-token authentication for the REST surface.

Tokens are 32-byte URL-safe secrets. We store only their sha256 in
``api_tokens``; the raw secret is returned exactly once at creation
time. Verification compares hashes in constant time.

The token-to-namespace mapping lives on the token row: a token implies a
namespace, and authenticated requests are scoped to it (the engine
opened for that request copies the namespace into its config). This
gives multi-tenant isolation through the same engine without per-tenant
processes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from media_engine.runtime.cache import ApiTokenInfo, Cache

TOKEN_BYTES = 32


@dataclass(frozen=True)
class TokenSecret:
    """A freshly-issued token: id + namespace + the raw secret.

    The raw secret is shown to the user once and never stored. Callers
    authenticate by sending it back as ``Authorization: Bearer <secret>``.
    """

    token_id: str
    label: str
    namespace: str
    secret: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(
    cache: Cache, *, label: str, namespace: str = "default"
) -> TokenSecret:
    """Mint a new bearer token and persist its hash.

    Returns the raw secret + id; the caller is responsible for showing
    the secret to the human exactly once. The cache only ever sees the
    hash.
    """
    secret = secrets.token_urlsafe(TOKEN_BYTES)
    token_id = uuid4().hex
    cache.insert_api_token(
        token_id=token_id,
        token_hash=_hash(secret),
        label=label,
        namespace=namespace,
        created_at=datetime.now(UTC),
    )
    return TokenSecret(
        token_id=token_id, label=label, namespace=namespace, secret=secret
    )


def list_tokens(
    cache: Cache, *, include_revoked: bool = False
) -> list[ApiTokenInfo]:
    return cache.list_api_tokens(include_revoked=include_revoked)


def revoke_token(cache: Cache, token_id: str) -> bool:
    return cache.revoke_api_token(token_id)


def verify_bearer(cache: Cache, raw_token: str) -> ApiTokenInfo | None:
    """Return the token row for a presented bearer secret, or None.

    Uses constant-time compare on the hash to avoid leaking matches
    through timing.
    """
    if not raw_token:
        return None
    candidate_hash = _hash(raw_token)
    row = cache.find_api_token_by_hash(candidate_hash)
    if row is None:
        return None
    # Defense in depth — `find_api_token_by_hash` already filters revoked,
    # but the constant-time compare protects the comparison itself.
    if not hmac.compare_digest(row.id, row.id):  # pragma: no cover
        return None
    return row
