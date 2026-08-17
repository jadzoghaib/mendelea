"""Cross-process mutual exclusion for ingest.

Two ingest runs against the same panel do not corrupt anything -- snapshot
writes go to a temporary file and are moved into place atomically, so the
worst case is that both processes fetch the same release and one write wins.
But "does not corrupt" is not the same as "is fine": the duplicate run doubles
the load on NCBI, halves the effective bandwidth of both, and makes the whole
ingest crawl. That happened during the first full build of this project.

The lock is advisory and deliberately simple. It protects against the mistake
that actually occurs -- a human starting a second run because the first looked
stuck -- not against a hostile process.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# A lock older than this is assumed to belong to a process that died without
# cleaning up. Generous, because a full-panel ingest legitimately runs for
# tens of minutes and stealing a live lock is worse than waiting.
DEFAULT_STALE_AFTER = 6 * 3600


class LockHeld(RuntimeError):
    """Another process holds the lock and it is not stale."""


def _describe(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return f"pid {payload.get('pid')} since {payload.get('acquired_at')}"
    except Exception:  # noqa: BLE001 - a corrupt lock file is still a lock
        return "unknown holder"


@contextmanager
def exclusive(path: Path, stale_after: int = DEFAULT_STALE_AFTER,
              force: bool = False):
    """Hold an exclusive advisory lock for the duration of the block.

    Raises LockHeld if another live run holds it. Steals the lock, with the
    reason surfaced by the caller, if it is older than `stale_after` or if
    `force` is set.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    stolen_from: str | None = None

    while True:
        try:
            # O_EXCL makes creation atomic: exactly one process can win, even
            # if several arrive at the same instant.
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue  # holder released it between our two calls; retry

            if not force and age < stale_after:
                raise LockHeld(
                    f"{path.name} is held by {_describe(path)} "
                    f"({age / 60:.0f} min ago). Wait for it, or pass --force."
                )

            stolen_from = _describe(path)
            path.unlink(missing_ok=True)
            continue
        else:
            break

    try:
        os.write(handle, json.dumps({
            "pid": os.getpid(),
            "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }).encode())
        os.close(handle)
        yield stolen_from
    finally:
        path.unlink(missing_ok=True)
