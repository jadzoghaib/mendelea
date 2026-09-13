"""The HTTP layer.

`queries.py` is covered elsewhere; this exercises the parts only the server
has: input validation, status codes, and the promise that a traceback never
reaches the client.
"""

import threading
from http.server import ThreadingHTTPServer

import pytest
import requests

from mendelea import tenancy
from mendelea.cases import model
from mendelea.web import server as server_module
from mendelea.web.server import make_handler
from tests.test_policy import build


@pytest.fixture
def base_url(tmp_path):
    connection = build(tmp_path, [
        ("2019-01-02", [("A1", "UNCERTAIN", 1), ("A2", "UNCERTAIN", 1)]),
        ("2025-01-02", [("A1", "UNCERTAIN", 1), ("A2", "PATHOGENIC", 3)]),
    ])
    # Release the write connection before the server opens it read-only.
    connection.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path / "policy.duckdb"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_index_is_served(base_url):
    response = requests.get(f"{base_url}/", timeout=10)
    assert response.status_code == 200
    assert "MENDELEA" in response.text


def test_context_reports_the_loaded_panel(base_url):
    payload = requests.get(f"{base_url}/api/context", timeout=10).json()
    assert payload["panel"]["panel"] == "testpanel"
    assert payload["releases"] == ["2019-01-02", "2025-01-02"]


def test_context_carries_the_panel_rate_and_ranked_genes(base_url):
    """The demo must be able to answer the whole-panel question in the browser."""
    payload = requests.get(f"{base_url}/api/context", timeout=10).json()
    assert payload["panel_headline"]["uncertain"] == 2
    assert payload["panel_headline"]["actionable"] == 1
    assert payload["panel_headline"]["actionable_pct"] == 50.0
    assert payload["genes"][0]["actionable_pct"] == 50.0
    assert "moved_pct" not in payload["genes"][0]


def test_context_survives_a_warehouse_with_no_timeline(tmp_path):
    """`serve` before `spans` must produce guidance, not a 500."""
    import duckdb
    duckdb.connect(str(tmp_path / "bare.duckdb")).close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path / "bare.duckdb"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        r = requests.get(f"http://127.0.0.1:{server.server_address[1]}/api/context",
                         timeout=10)
        assert r.status_code == 200
        payload = r.json()
        assert payload["panel"]["panel"] is None
        assert payload["releases"] == [] and payload["genes"] == []
        assert payload["panel_headline"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_context_carries_detected_policy_events(base_url):
    """Half the corpus moved in one step here, which the heuristic must report."""
    payload = requests.get(f"{base_url}/api/context", timeout=10).json()
    assert payload["policy_events"][0]["to_bucket"] == "PATHOGENIC"
    assert payload["policy_events"][0]["to_date"] == "2025-01-02"


def test_variants_endpoint(base_url):
    payload = requests.get(
        f"{base_url}/api/variants", params={"gene": "TESTGENE", "on": "2019-01-02"},
        timeout=10,
    ).json()
    assert payload["headline"]["uncertain"] == 2
    assert any(v["moved"] for v in payload["variants"])
    assert payload["total"] == 2 and payload["offset"] == 0


def test_variants_page_carries_the_population_count(base_url):
    payload = requests.get(
        f"{base_url}/api/variants",
        params={"gene": "TESTGENE", "on": "2019-01-02", "limit": "1", "offset": "1"},
        timeout=10,
    ).json()
    assert len(payload["variants"]) == 1
    assert payload["total"] == 2


def test_moved_filter_and_search_reach_the_query(base_url):
    moved = requests.get(
        f"{base_url}/api/variants",
        params={"gene": "TESTGENE", "on": "2019-01-02", "moved": "1"}, timeout=10,
    ).json()
    assert [v["allele_id"] for v in moved["variants"]] == ["A2"]
    found = requests.get(
        f"{base_url}/api/variants",
        params={"gene": "TESTGENE", "on": "2019-01-02", "q": "Test"}, timeout=10,
    ).json()
    assert found["total"] == 2
    # Every fixture row shares one condition, so a match proves nothing on its
    # own -- a filter that never ran would return the same 2. The miss is the
    # half of the pair that can fail.
    missed = requests.get(
        f"{base_url}/api/variants",
        params={"gene": "TESTGENE", "on": "2019-01-02", "q": "no such condition"},
        timeout=10,
    ).json()
    assert missed["total"] == 0 and missed["variants"] == []


def test_composition_endpoint(base_url):
    payload = requests.get(
        f"{base_url}/api/composition", params={"gene": "testgene"}, timeout=10
    ).json()
    assert payload["gene"] == "TESTGENE"
    assert [c["on"] for c in payload["composition"]] == ["2019-01-02", "2025-01-02"]
    assert payload["composition"][0]["counts"] == {"UNCERTAIN": 2}
    assert requests.get(f"{base_url}/api/composition", timeout=10).status_code == 400


def test_timeline_endpoint(base_url):
    payload = requests.get(
        f"{base_url}/api/timeline", params={"allele_id": "A2"}, timeout=10
    ).json()
    assert [s["bucket"] for s in payload["timeline"]] == ["UNCERTAIN", "PATHOGENIC"]


# --------------------------------------------------------------------------
# Validation and failure
# --------------------------------------------------------------------------


@pytest.mark.parametrize("params", [
    {"gene": "../../etc/passwd", "on": "2019-01-02"},
    {"gene": "TESTGENE", "on": "not-a-date"},
    {"gene": "TESTGENE"},                       # missing date
    {"on": "2019-01-02"},                       # missing gene
    {"gene": "A" * 200, "on": "2019-01-02"},    # absurd length
    {"gene": "TESTGENE", "on": "2019-01-02", "limit": "0"},
    {"gene": "TESTGENE", "on": "2019-01-02", "limit": "5000"},
    {"gene": "TESTGENE", "on": "2019-01-02", "offset": "-1"},
    {"gene": "TESTGENE", "on": "2019-01-02", "q": "<script>"},
    {"gene": "TESTGENE", "on": "2019-01-02", "q": "x" * 61},
])
def test_bad_input_is_rejected_with_400(base_url, params):
    response = requests.get(f"{base_url}/api/variants", params=params, timeout=10)
    assert response.status_code == 400
    assert "error" in response.json()


def test_unknown_endpoint_is_404(base_url):
    assert requests.get(f"{base_url}/api/nope", timeout=10).status_code == 404


def test_sql_metacharacters_do_not_reach_the_query(base_url):
    """Parameters are bound, but the pattern gate should refuse this outright."""
    response = requests.get(
        f"{base_url}/api/variants",
        params={"gene": "TESTGENE'; DROP TABLE assertion_span;--", "on": "2019-01-02"},
        timeout=10,
    )
    assert response.status_code == 400
    # And the table is still there.
    assert requests.get(f"{base_url}/api/context", timeout=10).status_code == 200


def test_unknown_gene_returns_an_empty_result_not_an_error(base_url):
    payload = requests.get(
        f"{base_url}/api/variants", params={"gene": "NOSUCHGENE", "on": "2019-01-02"},
        timeout=10,
    ).json()
    assert payload["variants"] == []
    assert payload["headline"]["uncertain"] == 0


def test_errors_never_leak_a_traceback(base_url):
    response = requests.get(
        f"{base_url}/api/timeline", params={"allele_id": "!!bad!!"}, timeout=10
    )
    assert response.status_code == 400
    assert "Traceback" not in response.text


def test_an_internal_failure_is_logged_not_served(base_url, monkeypatch, caplog):
    """The message can carry SQL and paths; the client gets a status, the log gets the rest."""
    def explode(*_):
        raise RuntimeError("secret detail: SELECT * FROM assertion_span")
    monkeypatch.setattr(server_module.queries, "timeline", explode)

    response = requests.get(
        f"{base_url}/api/timeline", params={"allele_id": "A1"}, timeout=10
    )
    assert response.status_code == 500
    assert response.json() == {"error": "internal error"}
    assert "secret detail" not in response.text
    assert "secret detail" in caplog.text


# --------------------------------------------------------------------------
# The authenticated surface
# --------------------------------------------------------------------------


@pytest.fixture
def authed(tmp_path):
    """Two tenants with data, a live server, and a token for each."""
    connection = build(tmp_path, [
        ("2019-01-02", [("A1", "UNCERTAIN", 1), ("A2", "UNCERTAIN", 1)]),
        ("2025-01-02", [("A1", "UNCERTAIN", 1), ("A2", "PATHOGENIC", 3)]),
    ])
    connection.execute(model.SCHEMA_DDL)
    tenancy.init(connection)
    tokens = {}
    for tenant, cases in (("lab-a", ["A-001", "A-002"]), ("lab-b", ["B-001"])):
        tenancy.create_tenant(connection, tenant)
        tokens[tenant] = tenancy.issue_token(connection, tenant).plaintext
        for case_ref in cases:
            connection.execute(
                "INSERT INTO case_variant "
                "(tenant_id, case_ref, allele_id, gene, contig, pos, ref, alt,"
                " reported_classification, reported_on) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [tenant, case_ref, "A2", "TESTGENE", "17", 1000, "C", "T",
                 "VUS", "2019-01-02"],
            )
    connection.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path / "policy.duckdb"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", tokens
    finally:
        server.shutdown()
        server.server_close()


