"""Tenant registry and API tokens.

Threat model
------------
Tenants are laboratories. What must never happen is one laboratory seeing
another's reported variants or review decisions. Two independent things have
to hold, and only one of them is authentication:

  1. A caller must prove which tenant they are.          <- this module
  2. Every query over tenant data must be scoped by      <- enforced by
     tenant_id, including ones nobody remembered to         tests/test_tenant_isolation.py
     scope.

The second is the one that actually broke: an unused query against
`case_variant` carried no tenant predicate while its docstring claimed to
serve one tenant. Authentication would not have caught it -- the caller was
perfectly authenticated, the query was simply wrong. So isolation is enforced
structurally, by a test that reads the source, rather than by discipline.

Token format
------------
    mdl.<token_id>.<secret>

`token_id` is public and stored in the clear so a token can be identified,
listed and revoked without knowing the secret. Only the SHA-256 of the secret
is stored, so the database cannot yield a working credential. The plaintext is
shown exactly once, at creation.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

import duckdb

TOKEN_PREFIX = "mdl"

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS tenant (
    tenant_id   VARCHAR PRIMARY KEY,
    name        VARCHAR,
    created_at  VARCHAR
);
CREATE TABLE IF NOT EXISTS tenant_token (
    token_id      VARCHAR PRIMARY KEY,
    tenant_id     VARCHAR,
    secret_sha256 VARCHAR,
    label         VARCHAR,
    created_at    VARCHAR,
    revoked_at    VARCHAR
);
"""


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class IssuedToken:
    """Returned once, at creation. The plaintext is never recoverable after."""

    tenant_id: str
    token_id: str
    plaintext: str


def init(connection) -> None:
    for statement in SCHEMA_DDL.strip().split(";"):
        if statement.strip():
            connection.execute(statement)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create_tenant(connection, tenant_id: str, name: str | None = None) -> None:
    init(connection)
    existing = connection.execute(
        "SELECT 1 FROM tenant WHERE tenant_id = ?", [tenant_id]
    ).fetchone()
    if existing:
        raise AuthError(f"tenant {tenant_id!r} already exists")
    connection.execute(
        "INSERT INTO tenant (tenant_id, name, created_at) VALUES (?, ?, ?)",
        [tenant_id, name or tenant_id, _now()],
    )


def issue_token(connection, tenant_id: str, label: str = "") -> IssuedToken:
    """Mint a token. The plaintext is returned here and never stored."""
    init(connection)
    known = connection.execute(
        "SELECT 1 FROM tenant WHERE tenant_id = ?", [tenant_id]
    ).fetchone()
    if not known:
        raise AuthError(f"no such tenant: {tenant_id!r}")

    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)

    connection.execute(
        "INSERT INTO tenant_token "
        "(token_id, tenant_id, secret_sha256, label, created_at, revoked_at) "
        "VALUES (?, ?, ?, ?, ?, NULL)",
        [token_id, tenant_id, _hash(secret), label, _now()],
    )
    return IssuedToken(
        tenant_id=tenant_id,
        token_id=token_id,
        plaintext=f"{TOKEN_PREFIX}.{token_id}.{secret}",
    )


def verify(connection, presented: str | None) -> str | None:
    """Resolve a token to its tenant_id, or None.

    Returns None for every failure mode rather than distinguishing them: a
    caller learns only that the credential did not work, never whether a
    token_id exists or has been revoked.

    A warehouse with no token table is one of those failure modes, not an
    error. The public build ships exactly that -- the evidence tables and
    nothing else -- so the case endpoint there answers 401 like any other
    bad credential instead of 500ing about a missing catalog entry. No
    tenant table is the strongest possible "this token is not valid here".
    """
    if not presented:
        return None

    parts = presented.strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    _, token_id, secret = parts

    try:
        row = connection.execute(
            "SELECT tenant_id, secret_sha256, revoked_at FROM tenant_token "
            "WHERE token_id = ?",
            [token_id],
        ).fetchone()
    except duckdb.CatalogException:
        return None
    if not row:
        return None

    tenant_id, stored_hash, revoked_at = row
    if revoked_at:
        return None

    # Constant time: a timing difference here leaks the stored hash bit by bit.
    if not hmac.compare_digest(stored_hash, _hash(secret)):
        return None

    return tenant_id


def revoke(connection, token_id: str) -> bool:
    init(connection)
    row = connection.execute(
        "SELECT revoked_at FROM tenant_token WHERE token_id = ?", [token_id]
    ).fetchone()
    if not row or row[0]:
        return False
    connection.execute(
        "UPDATE tenant_token SET revoked_at = ? WHERE token_id = ?",
        [_now(), token_id],
    )
    return True


def list_tenants(connection) -> list[dict]:
    init(connection)
    rows = connection.execute(
        """
        SELECT t.tenant_id, t.name, t.created_at,
               COUNT(k.token_id) FILTER (WHERE k.revoked_at IS NULL),
               COUNT(k.token_id) FILTER (WHERE k.revoked_at IS NOT NULL)
        FROM tenant t
        LEFT JOIN tenant_token k ON k.tenant_id = t.tenant_id
        GROUP BY t.tenant_id, t.name, t.created_at
        ORDER BY t.tenant_id
        """
    ).fetchall()
    return [
        {"tenant_id": r[0], "name": r[1], "created_at": r[2],
         "active_tokens": r[3], "revoked_tokens": r[4]}
        for r in rows
    ]


def list_tokens(connection, tenant_id: str) -> list[dict]:
    init(connection)
    rows = connection.execute(
        "SELECT token_id, label, created_at, revoked_at FROM tenant_token "
        "WHERE tenant_id = ? ORDER BY created_at",
        [tenant_id],
    ).fetchall()
    return [
        {"token_id": r[0], "label": r[1], "created_at": r[2], "revoked_at": r[3]}
        for r in rows
    ]
