# End-to-end drills against the RUNNING API (docker compose, localhost:8001), over real HTTP.
# The pytest concurrency tests call ledger.py directly; these go through uvicorn, the rate
# limiter, the routes and the connection pool too. Used by scripts/measure_all.sh.
#   1. latency: sequential transfers from one client (no queueing: the API's own speed)
#   2. concurrency: 100 parallel A->B, 50+50 opposite directions, overdraft race, same key x20
#   3. rate limit: one client sends requests as fast as it can; a second client is unaffected
# Run: .venv/bin/python benchmark/http_drills.py [--base-url http://localhost:8001]

import argparse
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx


def check(label, actual, expected):
    print(f"{'PASS' if actual == expected else 'FAIL'}  {label}: {actual} (expected {expected})")


def open_account(http, name, paise):
    r = http.post("/accounts", json={"owner_name": name, "initial_balance_paise": paise},
                  headers={"X-Client-Id": f"setup-{uuid.uuid4()}"})
    r.raise_for_status()
    return r.json()["id"]


def balance(http, account_id):
    return http.get(f"/accounts/{account_id}",
                    headers={"X-Client-Id": "reader"}).json()["balance_paise"]


def transfer(http, src, dst, paise, key=None, client_id=None):
    return http.post("/transfers",
                     json={"from_account_id": src, "to_account_id": dst, "amount_paise": paise},
                     headers={"Idempotency-Key": key or str(uuid.uuid4()),
                              # Each parallel request acts as a different user, so the drill
                              # tests the ledger's locking, not one client's rate limit.
                              "X-Client-Id": client_id or f"drill-{uuid.uuid4()}"})


def run_parallel(fn, arg_list):
    barrier = threading.Barrier(len(arg_list))  # all requests leave at the same instant

    def call(args):
        barrier.wait()
        return fn(*args)

    with ThreadPoolExecutor(max_workers=len(arg_list)) as pool:
        return list(pool.map(call, arg_list))


def percentile(sorted_ms, p):
    return sorted_ms[min(len(sorted_ms) - 1, round(p / 100 * (len(sorted_ms) - 1)))]


def latency_drill(http, n=1000):
    print(f"-- 1. Sequential latency: {n} POST /transfers from one client (after 50 warm-up)")
    a, b = open_account(http, "lat-A", 100_000_000), open_account(http, "lat-B", 0)
    for _ in range(50):
        transfer(http, a, b, 1, client_id="latency")
    times, errors = [], 0
    for i in range(n):
        if i and i % 40 == 0:
            time.sleep(1)  # stay under the 50 req/s per-client limit (burst 100)
        start = time.perf_counter()
        r = transfer(http, a, b, 1, client_id="latency")
        times.append((time.perf_counter() - start) * 1000)
        errors += r.status_code != 201
    t = sorted(times)
    print(f"latency ms: p50={percentile(t, 50):.2f} p95={percentile(t, 95):.2f} "
          f"p99={percentile(t, 99):.2f} mean={statistics.mean(t):.2f} max={t[-1]:.2f}  "
          f"errors={errors}")


def concurrency_drill(http):
    print("-- 2a. 100 parallel transfers A->B of ₹1 each (A starts with ₹1,000)")
    a, b = open_account(http, "par-A", 100_000), open_account(http, "par-B", 0)
    start = time.perf_counter()
    codes = [r.status_code for r in run_parallel(transfer, [(http, a, b, 100)] * 100)]
    print(f"wall time for all 100: {(time.perf_counter() - start) * 1000:.0f} ms")
    check("responses 201", codes.count(201), 100)
    check("balances [A, B] in paise", [balance(http, a), balance(http, b)], [90_000, 10_000])

    print("-- 2b. 50 A->B + 50 B->A at the same time (deadlock bait)")
    a, b = open_account(http, "dl-A", 100_000), open_account(http, "dl-B", 100_000)
    codes = [r.status_code for r in
             run_parallel(transfer, [(http, a, b, 100)] * 50 + [(http, b, a, 100)] * 50)]
    check("responses 201", codes.count(201), 100)
    check("balances unchanged", [balance(http, a), balance(http, b)], [100_000, 100_000])

    print("-- 2c. Overdraft race: A can afford 50 of 100 parallel transfers")
    a, b = open_account(http, "od-A", 5_000), open_account(http, "od-B", 0)
    codes = [r.status_code for r in run_parallel(transfer, [(http, a, b, 100)] * 100)]
    check("responses 201", codes.count(201), 50)
    check("responses 422 insufficient funds", codes.count(422), 50)
    check("final balance of A", balance(http, a), 0)

    print("-- 2d. Same Idempotency-Key sent 20 times in parallel")
    a, b = open_account(http, "idem-A", 10_000), open_account(http, "idem-B", 0)
    key = str(uuid.uuid4())
    responses = run_parallel(transfer, [(http, a, b, 1_000, key)] * 20)
    check("responses 201", [r.status_code for r in responses].count(201), 20)
    check("distinct response bodies", len({r.content for r in responses}), 1)
    check("requests that did the work (not replayed)",
          sum("Idempotent-Replayed" not in r.headers for r in responses), 1)
    check("balance of A (moved once)", balance(http, a), 9_000)


def rate_limit_drill(http, n=300, rate=50, burst=100):
    print(f"-- 3. Rate limit: one client sends {n} GET /accounts/1 back-to-back")
    codes, retry_after = [], set()
    start = time.perf_counter()
    for _ in range(n):
        r = http.get("/accounts/1", headers={"X-Client-Id": "greedy"})
        codes.append(r.status_code)
        if r.status_code == 429:
            retry_after.add(r.headers["Retry-After"])
    elapsed = time.perf_counter() - start
    other_codes = n - codes.count(200) - codes.count(429)
    print(f"in {elapsed:.2f} s: 200={codes.count(200)} 429={codes.count(429)} "
          f"other={other_codes}  Retry-After values={sorted(retry_after)}")
    print(f"first 429 at request #{codes.index(429) + 1 if 429 in codes else 'none'}; "
          f"token bucket allows at most burst + rate x time = {burst} + {rate} x {elapsed:.2f} "
          f"= {burst + rate * elapsed:.0f}")
    other = http.get("/accounts/1", headers={"X-Client-Id": "polite"}).status_code
    check("a different client is not affected", other, 200)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8001")
    args = parser.parse_args()
    # Enough pooled HTTP connections for 100 truly parallel requests.
    limits = httpx.Limits(max_connections=120, max_keepalive_connections=120)
    with httpx.Client(base_url=args.base_url, timeout=30, limits=limits) as http:
        latency_drill(http)
        concurrency_drill(http)
        rate_limit_drill(http)


if __name__ == "__main__":
    main()
