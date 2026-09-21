"""What has to hold before this is reachable from the internet.

Everything here guards a claim the deployment makes rather than a feature the
product has: that the public build cannot serve tenant data because it does
not contain any, that a caller cannot spend the container's CPU without limit,
and that a platform probe is cheap enough to run every few seconds.
"""

import base64
import re
import threading
import time
from http.server import ThreadingHTTPServer
from xml.etree import ElementTree

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
# The tab icon
# --------------------------------------------------------------------------


def _commands(d):
    """Every command letter in a path, whether or not _visits handles it."""
    return re.findall(r"[A-Za-z]", d)


def _icon_paths(page):
    """The `d` attribute of every path in the page's inline icon.

    Decoding proves the base64 survived editing, and parsing proves the
    result is still XML: a truncated payload gives a browser a broken icon
    and gives a reader of the HTML no clue at all.
    """
    match = re.search(
        r'<link rel="icon" href="data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)"', page
    )
    assert match, "the served page carries no inline icon"
    root = ElementTree.fromstring(base64.b64decode(match.group(1)))
    assert root.tag.endswith("svg")
    assert root.get("viewBox") == "0 0 32 32"
    paths = [node.get("d") for node in root.iter() if node.tag.endswith("path")]
    # Every test below indexes this list by position: [0] and [1] are the two
    # strands, [2] the rungs. Without this, inserting a path ahead of the
    # rungs makes the rung test measure the new path instead -- passing if
    # that path happens to sit inside the strand bounds, which is the same
    # silent false confidence the width test exists to prevent.
    assert len(paths) == 3, f"expected two strands and one rung path, got {len(paths)}"
    for d in paths:
        unhandled = sorted(set(_commands(d)) - set("MQh"))
        # Without this, an unhandled letter falls through the tokenizer and its
        # digits land in the command position, so swapping `h18` for `v18`
        # fails with "unhandled path command '18'" -- naming the number rather
        # than the command. An icon edit doing exactly that is what these
        # tests are for, so the failure has to say so.
        assert not unhandled, (
            f"icon path uses {unhandled}, which _visits cannot measure;"
            f" extend it or keep the icon to M, Q and h: {d}"
        )
    return paths


def _visits(d, steps=20):
    """Every point a path visits, sampling quadratics rather than solving them.

    A control point outside the viewBox pulls the curve far past its own
    endpoints, which is how this icon gets its width at all -- so endpoints
    alone would measure the wrong thing.
    """
    tokens = re.findall(r"[MQh]|-?\d+(?:\.\d+)?", d)
    x = y = 0.0
    index = 0
    while index < len(tokens):
        command = tokens[index]
        if command == "M":
            x, y = float(tokens[index + 1]), float(tokens[index + 2])
            yield x, y
            index += 3
        elif command == "h":
            x += float(tokens[index + 1])
            yield x, y
            index += 2
        elif command == "Q":
            cx, cy, x1, y1 = (float(t) for t in tokens[index + 1:index + 5])
            for step in range(steps + 1):
                t = step / steps
                u = 1 - t
                yield (u * u * x + 2 * u * t * cx + t * t * x1,
                       u * u * y + 2 * u * t * cy + t * t * y1)
            x, y = x1, y1
            index += 5
        else:
            raise AssertionError(f"unhandled path command {command!r}")


def _extent(points):
    """(left, right, top, bottom) of a path. Materialised first: _visits is a
    generator, and reading it twice leaves the second axis empty."""
    points = list(points)
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return min(xs), max(xs), min(ys), max(ys)


def _strand_x_at(d, y):
    """Where a strand sits horizontally at a given height.

    Sampled on a fine grid rather than solved: the curve is monotonic in y
    over each segment, so the nearest sample is within a fraction of a unit,
    and a solver here would be more machinery than the question needs."""
    return min(_visits(d, steps=400), key=lambda point: abs(point[1] - y))[0]


