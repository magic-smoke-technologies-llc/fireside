"""A token-bucket rate limiter, for the gate before our key goes on a hosted box.

Hosting with the server's provider key means every request spends our money, so
the gate is checked before a run starts. Keyed by user id. Pure and clock-
injectable so it is testable without sleeping.
"""

import threading
import time

PRUNE_ABOVE = 4096        # keys held before idle ones are dropped


class RateLimiter:
    def __init__(self, rate_per_min, burst=None):
        self.rate_per_min = float(rate_per_min)
        self.capacity = float(burst if burst is not None else max(1.0, rate_per_min))
        self._state = {}                 # key -> (tokens, last_ts)
        # Request threads share one limiter: without the lock two callers can
        # both read the last token and both pass.
        self._lock = threading.Lock()

    def allow(self, key, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._state) > PRUNE_ABOVE:
                self._prune(now)
            tokens, last = self._state.get(key, (self.capacity, now))
            tokens = min(self.capacity,
                         tokens + (now - last) * (self.rate_per_min / 60.0))
            if tokens < 1.0:
                self._state[key] = (tokens, now)
                return False
            self._state[key] = (tokens - 1.0, now)
            return True

    def _prune(self, now):
        """Forget keys whose bucket has refilled: a full bucket is what a key
        we never saw starts with, so dropping it changes no answer. Without
        this, a caller who varies its key grows the table for ever."""
        rate = self.rate_per_min / 60.0
        self._state = {
            k: (t, last) for k, (t, last) in self._state.items()
            if t + (now - last) * rate < self.capacity}
