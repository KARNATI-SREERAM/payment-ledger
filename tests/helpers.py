# Small helpers shared by several test files (plain functions, not fixtures, to keep them obvious).

import uuid


def open_account(client, name: str, paise: int) -> int:
    r = client.post("/accounts", json={"owner_name": name, "initial_balance_paise": paise})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def balance(client, account_id: int) -> int:
    return client.get(f"/accounts/{account_id}").json()["balance_paise"]


def send(client, src: int, dst: int, paise: int, key: str | None = None):
    """POST /transfers. Each call gets a fresh Idempotency-Key unless the test passes one."""
    return client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount_paise": paise},
        headers={"Idempotency-Key": key or str(uuid.uuid4())})