def test_each_strand_fills_its_box(public_server):
    """A helix drawn between its own endpoints is 7 units wide in a 32-unit
    box, which at 16px is a vertical smudge; the width comes from control
    points placed outside the box. The first attempt shipped the smudge.

    Measured per strand, not over the whole icon: the rungs are a fixed
    18-unit horizontal line, so a bound on the union of all three paths is
    satisfied by the rungs alone and says nothing about the strands. That
    version of this test passed on the broken icon."""
    url, _ = public_server
    page = requests.get(f"{url}/", timeout=10).text
    for index, d in enumerate(_icon_paths(page)[:2]):
        left, right, top, bottom = _extent(_visits(d))
        assert right - left >= 0.4 * 32, (
            f"strand {index} is {right - left:.1f} units wide in a 32-unit box"
        )
        assert bottom - top >= 0.7 * 32, (
            f"strand {index} is {bottom - top:.1f} units tall in a 32-unit box"
        )


def test_the_rungs_land_on_the_strands(public_server):
    """A rung reads as connecting the strands only if it ends *on* them, at
    its own height.

    Bounding the rung against the strands' overall left and right instead
    passes on a rung floating in clear space: move these two to y=5 and y=19,
    where the strands sit at x 9.9 and 22.1, and a 7-to-25 bar overhangs both
    by three units while every such assertion still holds. Review caught that;
    this samples each strand at the rung's own y instead."""
    url, _ = public_server
    page = requests.get(f"{url}/", timeout=10).text
    paths = _icon_paths(page)
    rungs = list(_visits(paths[2]))
    assert len(rungs) == 4, f"expected two rungs, two ends each; got {rungs}"

    for (left, y), (right, _) in zip(rungs[::2], rungs[1::2]):
        at_height = sorted(_strand_x_at(d, y) for d in paths[:2])
        assert left == pytest.approx(at_height[0], abs=0.3), (
            f"rung at y={y} starts at x={left}, but the left strand is at"
            f" x={at_height[0]:.2f} there"
        )
        assert right == pytest.approx(at_height[1], abs=0.3), (
            f"rung at y={y} ends at x={right}, but the right strand is at"
            f" x={at_height[1]:.2f} there"
        )


def test_the_two_strands_are_mirrored(public_server):
    """The pair reads as a helix only if one strand is the other reflected in
    the centre line. Drifting control points give two unrelated squiggles."""
    url, _ = public_server
    page = requests.get(f"{url}/", timeout=10).text
    drawn = _icon_paths(page)[:2]
    # Equal point counts are not enough to make position i comparable: two
    # different command layouts can sample to the same total and shift the
    # grid, so the zip below would compare unrelated points on the curves.
    layouts = [_commands(d) for d in drawn]
    assert layouts[0] == layouts[1], f"strands are drawn differently: {layouts}"
    strand_a, strand_b = (list(_visits(d)) for d in drawn)
    assert len(strand_a) == len(strand_b)
    for (ax, ay), (bx, by) in zip(strand_a, strand_b):
        assert ay == pytest.approx(by)
        assert ax - 16 == pytest.approx(16 - bx)


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
def limited_server(full_warehouse, tmp_path, monkeypatch):
    """Builds a throttled public server; the caller says what proxy to expect.

    One factory rather than three near-identical fixtures, so a change to how
    the server starts or stops is made once. The only thing the callers below
    differ by is the proxy configuration under test.
    """
    servers = []

    def build(header="", hops=1):
        if servers:
            raise AssertionError(
                "limited_server builds one server per test. A second call would"
                " re-point the module-level proxy settings that the first server"
                " reads on every request, and re-export over the file it holds"
                " open -- so the first server would silently change behaviour"
                " with nothing failing to say so."
            )
        source, _ = full_warehouse
        target = tmp_path / "public.duckdb"
        spans.export_public(source, target)
        # A tenth of a token per second, not one. At one per second the test
        # only produced a 429 if eight sequential HTTP round trips finished
        # inside a second, which is true on this laptop and not on a loaded
        # CI box.
        monkeypatch.setattr(server_module, "RATE_PER_MINUTE", 6.0)
        monkeypatch.setattr(server_module, "RATE_BURST", 3)
        monkeypatch.setattr(server_module, "TRUSTED_IP_HEADER", header)
        monkeypatch.setattr(server_module, "TRUSTED_PROXY_HOPS", hops)

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(target))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    try:
        yield build
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.fixture
def throttled(limited_server):
    """Directly exposed: the limiter reads the socket, which is the client."""
    return limited_server()


