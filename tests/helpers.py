# Small helpers shared by several test files (plain functions, not fixtures, to keep them obvious).


def open_account(client, name: str, paise: int) -> int:
    r = client.post("/accounts", json={"owner_name": name, "initial_balance_paise": paise})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def balance(client, account_id: int) -> int:
    return client.get(f"/accounts/{account_id}").json()["balance_paise"]
