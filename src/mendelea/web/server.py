"""A small read-only HTTP server for the time machine.

Stdlib only, deliberately. This is the artefact you put in front of a
laboratory in a first meeting, and every dependency is one more thing that can
fail on someone else's laptop five minutes before the demo. The query layer is
separate (`queries.py`), so moving to FastAPI later is an adapter, not a
rewrite.

Read-only throughout: the connection is opened read-only, there are no write
endpoints, and it serves public ClinVar data only. The one authenticated
endpoint, `/api/case/report`, is the single place tenant data is reachable,
and the tenant comes from the token, never from the request.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb

from .. import config, tenancy
from ..cases import report as case_report
from ..reports import policy
from . import queries

STATIC = Path(__file__).resolve().parent / "static"
log = logging.getLogger("mendelea.web")

# Gene symbols reach SQL as a bound parameter, but bound or not we only ever
# want to see something that looks like a gene symbol.
SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
SAFE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_:.\-]{1,120}$")
# A search term: a variation ID, a position, or a fragment of a condition
# name ("Li-Fraumeni", "breast and/or ovarian").
SAFE_QUERY = re.compile(r"^[A-Za-z0-9 ,_:./()'\-]{1,60}$")
SAFE_INT = re.compile(r"^\d{1,7}$")

PAGE_LIMIT = 1000


def _panel_genes(panel: str | None) -> set[str] | None:
    """The panel's declared gene list, used to hide flanking neighbours.

    Returns None if the panel file cannot be found, which degrades to showing
    every symbol rather than showing none.
    """
    if not panel:
        return None
    path = config.panel_dir() / f"{panel}.json"
    if not path.exists():
        return None
    try:
        return {g.upper() for g in json.loads(path.read_text(encoding="utf-8"))["genes"]}
    except Exception:  # noqa: BLE001 - a malformed panel must not break the demo
        return None


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def make_handler(warehouse: Path):
    # One connection per thread: DuckDB connections are not safe to share
    # across threads, and ThreadingHTTPServer will hand us several.
    local = threading.local()

    def conn():
        if not hasattr(local, "c"):
            local.c = duckdb.connect(str(warehouse), read_only=True)
        return local.c

    # Computed once per process. The warehouse cannot change underneath a
    # running server: DuckDB admits one writer or many readers to a file,
    # never both, so a rebuild waits for the server to stop. Restart is the
    # invalidation. Policy detection alone takes most of a second and the
    # gene ranking scans every span; neither belongs on the page-load path.
    cache: dict = {}
    cache_lock = threading.Lock()

    def context() -> tuple[dict, list]:
        with cache_lock:
            if "context" not in cache:
                c = conn()
                panel = queries.loaded_panel(c)
                releases = queries.release_dates(c, panel["panel"])
                # Both rates are anchored to the earliest date the timeline
                # holds data for, which is normally the earliest release and
                # differs from it only when a snapshot is recorded but its
                # file is missing. Using the release date there ranked every
                # gene off an empty baseline and the page called that "no
                # timeline".
                baseline = queries.timeline_start(c)
                events = queries.policy_events(c)
                cache["events"] = events
                cache["context"] = {
                    "panel": panel,
                    "releases": releases,
                    "genes": queries.genes(c, baseline, _panel_genes(panel["panel"])),
                    "panel_headline": queries.panel_headline(c, baseline),
                    # So the page can name the bar it ranked by rather than
                    # describing a range whose scope it cannot state.
                    "headline_vus_floor": queries.HEADLINE_VUS_FLOOR,
                    "policy_events": [asdict(e) for e in events],
                }
            return cache["context"], cache["events"]

    def require(value: str | None, pattern: re.Pattern, name: str) -> str:
        if not value or not pattern.match(value):
            raise ApiError(400, f"invalid or missing {name}")
        return value

    def optional(value: str | None, pattern: re.Pattern, name: str) -> str | None:
        if value is None or value == "":
            return None
        return require(value, pattern, name)

    def integer(value: str | None, name: str, default: int, low: int, high: int) -> int:
        if value is None or value == "":
            return default
        number = int(require(value, SAFE_INT, name))
        if not low <= number <= high:
            raise ApiError(400, f"{name} must be between {low} and {high}")
        return number

    class Handler(BaseHTTPRequestHandler):
        server_version = "mendelea"
        # Persistent connections: the page issues a request per slider step,
        # and a fresh TCP handshake for each of them was the slowest part.
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: A003 - quiet by default
            pass

        def _send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Never cache. Keep-alive plus a Content-Length and no cache
            # directive is enough for Chrome to reuse a stale page heuristically
            # -- which it did during development, serving the previous build of
            # index.html after a restart. A page that must be re-read to be
            # trusted is not worth the saved kilobytes.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200):
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _authenticate(self) -> str:
            """Resolve the bearer token to a tenant, or refuse.

            The tenant is never read from a query parameter. If it were, a
            valid token for one laboratory could be pointed at another
            laboratory's data -- authentication without authorisation.
            """
            header = self.headers.get("Authorization", "")
            token = header[7:].strip() if header.lower().startswith("bearer ") else None
            tenant = tenancy.verify(conn(), token)
            if not tenant:
                raise ApiError(401, "missing or invalid bearer token")
            return tenant

        def do_GET(self):  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            one = lambda key: (query.get(key) or [None])[0]  # noqa: E731

            try:
                if parsed.path in ("/", "/index.html"):
                    return self._send(
                        200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8"
                    )

                if parsed.path == "/api/context":
                    return self._json(context()[0])

                if parsed.path == "/api/variants":
                    gene = require(one("gene"), SAFE_SYMBOL, "gene")
                    on = require(one("on"), SAFE_DATE, "on")
                    q = optional(one("q"), SAFE_QUERY, "q")
                    only_moved = one("moved") == "1"
                    limit = integer(one("limit"), "limit", 400, 1, PAGE_LIMIT)
                    offset = integer(one("offset"), "offset", 0, 0, 9_999_999)
                    _, events = context()
                    page = queries.variants_on(
                        conn(), gene, on, events=events, only_moved=only_moved,
                        q=q, limit=limit, offset=offset,
                    )
                    return self._json({
                        "headline": queries.headline(conn(), gene, on, events),
                        "variants": page["rows"],
                        "total": page["total"],
                        "offset": offset,
                        "limit": limit,
                    })

                if parsed.path == "/api/composition":
                    gene = require(one("gene"), SAFE_SYMBOL, "gene")
                    ctx, _ = context()
                    return self._json({
                        "gene": gene.upper(),
                        "composition": queries.composition(
                            conn(), gene, ctx["panel"]["panel"]
                        ),
                    })

                if parsed.path == "/api/timeline":
                    allele = require(one("allele_id"), SAFE_ID, "allele_id")
                    return self._json({"timeline": queries.timeline(conn(), allele)})

                if parsed.path == "/api/case/report":
                    # The only endpoint that touches tenant data. The tenant is
                    # taken from the token and never from the request, so a
                    # caller cannot ask for someone else's report.
                    tenant = self._authenticate()
                    _, events = context()
                    rep = case_report.build(conn(), tenant, policy.suspect_keys(events))
                    return self._json({
                        "tenant": rep.tenant_id,
                        "evidence_panel": rep.evidence_panel,
                        "evidence_from": rep.evidence_from,
                        "evidence_to": rep.evidence_to,
                        "reconciliation": {
                            "loaded": rep.reconciliation.loaded,
                            "unmatched": rep.reconciliation.unmatched,
                            "before_coverage": rep.reconciliation.before_coverage,
                            "examined": rep.reconciliation.examined,
                            "match_rate": round(rep.reconciliation.match_rate, 4),
                            "sound": rep.reconciliation.is_sound(),
                        },
                        "unchanged": rep.unchanged,
                        "moved": rep.moved,
                        "actionable": rep.actionable,
                        "policy_suspect": rep.policy_suspect,
                        "findings": rep.findings,
                    })

                raise ApiError(404, "no such endpoint")

            except ApiError as exc:
                self._json({"error": exc.message}, exc.status)
            except Exception:  # noqa: BLE001 - never leak a traceback, nor its message
                # The message can carry SQL fragments and file paths. It goes
                # to the server log, where whoever is running the demo can
                # read it; the client learns only that something broke.
                log.exception("unhandled error serving %s", self.path)
                self._json({"error": "internal error"}, 500)

    return Handler


def serve(warehouse: Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(warehouse))
    print(f"  Mendelea time machine  ->  http://{host}:{port}")
    print("  read-only, public ClinVar data, Ctrl-C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        server.server_close()
