"""Tenant registry and API tokens."""

import duckdb
import pytest

from mendelea import tenancy


@pytest.fixture
def connection(tmp_path):
    conn = duckdb.connect(str(tmp_path / "auth.duckdb"))
    tenancy.init(conn)
    tenancy.create_tenant(conn, "lab-a", "Laboratory A")
    tenancy.create_tenant(conn, "lab-b", "Laboratory B")
    yield conn
    conn.close()


def test_issued_token_authenticates_its_own_tenant(connection):
    token = tenancy.issue_token(connection, "lab-a")
    assert tenancy.verify(connection, token.plaintext) == "lab-a"


def test_token_has_the_documented_shape(connection):
    token = tenancy.issue_token(connection, "lab-a")
    prefix, token_id, secret = token.plaintext.split(".")
    assert prefix == "mdl"
    assert token_id == token.token_id
    assert len(secret) >= 32


def test_the_secret_is_never_stored(connection):
    """A database dump must not yield a working credential."""
    token = tenancy.issue_token(connection, "lab-a")
    _, _, secret = token.plaintext.split(".")
    stored = connection.execute(
        "SELECT secret_sha256 FROM tenant_token WHERE token_id = ?", [token.token_id]
    ).fetchone()[0]
    assert secret not in stored
    assert stored != secret


def test_tokens_are_unique(connection):
    a = tenancy.issue_token(connection, "lab-a")
    b = tenancy.issue_token(connection, "lab-a")
    assert a.plaintext != b.plaintext
    assert a.token_id != b.token_id


def test_one_tenants_token_never_resolves_to_another(connection):
    a = tenancy.issue_token(connection, "lab-a")
    assert tenancy.verify(connection, a.plaintext) != "lab-b"


# --------------------------------------------------------------------------
# Rejection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("presented", [
    None, "", "   ",
    "garbage",
    "mdl.only-two-parts",
    "wrong.aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb",   # bad prefix
    "mdl.deadbeefdeadbeef.nonexistent-secret",   # unknown token_id
    "mdl..secret",                               # empty token_id
])
def test_malformed_or_unknown_tokens_are_rejected(connection, presented):
    assert tenancy.verify(connection, presented) is None


def test_a_tampered_secret_is_rejected(connection):
    token = tenancy.issue_token(connection, "lab-a")
    prefix, token_id, secret = token.plaintext.split(".")
    tampered = f"{prefix}.{token_id}.{secret[:-1]}{'X' if secret[-1] != 'X' else 'Y'}"
    assert tenancy.verify(connection, tampered) is None


def test_a_valid_secret_under_the_wrong_token_id_is_rejected(connection):
    a = tenancy.issue_token(connection, "lab-a")
    b = tenancy.issue_token(connection, "lab-b")
    _, _, a_secret = a.plaintext.split(".")
    assert tenancy.verify(connection, f"mdl.{b.token_id}.{a_secret}") is None


def test_revoked_token_stops_working(connection):
    token = tenancy.issue_token(connection, "lab-a")
    assert tenancy.verify(connection, token.plaintext) == "lab-a"
    assert tenancy.revoke(connection, token.token_id) is True
    assert tenancy.verify(connection, token.plaintext) is None


def test_revoking_twice_reports_no_change(connection):
    token = tenancy.issue_token(connection, "lab-a")
    tenancy.revoke(connection, token.token_id)
    assert tenancy.revoke(connection, token.token_id) is False


def test_revoking_one_token_leaves_the_others(connection):
    keep = tenancy.issue_token(connection, "lab-a")
    drop = tenancy.issue_token(connection, "lab-a")
    tenancy.revoke(connection, drop.token_id)
    assert tenancy.verify(connection, keep.plaintext) == "lab-a"


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_duplicate_tenant_is_refused(connection):
    with pytest.raises(tenancy.AuthError, match="already exists"):
        tenancy.create_tenant(connection, "lab-a")


def test_token_for_unknown_tenant_is_refused(connection):
    with pytest.raises(tenancy.AuthError, match="no such tenant"):
        tenancy.issue_token(connection, "lab-nowhere")


def test_listing_counts_active_and_revoked(connection):
    tenancy.issue_token(connection, "lab-a")
    drop = tenancy.issue_token(connection, "lab-a")
    tenancy.revoke(connection, drop.token_id)

    row = next(t for t in tenancy.list_tenants(connection) if t["tenant_id"] == "lab-a")
    assert row["active_tokens"] == 1
    assert row["revoked_tokens"] == 1


def test_token_listing_never_exposes_a_secret(connection):
    token = tenancy.issue_token(connection, "lab-a", label="laptop")
    listed = tenancy.list_tokens(connection, "lab-a")[0]
    assert listed["token_id"] == token.token_id
    assert listed["label"] == "laptop"
    assert "secret" not in listed
    assert token.plaintext.split(".")[2] not in str(listed)
