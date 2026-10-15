# Locust load test for the ledger API. Runs inside the official locust image (compose profile
# "loadtest"), so nothing needs installing on your Mac:
#   docker compose --profile loadtest run --rm locust
# At start it opens 100 funded accounts. Then every simulated user, each with its own client id,
# sends a steady 10 requests/s: mostly NEW transfers between random accounts, plus account reads,
# statements, and deliberate RETRIES of its previous transfer (same key, same body).
# A retry counts as a failure unless it is replayed exactly (same status, Idempotent-Replayed).
# At the end it prints how many transfers the clients saw succeed, so measure_all.sh can check
# that the database contains exactly that many.

import random
import time
import uuid

import requests
from locust import HttpUser, constant_throughput, events, task

NUM_ACCOUNTS = 100
OPENING_BALANCE_PAISE = 10_000_000  # ₹1,00,000 each
account_ids: list[int] = []
counts = {"new_201": 0, "new_422_insufficient": 0, "retries_replayed": 0}


@events.test_start.add_listener
def open_accounts(environment, **kwargs):
    account_ids.clear()
    while len(account_ids) < NUM_ACCOUNTS:
        r = requests.post(f"{environment.host}/accounts",
                          json={"owner_name": f"load-{len(account_ids)}",
                                "initial_balance_paise": OPENING_BALANCE_PAISE},
                          headers={"X-Client-Id": "loadtest-setup"}, timeout=10)
        if r.status_code == 429:  # a well-behaved client waits as long as the server asks
            time.sleep(int(r.headers["Retry-After"]))
            continue
        r.raise_for_status()
        account_ids.append(r.json()["id"])


@events.test_stop.add_listener
def report(environment, **kwargs):
    print("client-side transfer outcomes: " + " ".join(f"{k}={v}" for k, v in counts.items()))


class LedgerUser(HttpUser):
    wait_time = constant_throughput(10)  # each user: at most 10 tasks per second

    def on_start(self):
        self.client.headers["X-Client-Id"] = f"user-{uuid.uuid4()}"
        self.last = None  # (key, body, status) of this user's previous transfer

    @task(8)
    def new_transfer(self):
        src, dst = random.sample(account_ids, 2)
        body = {"from_account_id": src, "to_account_id": dst,
                "amount_paise": random.randint(1, 50_000)}
        key = str(uuid.uuid4())
        with self.client.post("/transfers", json=body, headers={"Idempotency-Key": key},
                              name="POST /transfers (new)", catch_response=True) as r:
            if r.status_code == 201:
                counts["new_201"] += 1
            elif r.status_code == 422 and r.json().get("detail") == "insufficient funds":
                counts["new_422_insufficient"] += 1  # a correct business answer, not an error
                r.success()
            else:
                r.failure(f"unexpected {r.status_code}")
                return
            self.last = (key, body, r.status_code)

    @task(1)
    def retry_previous_transfer(self):
        if self.last is None:
            return
        key, body, status = self.last
        with self.client.post("/transfers", json=body, headers={"Idempotency-Key": key},
                              name="POST /transfers (retry)", catch_response=True) as r:
            if r.status_code == status and r.headers.get("Idempotent-Replayed") == "true":
                counts["retries_replayed"] += 1
                r.success()
            else:
                r.failure(f"retry not replayed: {r.status_code}")

    @task(2)
    def read_account(self):
        self.client.get(f"/accounts/{random.choice(account_ids)}", name="GET /accounts/{id}")

    @task(1)
    def read_statement(self):
        self.client.get(f"/accounts/{random.choice(account_ids)}/statement?limit=20",
                        name="GET /accounts/{id}/statement")
