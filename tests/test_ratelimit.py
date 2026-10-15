# Tests for the per-client token-bucket rate limiter: the algorithm itself (with a fake clock,
# so no sleeping) and the HTTP behaviour (429 + Retry-After, per-client, /health exempt).

import pytest

import main
from ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_burst_then_refill():
    clock = FakeClock()
    limiter = RateLimiter(rate_per_sec=2, burst=3, clock=clock)
    assert [limiter.allow("c")[0] for _ in range(4)] == [True, True, True, False]
    allowed, retry_after = limiter.allow("c")
    assert not allowed and retry_after == pytest.approx(0.5)  # 1 token at 2 tokens/s
    clock.now += 0.5                                          # wait for exactly one token
    assert limiter.allow("c")[0] is True
    assert limiter.allow("c")[0] is False


def test_bucket_never_holds_more_than_burst():
    clock = FakeClock()
    limiter = RateLimiter(rate_per_sec=10, burst=3, clock=clock)
    clock.now += 3600                                         # idle for an hour
    assert [limiter.allow("c")[0] for _ in range(4)] == [True, True, True, False]


def test_clients_have_separate_buckets():
    limiter = RateLimiter(rate_per_sec=1, burst=1, clock=FakeClock())
    assert limiter.allow("alice")[0] is True
    assert limiter.allow("alice")[0] is False
    assert limiter.allow("bob")[0] is True                    # alice's usage doesn't affect bob


def test_api_returns_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(main, "limiter", RateLimiter(rate_per_sec=0.5, burst=2))
    alice = {"X-Client-Id": "alice"}
    codes = [client.get("/accounts/1", headers=alice).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = client.get("/accounts/1", headers=alice)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    assert client.get("/accounts/1", headers={"X-Client-Id": "bob"}).status_code == 200
    assert client.get("/health", headers=alice).status_code == 200  # never rate limited
