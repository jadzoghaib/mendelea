"""A small read-only HTTP server for the time machine.

Stdlib only, deliberately. This is the artefact you put in front of a
laboratory in a first meeting, and every dependency is one more thing that can
fail on someone else's laptop five minutes before the demo. The query layer is
separate (`queries.py`), so moving to FastAPI later is an adapter, not a
rewrite.

Read-only throughout: the connection is opened read-only, there are no write
endpoints, and it serves public ClinVar data only. Nothing here should ever
touch the case or decision planes -- those need auth and tenant isolation,
which is Phase 3.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb

from . import queries

STATIC = Path(__file__).resolve().parent / "static"

# Gene symbols reach SQL as a bound parameter, but bound or not we only ever
# want to see something that looks like a gene symbol.
SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
SAFE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_:.\-]{1,120}$")


PANEL_DIR = Path(__file__).resolve().parents[3] / "panels"


def _panel_genes(panel: str | None) -> set[str] | None:
    """The panel's declared gene list, used to hide flanking neighbours.

    Returns None if the panel file cannot be found, which degrades to showing
    every symbol rather than showing none.
    """
    if not panel:
        return None
    path = PANEL_DIR / f"{panel}.json"
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

    def require(value: str | None, pattern: re.Pattern, name: str) -> str:
        if not value or not pattern.match(value):
            raise ApiError(400, f"invalid or missing {name}")
        return value

    class Handler(BaseHTTPRequestHandler):
        server_version = "mendelea"

        def log_message(self, *args):  # noqa: A003 - quiet by default
            pass

        def _send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200):
            self._send(status, json.dumps(payload).encode(), "application/json")

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
                    panel = queries.loaded_panel(conn())
                    return self._json({
                        "panel": panel,
                        "releases": queries.release_dates(conn(), panel["panel"]),
                        "genes": queries.genes(conn(), _panel_genes(panel["panel"])),
                    })

                if parsed.path == "/api/variants":
                    gene = require(one("gene"), SAFE_SYMBOL, "gene")
                    on = require(one("on"), SAFE_DATE, "on")
                    return self._json({
                        "headline": queries.headline(conn(), gene, on),
                        "variants": queries.variants_on(conn(), gene, on),
                    })

                if parsed.path == "/api/timeline":
                    allele = require(one("allele_id"), SAFE_ID, "allele_id")
                    return self._json({"timeline": queries.timeline(conn(), allele)})

                raise ApiError(404, "no such endpoint")

            except ApiError as exc:
                self._json({"error": exc.message}, exc.status)
            except Exception as exc:  # noqa: BLE001 - never leak a traceback
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

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
