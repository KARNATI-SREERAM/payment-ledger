# Failure drill: "what happens if the database dies in the middle of transfers?"
# A client sends 300 transfers of ₹1 (A -> B), one at a time, each with its own Idempotency-Key.
# After the 100th, a background thread SIGKILLs the Postgres container and starts it again 2 s
# later. The client behaves like a correct payments client: on a 503, a connection error or a
# timeout it retries THE SAME KEY until it gets a final answer (201 or 422).
# Afterwards we check that exactly 300 transfers exist and A and B moved by exactly ₹300: nothing
# was lost (every key eventually succeeded) and nothing doubled (no retry moved money twice).
# Run (stack must be up): .venv/bin/python benchmark/failure_drill.py

import os
import subprocess
import threading
import time
import uuid

import httpx

N = 300
KILL_AFTER = 100
AMOUNT = 100  # paise
PROJECT_DIR = os.path.join(os.path.dirname(__file__), "..")
HEADERS = {"X-Client-Id": "failure-drill"}


def compose(*args):
    subprocess.run(["docker", "compose", *args], cwd=PROJECT_DIR, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def crash_postgres(events):
    events["killed_at"] = time.perf_counter()
    compose("kill", "postgres")  # SIGKILL: no clean shutdown, like pulling the power cable
    time.sleep(2)
    compose("start", "postgres")
    events["restarted_at"] = time.perf_counter()


def check(label, actual, expected):
    print(f"{'PASS' if actual == expected else 'FAIL'}  {label}: {actual} (expected {expected})")


def main():
    http = httpx.Client(base_url="http://localhost:8001", timeout=10, headers=HEADERS)
    ids = []
    for name, paise in [("drill-A", 1_000_000), ("drill-B", 0)]:
        r = http.post("/accounts", json={"owner_name": name, "initial_balance_paise": paise})
        r.raise_for_status()
        ids.append(r.json()["id"])
    a, b = ids

    events, attempts, failures, replays, rate_limited = {}, 0, {}, 0, 0
    first_failure = recovered = None
    killer = None
    start = time.perf_counter()
    for i in range(N):
        if i == KILL_AFTER:
            killer = threading.Thread(target=crash_postgres, args=(events,))
            killer.start()
        key = str(uuid.uuid4())
        body = {"from_account_id": a, "to_account_id": b, "amount_paise": AMOUNT}
        while True:  # retry the same key until we get a final answer
            attempts += 1
            try:
                r = http.post("/transfers", json=body, headers={"Idempotency-Key": key})
                outcome = r.status_code
            except httpx.TransportError as e:  # connection refused/reset, timeout
                outcome = type(e).__name__
            if outcome == 429:  # our own rate limit, not the outage: wait as the server asks
                rate_limited += 1
                time.sleep(int(r.headers["Retry-After"]))
                continue
            if outcome in (201, 422):
                if first_failure is not None and recovered is None:
                    recovered = time.perf_counter()
                replays += r.headers.get("Idempotent-Replayed") == "true"
                break
            failures[outcome] = failures.get(outcome, 0) + 1
            if first_failure is None:
                first_failure = time.perf_counter()
            time.sleep(0.2)  # back off a little before retrying
    killer.join()
    total = time.perf_counter() - start

    print(f"transfers requested={N} attempts={attempts} failed_attempts={sum(failures.values())} "
          f"by_type={failures} replayed_responses={replays} rate_limited_429={rate_limited}")
    if first_failure is not None:
        print(f"Postgres SIGKILLed at t={events['killed_at'] - start:.2f} s, restarted at "
              f"t={events['restarted_at'] - start:.2f} s; first failed request "
              f"t={first_failure - start:.2f} s; first success after it "
              f"t={recovered - start:.2f} s "
              f"(client-visible outage {recovered - first_failure:.2f} s); total {total:.2f} s")
    else:
        print(f"no request failed (total {total:.2f} s)")
    http.headers["X-Client-Id"] = "failure-drill-verify"  # fresh rate-limit bucket for checks
    entries = http.get(f"/accounts/{a}/statement?limit=500").json()["entries"]
    debits = sum(e["direction"] == "DEBIT" for e in entries)
    check("transfers recorded from A", debits, N)
    check("balance of A in paise", http.get(f"/accounts/{a}").json()["balance_paise"],
          1_000_000 - N * AMOUNT)
    check("balance of B in paise", http.get(f"/accounts/{b}").json()["balance_paise"], N * AMOUNT)


if __name__ == "__main__":
    main()