def test_case_report_requires_a_token(authed):
    url, _ = authed
    assert requests.get(f"{url}/api/case/report", timeout=10).status_code == 401


@pytest.mark.parametrize("header", [
    "Bearer garbage",
    "Bearer mdl.deadbeefdeadbeef.nope",
    "Basic mdl.aaaa.bbbb",          # wrong scheme
    "mdl.aaaa.bbbb",                # no scheme
])
def test_bad_credentials_are_refused(authed, header):
    url, _ = authed
    response = requests.get(f"{url}/api/case/report",
                            headers={"Authorization": header}, timeout=10)
    assert response.status_code == 401


def test_a_token_returns_only_its_own_tenants_cases(authed):
    url, tokens = authed
    a = requests.get(f"{url}/api/case/report",
                     headers={"Authorization": f"Bearer {tokens['lab-a']}"},
                     timeout=10).json()
    b = requests.get(f"{url}/api/case/report",
                     headers={"Authorization": f"Bearer {tokens['lab-b']}"},
                     timeout=10).json()

    assert a["tenant"] == "lab-a" and a["reconciliation"]["loaded"] == 2
    assert b["tenant"] == "lab-b" and b["reconciliation"]["loaded"] == 1
    assert {f["case_ref"] for f in a["findings"]} == {"A-001", "A-002"}
    assert {f["case_ref"] for f in b["findings"]} == {"B-001"}


def test_tenant_cannot_be_overridden_by_a_query_parameter(authed):
    """Authentication without authorisation: a valid token aimed elsewhere."""
    url, tokens = authed
    response = requests.get(
        f"{url}/api/case/report",
        params={"tenant": "lab-b", "tenant_id": "lab-b"},
        headers={"Authorization": f"Bearer {tokens['lab-a']}"},
        timeout=10,
    ).json()
    assert response["tenant"] == "lab-a"
    assert all(f["case_ref"].startswith("A-") for f in response["findings"])


def test_public_endpoints_stay_open(authed):
    """The time machine is public ClinVar data and must not require a token."""
    url, _ = authed
    assert requests.get(f"{url}/api/context", timeout=10).status_code == 200