@pytest.fixture
def proxied(limited_server):
    """Behind a proxy that appends one entry, as Cloud Run does.

    One, not two: measured against the live service rather than taken from
    Google's load-balancer documentation, which describes a different ingress
    and would put the hop count one too high -- which is precisely where a
    forged prefix lands.
    """
    return limited_server("X-Forwarded-For", 1)


@pytest.fixture
def short_chain(limited_server):
    """Configured for two appended entries against a proxy that appends one.

    Only to prove the fallback: a chain shorter than the hop count is not the
    chain this deployment was configured for, so no entry in it is trusted.
    There is deliberately no test asserting what a too-high hop count *does*
    read. Review pointed out that such a test passes exactly when the
    deployment is exploitable, and would fail on a future change that made an
    over-long chain fall back instead -- cementing the vulnerable behaviour
    rather than guarding against it. What the wrong number costs is recorded
    in `server.py` and `deploy/README.md`, measured.
    """
    return limited_server("X-Forwarded-For", 2)


def test_a_flood_is_refused_with_429_and_retry_after(throttled):
    codes = [requests.get(f"{throttled}/api/context", timeout=10).status_code
             for _ in range(8)]
    assert codes[0] == 200
    assert 429 in codes, f"nothing was throttled: {codes}"

    refused = requests.get(f"{throttled}/api/context", timeout=10)
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1
    assert refused.json() == {"error": "too many requests"}


# --------------------------------------------------------------------------
# Bounded concurrency: what everyone can ask for together
# --------------------------------------------------------------------------



def test_the_database_caps_are_actually_applied(monkeypatch, tmp_path):
    """Reading the module constants proves nothing.

    The previous version of this test asserted that `DB_MEMORY_LIMIT` existed,
    which would still have passed with both `SET` statements deleted. This one
    records what is issued to DuckDB.
    """
    duckdb.connect(str(tmp_path / "w.duckdb")).close()
    issued = []
    real_connect = duckdb.connect

    class Recording:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            issued.append(sql)
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(server_module.duckdb, "connect",
                        lambda *a, **k: Recording(real_connect(*a, **k)))
    make_handler(tmp_path / "w.duckdb")

    assert any("memory_limit" in s for s in issued), f"no memory cap issued: {issued}"
    assert any("threads" in s for s in issued), f"no thread cap issued: {issued}"
    assert any(server_module.DB_MEMORY_LIMIT in s for s in issued), \
        "the cap issued is not the configured one"


def test_concurrent_requests_all_succeed(public_server):
    """The failure this bound exists to prevent: 60 simultaneous requests for
    a large gene ran enough queries at once to exhaust DuckDB's budget, and 38
    of them came back to a visitor as "internal error"."""
    url, _ = public_server
    codes, errors = [], []

    def hit():
        try:
            codes.append(requests.get(
                f"{url}/api/variants", params={"gene": "TESTGENE", "on": "2019-01-02"},
                timeout=60).status_code)
        except Exception as exc:                       # noqa: BLE001
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=hit) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert set(codes) == {200}, f"expected every request served, got {sorted(set(codes))}"


