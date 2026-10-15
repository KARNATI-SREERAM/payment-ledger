# Per-client rate limiting with a token bucket, kept in this process's memory.
# Why: one misbehaving client (a retry loop with no back-off, a script, an attacker) must not be
# able to use up the database connections that every other client needs.
#
# Token bucket: each client has a bucket that holds at most `burst` tokens and refills at `rate`
# tokens per second. A request spends one token; with no token left it gets HTTP 429.
# So a client can briefly burst up to `burst` requests, but its long-run average is `rate`/s.

import time


class RateLimiter:
    def __init__(self, rate_per_sec: float, burst: int, clock=time.monotonic):
        self.rate = rate_per_sec
        self.burst = burst
        # monotonic, not time.time(): it never jumps backwards when the system clock is adjusted.
        # Tests pass a fake clock so they don't have to sleep.
        self.clock = clock
        self.buckets: dict[str, tuple[float, float]] = {}  # client -> (tokens left, last update)

    def allow(self, client_id: str) -> tuple[bool, float]:
        """Spend one token for this client. Returns (allowed, seconds until a token is free)."""
        now = self.clock()
        tokens, last = self.buckets.get(client_id, (self.burst, now))  # new clients start full
        # Refill lazily: add the tokens earned since the last request instead of running a timer.
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens >= 1:
            self.buckets[client_id] = (tokens - 1, now)
            return True, 0.0
        self.buckets[client_id] = (tokens, now)
        return False, (1 - tokens) / self.rate
