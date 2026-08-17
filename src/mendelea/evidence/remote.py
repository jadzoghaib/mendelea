"""HTTP access to remote BGZF files, with a local cache for index files.

NCBI's FTP mirror serves over HTTPS and honours byte ranges (verified: it
returns 206 with a Content-Range header). That is the one external assumption
this whole ingest design rests on, so `check_range_support` exists to assert
it loudly rather than let a silent 200 quietly download 193 MB.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import requests

USER_AGENT = "mendelea/0.1 (+evidence-timeline research build)"
_RETRY_STATUS = {429, 500, 502, 503, 504}


class RangeUnsupported(RuntimeError):
    """Raised when a server ignores Range and would hand back the whole file."""


@dataclass
class FetchStats:
    """Bytes actually moved, so the ingest can report what range queries saved."""

    requests_made: int = 0
    bytes_fetched: int = 0
    full_size: int = 0

    @property
    def saved_ratio(self) -> float:
        if not self.full_size:
            return 0.0
        return 1.0 - (self.bytes_fetched / self.full_size)


class RemoteFile:
    """A remote file addressed by byte range."""

    def __init__(self, url: str, session: requests.Session | None = None,
                 max_retries: int = 4, timeout: int = 120):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.stats = FetchStats()

    def _request(self, headers: dict[str, str]) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.get(
                    self.url, headers=headers, timeout=self.timeout
                )
                if response.status_code in _RETRY_STATUS:
                    raise requests.HTTPError(f"status {response.status_code}")
                response.raise_for_status()
                return response
            except Exception as exc:  # noqa: BLE001 - retry on anything transient
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
        raise RuntimeError(f"GET failed after {self.max_retries} tries: {self.url}") from last_error

    def size(self) -> int:
        response = self.session.head(self.url, timeout=self.timeout)
        response.raise_for_status()
        self.stats.full_size = int(response.headers.get("Content-Length", 0))
        return self.stats.full_size

    def check_range_support(self) -> None:
        """Fail fast if the server would ignore our Range headers."""
        response = self.session.get(
            self.url, headers={"Range": "bytes=0-99"}, timeout=self.timeout
        )
        if response.status_code != 206:
            raise RangeUnsupported(
                f"{self.url} answered {response.status_code}, not 206; "
                "refusing to fall back to a full download"
            )

    def read_range(self, start: int, end: int) -> bytes:
        """Fetch an inclusive byte range."""
        response = self._request({"Range": f"bytes={start}-{end}"})
        payload = response.content
        self.stats.requests_made += 1
        self.stats.bytes_fetched += len(payload)
        return payload

    def read_ranges(self, ranges: list[tuple[int, int]]) -> bytes:
        """Fetch several ranges and concatenate them in file order.

        Ranges are expected pre-merged and sorted by the tabix planner, so the
        concatenation stays a valid run of whole BGZF members.
        """
        return b"".join(self.read_range(start, end) for start, end in ranges)

    def read_all(self) -> bytes:
        payload = self._request({}).content
        self.stats.requests_made += 1
        self.stats.bytes_fetched += len(payload)
        return payload


def fetch_cached(url: str, cache_dir: Path, session: requests.Session | None = None) -> bytes:
    """Download a small file once and reuse it forever.

    Used for .tbi indexes, which are a few MB, immutable per release, and
    needed on every gene query against that release.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    cached = cache_dir / f"{key}-{url.rsplit('/', 1)[-1]}"

    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_bytes()

    payload = RemoteFile(url, session=session).read_all()
    tmp = cached.with_suffix(cached.suffix + ".partial")
    tmp.write_bytes(payload)
    tmp.replace(cached)  # atomic, so an interrupted run cannot poison the cache
    return payload