def test_a_slot_is_released_after_every_request(public_server):
    """A leaked slot would pass a happy-path test and wedge the server after
    `DB_MAX_CONCURRENT` requests in production, including failed ones."""
    url, _ = public_server
    for _ in range(server_module.DB_MAX_CONCURRENT + 3):
        requests.get(f"{url}/api/variants", params={"gene": "!!bad!!"}, timeout=10)
    assert requests.get(f"{url}/api/context", timeout=10).status_code == 200


@pytest.fixture
def blocked(full_warehouse, tmp_path, monkeypatch):
    """A server whose single query slot is demonstrably held.

    Racing real queries and hoping one wins is how a test passes without
    exercising anything. Here the holding request waits on an event, so "the
    slot is taken" is a fact before the assertion runs.
    """
    source, _ = full_warehouse
    target = tmp_path / "public.duckdb"
    spans.export_public(source, target)
    monkeypatch.setattr(server_module, "DB_MAX_CONCURRENT", 1)
    monkeypatch.setattr(server_module, "DB_QUEUE_TIMEOUT", 0.4)

    holding, release = threading.Event(), threading.Event()
    real = server_module.queries.variants_on

    def block(*a, **k):
        holding.set()
        release.wait(timeout=30)
        return real(*a, **k)

    monkeypatch.setattr(server_module.queries, "variants_on", block)

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(target))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"

    hold = threading.Thread(target=lambda: requests.get(
        f"{url}/api/variants", params={"gene": "TESTGENE", "on": "2019-01-02"},
        timeout=60), daemon=True)
    hold.start()
    assert holding.wait(timeout=20), "the blocking request never reached the query"
    try:
        yield url, release
    finally:
        release.set()
        hold.join(timeout=30)
        server.shutdown()
        server.server_close()


def test_an_exhausted_queue_answers_503(blocked):
    """With the only slot provably held, a second request must be shed."""
    url, _ = blocked
    response = requests.get(f"{url}/api/variants",
                            params={"gene": "TESTGENE", "on": "2019-01-02"}, timeout=30)
    assert response.status_code == 503
    assert response.json() == {"error": "server busy, please retry"}


def test_the_queue_recovers_once_the_rush_passes(blocked):
    url, release = blocked
    assert requests.get(f"{url}/api/context", timeout=30).status_code == 503
    release.set()
    time.sleep(0.6)
    assert requests.get(f"{url}/api/context", timeout=30).status_code == 200


def test_the_health_probe_does_not_queue_behind_queries(blocked):
    """A probe that waits reports the container unhealthy for being busy, and
    gets it restarted at precisely the wrong moment. The slot is held here, so
    a prompt 200 can only mean the probe never asked for one."""
    url, _ = blocked
    # Structural rather than timed: a request that queued for this slot would
    # have timed out and answered 503, so the 200 is itself the proof. A
    # wall-clock budget on a real round trip only adds CI flakiness.
    response = requests.get(f"{url}/health", timeout=30)
    assert response.status_code == 200
    assert response.json()["timeline"] is True


def test_an_unknown_path_is_404_without_taking_a_slot(blocked):
    """Otherwise a scanner asking for /favicon.ico queues for a database slot
    it will never use, and gets a 503 under load instead of a prompt 404."""
    url, _ = blocked
    # Also structural: had it entered the queue it would be 503, not 404.
    response = requests.get(f"{url}/favicon.ico", timeout=30)
    assert response.status_code == 404
    assert response.json() == {"error": "no such endpoint"}


