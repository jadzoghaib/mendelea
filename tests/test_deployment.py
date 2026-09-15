"""What has to hold before this is reachable from the internet.

Everything here guards a claim the deployment makes rather than a feature the
product has: that the public build cannot serve tenant data because it does
not contain any, that a caller cannot spend the container's CPU without limit,
and that a platform probe is cheap enough to run every few seconds.
"""

import threading
from http.server import ThreadingHTTPServer

import duckdb
import pytest
import requests

from mendelea import tenancy
from mendelea.cases import model
from mendelea.evidence import spans
from mendelea.web import server as server_module
from mendelea.web.ratelimit import RateLimiter
from mendelea.web.server import make_handler
from tests.test_policy import build


# --------------------------------------------------------------------------
# The public artefact
# --------------------------------------------------------------------------


@pytest.fixture
def full_warehouse(tmp_path):
    """A warehouse with both planes in it, as a working install has."""
    connection = build(tmp_path, [
        ("2019-01-02", [("A1", "UNCERTAIN", 1), ("A2", "UNCERTAIN", 1)]),
        ("2025-01-02", [("A1", "UNCERTAIN", 1), ("A2", "PATHOGENIC", 3)]),
    ])
    connection.execute(model.SCHEMA_DDL)
    tenancy.init(connection)
    tenancy.create_tenant(connection, "lab-a")
    token = tenancy.issue_token(connection, "lab-a").plaintext
    connection.execute(
        "INSERT INTO case_variant (tenant_id, case_ref, allele_id, gene, contig, pos,"
        " ref, alt, reported_classification, reported_on) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ["lab-a", "A-001", "A2", "TESTGENE", "17", 1000, "C", "T", "VUS", "2019-01-02"],
    )
    connection.close()
    return tmp_path / "policy.duckdb", token


def test_the_public_export_carries_the_evidence(full_warehouse, tmp_path):
    source, _ = full_warehouse
    target = tmp_path / "public.duckdb"
    counts = spans.export_public(source, target)

    assert set(counts) == set(spans.PUBLIC_TABLES)
    assert counts["assertion_span"] > 0
    assert target.exists()


def test_the_public_export_contains_no_private_table(full_warehouse, tmp_path):
    """The security claim: it cannot leak what it does not hold."""
    source, _ = full_warehouse
    target = tmp_path / "public.duckdb"
    spans.export_public(source, target)

    connection = duckdb.connect(str(target), read_only=True)
    try:
        present = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
    finally:
        connection.close()

    for private in ("case_variant", "decision_ledger", "tenant", "tenant_token"):
        assert private not in present
    # and the build intermediates, which are four times the size of the timeline
    assert "assertion_raw" not in present
    assert "allele_dim" not in present


def test_a_failed_export_leaves_no_partial_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        spans.export_public(tmp_path / "nothing.duckdb", tmp_path / "out.duckdb")
    assert list(tmp_path.iterdir()) == []


