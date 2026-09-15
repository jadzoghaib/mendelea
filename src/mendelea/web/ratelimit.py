"""A token bucket per client, for the one thing a public demo really needs.

Every endpoint here is read-only, so the risk is not damage but cost: one
`/api/variants` call scans the span table, and nothing stopped a script from
asking for that a thousand times a second. On a laptop bound to 127.0.0.1 that
did not matter. On a public URL it is the whole attack surface.

Deliberately in-process and in-memory. A shared store would mean another
service to run, and the thing being protected is a single container holding a
read-only file -- if it falls over, restarting it loses nothing.

Two properties worth stating because they are easy to get wrong:

  * The client table is capped. An attacker rotating source addresses would
    otherwise grow it without bound, which turns a rate limiter into the
    memory exhaustion it was meant to prevent.
  * Buckets refill continuously rather than resetting on a window boundary,
    so a caller cannot save up a burst by waiting for the top of a minute.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Allow `per_minute` requests per client, tolerating bursts of `burst`."""

    def __init__(self, per_minute: float = 120.0, burst: int = 40,
                 max_clients: int = 4096, clock=time.monotonic):
        # A rate of zero would divide by zero on the first refused request,
        # turning a misconfigured env var into a 500 on every call rather than
        # the 429 it was reaching for.
        if per_minute <= 0:
            raise ValueError(f"per_minute must be positive, got {per_minute}")
        if burst < 1:
            raise ValueError(f"burst must be at least 1, got {burst}")
        self.rate = per_minute / 60.0
        self.burst = float(burst)
        self.max_clients = max_clients
        self._clock = clock
        self._lock = threading.Lock()
        # client -> (tokens, last seen). Ordered by insertion, which is what
        # makes the eviction below "oldest first" without a second structure.
        self._buckets: dict[str, tuple[float, float]] = {}

    def check(self, client: str) -> float:
        """Consume a token. Returns 0.0 if allowed, else seconds to wait."""
        with self._lock:
            # Read inside the lock. Read outside it and two threads can commit
            # timestamps out of order, so a bucket refills by a negative amount
            # and the caller is refused for being early.
            now = self._clock()
            tokens, last = self._buckets.pop(client, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)

            if tokens < 1.0:
                self._buckets[client] = (tokens, now)
                return max((1.0 - tokens) / self.rate, 0.001)

            self._buckets[client] = (tokens - 1.0, now)
            if len(self._buckets) > self.max_clients:
                # Drop the least recently seen. Re-inserting on every request
                # above keeps insertion order equal to recency order.
                self._buckets.pop(next(iter(self._buckets)))
            return 0.0

    @property
    def tracked(self) -> int:
        with self._lock:
            return len(self._buckets)