def test_the_slot_is_released_before_the_response_is_written(full_warehouse, tmp_path,
                                                             monkeypatch):
    """The bug both PR reviewers found in the first version of this bound.

    The slot used to wrap `json.dumps` and the blocking socket write, so a
    client that stopped reading held a slot for the life of its connection.
    Four of those and the demo was down -- an outage dressed as a safety
    feature. With serialisation made slow and only one slot, three concurrent
    requests can all succeed only if the slot is given back first.
    """
    source, _ = full_warehouse
    target = tmp_path / "public.duckdb"
    spans.export_public(source, target)
    # The queue timeout must be *shorter* than the serialisation it is racing,
    # or the test cannot fail: with a 10s timeout and a 0.8s dump, a queued
    # caller waits 0.8s and still gets its 200 whether or not the slot was
    # held. At 0.25s against 0.8s, holding the slot through serialisation
    # forces a 503 and releasing it does not.
    monkeypatch.setattr(server_module, "DB_MAX_CONCURRENT", 1)
    monkeypatch.setattr(server_module, "DB_QUEUE_TIMEOUT", 0.25)

    real_dumps = server_module.json.dumps

    def slow_dumps(*a, **k):
        time.sleep(0.8)
        return real_dumps(*a, **k)

    monkeypatch.setattr(server_module.json, "dumps", slow_dumps)

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(target))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        codes = []
        threads = [threading.Thread(target=lambda: codes.append(
            requests.get(f"{url}/api/variants",
                         params={"gene": "TESTGENE", "on": "2019-01-02"},
                         timeout=30).status_code)) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert set(codes) == {200}, (
            f"got {sorted(codes)}: a slot is held through serialisation, so a "
            "slow client can deny it to everyone else"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_the_probe_stops_querying_after_the_first_call(public_server, monkeypatch):
    """The warehouse is read-only and cannot change under a running server, so
    the verdict is a constant. A probe that re-queries on every poll is a
    database path sitting outside the concurrency bound."""
    url, _ = public_server
    assert requests.get(f"{url}/health", timeout=10).status_code == 200

    calls = []
    real = server_module.queries.timeline_start
    monkeypatch.setattr(server_module.queries, "timeline_start",
                        lambda c: (calls.append(1), real(c))[1])
    for _ in range(5):
        assert requests.get(f"{url}/health", timeout=10).status_code == 200
    assert calls == [], f"the probe queried {len(calls)} more times after the first"


def test_the_context_is_warmed_before_the_port_opens(tmp_path, full_warehouse):
    """2.7s on one vCPU, and it used to be charged to whoever arrived first --
    which on a scale-to-zero deployment is every visitor who wakes it."""
    source, _ = full_warehouse
    target = tmp_path / "public.duckdb"
    spans.export_public(source, target)

    handler = make_handler(target)
    assert hasattr(handler, "warm"), "serve() needs a way to pay this cost up front"
    handler.warm()

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        started = time.perf_counter()
        response = requests.get(
            f"http://127.0.0.1:{server.server_address[1]}/api/context", timeout=30)
        elapsed = time.perf_counter() - started
        assert response.status_code == 200
        assert elapsed < 1.0, f"first request took {elapsed:.2f}s; the cache was not warm"
    finally:
        server.shutdown()
        server.server_close()


def test_a_spoofed_forwarded_prefix_does_not_mint_a_fresh_bucket(proxied):
    """The defect this guards is a limiter that looks like one and is not.

    Google's load balancer appends `<client-ip>,<load-balancer-ip>` to
    whatever the caller sent and does not verify anything before it. Reading
    the leftmost entry therefore reads the caller's own text, so rotating it
    gives an unlimited supply of buckets. Every request below carries a
    different forged prefix and they must still share one bucket."""
    codes = [
        requests.get(
            f"{proxied}/api/context",
            headers={"X-Forwarded-For": f"203.0.113.{n}, 198.51.100.7"},
            timeout=10,
        ).status_code
        for n in range(8)
    ]
    assert codes[0] == 200
    assert 429 in codes, f"forged prefixes were handed their own buckets: {codes}"


def test_two_real_clients_do_not_share_a_bucket(proxied):
    """The other half: the entry the proxy itself wrote does separate
    visitors, which is the whole reason for reading the header at all."""
    spent = [
        requests.get(
            f"{proxied}/api/context",
            headers={"X-Forwarded-For": "198.51.100.7"},
            timeout=10,
        ).status_code
        for _ in range(8)
    ]
    assert 429 in spent, f"the first client was never throttled: {spent}"
    fresh = requests.get(
        f"{proxied}/api/context",
        headers={"X-Forwarded-For": "198.51.100.99"},
        timeout=10,
    )
    assert fresh.status_code == 200, "a second visitor inherited the first one's bucket"


def test_a_chain_shorter_than_the_hop_count_falls_back_to_the_socket(short_chain):
    """A request that did not come through the configured proxy has no
    trustworthy entry to read, so it must not be believed."""
    codes = [
        requests.get(
            f"{short_chain}/api/context",
            headers={"X-Forwarded-For": f"203.0.113.{n}"},
            timeout=10,
        ).status_code
        for n in range(8)
    ]
    assert 429 in codes, f"a one-hop chain was read as a client address: {codes}"


def test_the_health_probe_is_never_throttled(throttled):
    """A platform polls it far more often than a human browses, and a probe
    answering 429 is a container that gets restarted for being popular."""
    for _ in range(12):
        requests.get(f"{throttled}/api/context", timeout=10)
    assert requests.get(f"{throttled}/health", timeout=10).status_code == 200


def test_a_warehouse_that_cannot_be_warmed_still_serves(tmp_path, monkeypatch, capsys):
    """Moving the cold-start cost before the port binds moved the blast radius
    with it: what used to be one failed request would become a boot failure,
    and on a platform that restarts containers that is a crash loop rather
    than a degraded demo."""
    duckdb.connect(str(tmp_path / "w.duckdb")).close()
    handler = make_handler(tmp_path / "w.duckdb")

    def explode():
        raise RuntimeError("assertion_dense is not readable")

    handler.warm = staticmethod(explode)
    served = threading.Event()

    class FakeServer:
        def __init__(self, *a, **k):
            pass

        def serve_forever(self):
            served.set()

        def server_close(self):
            pass

    monkeypatch.setattr(server_module, "make_handler", lambda _w: handler)
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", FakeServer)
    server_module.serve(tmp_path / "w.duckdb")

    assert served.is_set(), "a failed warm-up must not stop the server starting"
    assert "could not be read ahead of time" in capsys.readouterr().out


def test_the_policy_threshold_reaches_the_served_context(full_warehouse, tmp_path,
                                                         monkeypatch):
    """The knob has to change what a visitor is shown, not merely exist.

    The first version of this test asserted that the fixture's sweep tripped
    the default and that the constant was positive, which would have passed
    with the threshold wired to nothing at all. It is a share of the corpus,
    so it depends on the panel: the 2023 re-aggregation sweeps 5,843 variants
    of the five-gene panel, 13.3% of it, and 6,083 of the thirty-one gene
    panel, only 4.05% of that far larger corpus. `spike` has taken it as a
    flag from the start; the web layer had none, so densifying the ingest left
    the demo reporting no event at all -- this detector's own failure mode,
    turned on itself.
    """
    source, _ = full_warehouse
    target = tmp_path / "public.duckdb"
    spans.export_public(source, target)

    def context_events(threshold):
        monkeypatch.setattr(server_module, "POLICY_THRESHOLD", threshold)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(target))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            return requests.get(
                f"http://127.0.0.1:{server.server_address[1]}/api/context",
                timeout=20).json()["policy_events"]
        finally:
            server.shutdown()
            server.server_close()

    assert context_events(0.05), "the fixture sweep is detectable at 5%"
    assert context_events(0.99) == [], (
        "raising the threshold above the sweep must reach the served context; "
        "if this still reports events the knob is not wired through"
    )


def test_a_threshold_above_the_sweep_finds_nothing(full_warehouse, tmp_path):
    """The knob has to actually reach the detector, not just exist."""
    source, _ = full_warehouse
    target = tmp_path / "public.duckdb"
    spans.export_public(source, target)
    connection = duckdb.connect(str(target), read_only=True)
    try:
        from mendelea.web import queries
        assert queries.policy_events(connection, 0.05), "sweep is detectable at 5%"
        assert queries.policy_events(connection, 0.99) == [], "nothing sweeps 99%"
    finally:
        connection.close()


# --------------------------------------------------------------------------
# The ignore lists, which decide whether a build gets its data at all
# --------------------------------------------------------------------------


def _ignored_by(ignore_file: str, name: str) -> bool:
    """Last matching pattern wins; a leading ! negates. Docker and gcloud
    both work this way."""
    from pathlib import Path as _P
    from pathlib import PurePath

    verdict = False
    for line in _P(ignore_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        if PurePath(name).match(pattern):
            verdict = not negated
    return verdict


@pytest.mark.parametrize("ignore_file", [".dockerignore", ".gcloudignore"])
def test_the_build_context_keeps_the_artefact_and_drops_the_warehouse(ignore_file):
    """`gcloud run deploy --source .` falls back to .gitignore when there is
    no .gcloudignore, and .gitignore excludes *.duckdb -- which is the entire
    artefact the image is built around. The build would reach `COPY
    mendelea-public.duckdb` with no such file in the context.

    The working warehouse must stay out just as firmly: it carries both
    private planes and is several hundred megabytes.
    """
    assert not _ignored_by(ignore_file, "mendelea-public.duckdb"), \
        f"{ignore_file} drops the artefact; the image cannot be built"
    assert _ignored_by(ignore_file, "mendelea.duckdb"), \
        f"{ignore_file} would upload the working warehouse, private planes and all"
    assert not _ignored_by(ignore_file, "panels/spike.json"), \
        f"{ignore_file} drops the panel definitions the gene picker needs"


def test_the_image_sets_a_threshold_that_matches_the_panel_it_ships():
    """The threshold is a share of the corpus and the image ships one panel.

    Left at the 5% default the 31-gene timeline reports no policy event at
    all, so a plain `docker run` would lose the relabelling story -- the
    detector's own failure mode, silently.
    """
    from pathlib import Path as _P

    import re as _re
    from mendelea.reports import policy as _policy

    dockerfile = _P("Dockerfile").read_text(encoding="utf-8")
    match = _re.search(r"MENDELEA_POLICY_THRESHOLD=([0-9.]+)", dockerfile)
    assert match, "the image ships a panel the default threshold cannot see an event in"

    # Checking the name appears would pass with the default written out in
    # full -- the exact regression this guards. The value has to be lower than
    # the default, because the panel baked into the image sweeps 4.05% where
    # the default asks for 5%.
    shipped = float(match.group(1))

    # The window is bounded by two *measurements* on the panel in the image,
    # not by the default. Comparing against DEFAULT_THRESHOLD was the first
    # attempt and it does not discriminate: 0.045 is below the 5% default and
    # still above the event, so it passes while the detector reports nothing.
    #
    # Both numbers come from `policy.step_series` on the 18-release timeline:
    EVENT_SHARE = 0.0405     # the 2023 re-aggregation, the thing to catch
    LOUDEST_ORDINARY = 0.0291  # LIKELY_BENIGN -> BENIGN, the thing to exclude
    assert LOUDEST_ORDINARY < EVENT_SHARE < _policy.DEFAULT_THRESHOLD, \
        "the window these bounds describe has to exist"

    assert shipped <= EVENT_SHARE, (
        f"image ships {shipped}, above the 2023 event's {EVENT_SHARE} share; "
        "the detector would report nothing and the demo loses the relabelling story"
    )
    assert shipped > LOUDEST_ORDINARY, (
        f"image ships {shipped}, at or below the loudest ordinary step on this "
        f"panel ({LOUDEST_ORDINARY}); genuine movement would be called relabelling"
    )