def test_exporting_over_the_source_is_refused(full_warehouse):
    """`--out` pointing at the working warehouse would destroy both private
    planes, which are the only thing here that public data cannot rebuild."""
    source, _ = full_warehouse
    with pytest.raises(ValueError, match="refusing to overwrite"):
        spans.export_public(source, source)
    # and the case plane is still there
    connection = duckdb.connect(str(source), read_only=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM case_variant").fetchone()[0] == 1
    finally:
        connection.close()


def test_the_cli_reports_the_refusal_instead_of_crashing(full_warehouse, tmp_path,
                                                         monkeypatch, capsys):
    """The guard is only useful if the person who tripped it can read it.

    `export_public` raises ValueError to refuse overwriting the working
    warehouse, and the CLI caught only FileNotFoundError, so the refusal
    arrived as a traceback -- which reads like a crash rather than a decision.
    """
    from mendelea import cli

    source, _ = full_warehouse
    monkeypatch.setenv("MENDELEA_DATA_DIR", str(source.parent))
    monkeypatch.setattr("mendelea.config.Config.warehouse",
                        property(lambda self: source))

    code = cli.main(["export-public", "--out", str(source)])
    captured = capsys.readouterr()

    assert code == 1
    assert "refusing to overwrite" in captured.err
    assert "Traceback" not in captured.err and "Traceback" not in captured.out
    # and the private planes are untouched
    connection = duckdb.connect(str(source), read_only=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM case_variant").fetchone()[0] == 1
    finally:
        connection.close()


def test_a_re_export_never_leaves_the_target_missing(full_warehouse, tmp_path):
    """The previous artefact is replaced in one step, not removed and rewritten."""
    source, _ = full_warehouse
    target = tmp_path / "public.duckdb"
    spans.export_public(source, target)
    first = target.stat().st_size
    spans.export_public(source, target)          # overwrite in place
    assert target.exists() and target.stat().st_size == first
    assert not (tmp_path / "public.duckdb.partial").exists()


# --------------------------------------------------------------------------
# A server running on the public artefact
# --------------------------------------------------------------------------


@pytest.fixture
def public_server(full_warehouse, tmp_path):
    """Exactly what a container would run: the exported warehouse, nothing else."""
    source, token = full_warehouse
    target = tmp_path / "public.duckdb"
    spans.export_public(source, target)

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(target))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", token
    finally:
        server.shutdown()
        server.server_close()


def test_the_public_build_still_serves_the_timeline(public_server):
    url, _ = public_server
    payload = requests.get(f"{url}/api/context", timeout=10).json()
    assert payload["panel"]["panel"] == "testpanel"
    assert payload["panel_headline"]["uncertain"] == 2


def test_a_real_token_gets_401_on_the_public_build(public_server):
    """Not a 500 about a missing table: a token that is valid elsewhere is
    simply not valid here, and the build says so in the ordinary way."""
    url, token = public_server
    response = requests.get(f"{url}/api/case/report",
                            headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert response.status_code == 401
    assert response.json() == {"error": "missing or invalid bearer token"}


def test_the_health_probe_answers(public_server):
    url, _ = public_server
    response = requests.get(f"{url}/health", timeout=10)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "timeline": True}


def test_a_degraded_probe_answers_503_not_200(tmp_path):
    """The status code has to carry the verdict.

    Docker's HEALTHCHECK reads the JSON body, but a platform HTTP check reads
    only the code -- so a 200 saying "degraded" is a container with no
    timeline in it passing its health check forever.
    """
    duckdb.connect(str(tmp_path / "empty.duckdb")).close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path / "empty.duckdb"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        response = requests.get(
            f"http://127.0.0.1:{server.server_address[1]}/health", timeout=10
        )
        assert response.status_code == 503
        assert response.json() == {"status": "degraded", "timeline": False}
    finally:
        server.shutdown()
        server.server_close()


def test_responses_carry_the_security_headers(public_server):
    url, _ = public_server
    headers = requests.get(f"{url}/", timeout=10).headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "default-src 'self'" in headers["Content-Security-Policy"]


# --------------------------------------------------------------------------
# The limiter
# --------------------------------------------------------------------------


def test_a_burst_is_allowed_then_refused():
    clock = [0.0]
    limiter = RateLimiter(per_minute=60, burst=5, clock=lambda: clock[0])
    assert [limiter.check("a") for _ in range(5)] == [0.0] * 5
    assert limiter.check("a") > 0, "the sixth request exceeds the burst"


def test_the_bucket_refills_with_time():
    clock = [0.0]
    limiter = RateLimiter(per_minute=60, burst=5, clock=lambda: clock[0])
    for _ in range(5):
        limiter.check("a")
    clock[0] = 2.0                      # 60/min = one token a second
    assert limiter.check("a") == 0.0
    assert limiter.check("a") == 0.0
    assert limiter.check("a") > 0.0


def test_clients_do_not_share_a_bucket():
    clock = [0.0]
    limiter = RateLimiter(per_minute=60, burst=2, clock=lambda: clock[0])
    limiter.check("a"), limiter.check("a")
    assert limiter.check("a") > 0
    assert limiter.check("b") == 0.0, "one noisy client must not throttle another"


def test_the_client_table_is_capped():
    """Otherwise rotating source addresses turns the limiter into the memory
    exhaustion it exists to prevent."""
    limiter = RateLimiter(per_minute=600, burst=2, max_clients=50)
    for i in range(500):
        limiter.check(f"client-{i}")
    assert limiter.tracked <= 51


@pytest.mark.parametrize("per_minute,burst", [
    (0, 10), (-1, 10), (60, 0),
    # Both come straight out of float() on an environment variable, and
    # `nan <= 0` is False, so the obvious guard lets them through. Every
    # comparison against nan is then false, the bucket never empties, and the
    # limiter is silently absent while appearing configured.
    (float("nan"), 10), (float("inf"), 10),
])
def test_a_useless_configuration_is_refused_at_construction(per_minute, burst):
    """A rate of zero divided by zero on the first refusal, turning a
    mistyped environment variable into a 500 on every request."""
    with pytest.raises(ValueError):
        RateLimiter(per_minute=per_minute, burst=burst)


def test_a_non_finite_rate_would_have_disabled_the_limiter(monkeypatch):
    """The reason the check above is worth its line: prove the failure mode."""
    monkeypatch.setattr("mendelea.web.ratelimit.math.isfinite", lambda _: True)
    limiter = RateLimiter(per_minute=float("nan"), burst=1)
    assert [limiter.check("a") for _ in range(20)] == [0.0] * 20, (
        "with the guard bypassed a nan rate lets everything through, "
        "which is what the guard exists to stop"
    )


def test_waiting_does_not_bank_a_bigger_burst():
    clock = [0.0]
    limiter = RateLimiter(per_minute=60, burst=3, clock=lambda: clock[0])
    clock[0] = 10_000.0                 # idle for hours
    assert [limiter.check("a") for _ in range(3)] == [0.0] * 3
    assert limiter.check("a") > 0, "the bucket caps at the burst, however long the wait"


# --------------------------------------------------------------------------
# The limiter, wired in
# --------------------------------------------------------------------------


@pytest.fixture
def throttled(full_warehouse, tmp_path, monkeypatch):
    source, _ = full_warehouse
    target = tmp_path / "public.duckdb"
    spans.export_public(source, target)
    # A tenth of a token per second, not one. At one per second the test only
    # produced a 429 if eight sequential HTTP round trips finished inside a
    # second, which is true on this laptop and not on a loaded CI box.
    monkeypatch.setattr(server_module, "RATE_PER_MINUTE", 6.0)
    monkeypatch.setattr(server_module, "RATE_BURST", 3)

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(target))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_flood_is_refused_with_429_and_retry_after(throttled):
    codes = [requests.get(f"{throttled}/api/context", timeout=10).status_code
             for _ in range(8)]
    assert codes[0] == 200
    assert 429 in codes, f"nothing was throttled: {codes}"

    refused = requests.get(f"{throttled}/api/context", timeout=10)
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1
    assert refused.json() == {"error": "too many requests"}


def test_the_health_probe_is_never_throttled(throttled):
    """A platform polls it far more often than a human browses, and a probe
    answering 429 is a container that gets restarted for being popular."""
    for _ in range(12):
        requests.get(f"{throttled}/api/context", timeout=10)
    assert requests.get(f"{throttled}/health", timeout=10).status_code == 200
