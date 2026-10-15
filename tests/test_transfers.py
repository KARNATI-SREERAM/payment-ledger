# Tests for transfers and double-entry bookkeeping: money moves, both ledger legs are written,
# failed transfers change nothing, and statements show a correct running balance.

from helpers import balance, open_account


def send(client, src, dst, paise):
    return client.post("/transfers",
                       json={"from_account_id": src, "to_account_id": dst, "amount_paise": paise})


def test_transfer_moves_money_and_writes_two_entries(client):
    a, b = open_account(client, "A", 10_000), open_account(client, "B", 0)
    r = send(client, a, b, 2_500)
    assert r.status_code == 201
    t = r.json()
    assert (t["from_account_id"], t["to_account_id"], t["amount_paise"]) == (a, b, 2_500)
    # Double entry: one DEBIT on the sender, one CREDIT on the receiver, same amount.
    assert t["entries"] == [
        {"account_id": a, "direction": "DEBIT", "amount_paise": 2_500},
        {"account_id": b, "direction": "CREDIT", "amount_paise": 2_500},
    ]
    assert balance(client, a) == 7_500
    assert balance(client, b) == 2_500
    assert client.get(f"/transfers/{t['id']}").json() == t


def test_opening_balance_is_a_funding_transfer(client):
    # The system account (id 1) pays for every opening balance, so it goes negative.
    a = open_account(client, "A", 10_000)
    assert balance(client, 1) == -10_000
    [line] = client.get(f"/accounts/{a}/statement").json()["entries"]
    assert (line["direction"], line["counterparty_account_id"]) == ("CREDIT", 1)


def test_insufficient_funds_is_rejected_and_changes_nothing(client):
    a, b = open_account(client, "A", 1_000), open_account(client, "B", 0)
    r = send(client, a, b, 1_001)
    assert r.status_code == 422
    assert r.json()["detail"] == "insufficient funds"
    assert (balance(client, a), balance(client, b)) == (1_000, 0)


def test_exact_balance_can_be_sent(client):
    a, b = open_account(client, "A", 1_000), open_account(client, "B", 0)
    assert send(client, a, b, 1_000).status_code == 201
    assert balance(client, a) == 0


def test_unknown_account_is_404(client):
    a = open_account(client, "A", 1_000)
    assert send(client, a, 999, 1).status_code == 404
    assert send(client, 999, a, 1).status_code == 404
    assert balance(client, a) == 1_000


def test_invalid_transfers_are_422(client):
    a, b = open_account(client, "A", 1_000), open_account(client, "B", 0)
    assert send(client, a, a, 1).status_code == 422        # to yourself
    assert send(client, a, b, 0).status_code == 422        # zero amount
    assert send(client, a, b, -5).status_code == 422       # negative amount
    assert send(client, a, b, 1.5).status_code == 422      # fractional paise


def test_system_account_cannot_be_used_in_transfers(client):
    # Otherwise anyone could pull money out of external_funding and create money from nothing.
    a = open_account(client, "A", 0)
    r = send(client, 1, a, 1_000_000)
    assert r.status_code == 422
    assert balance(client, a) == 0


def test_unknown_transfer_is_404(client):
    assert client.get("/transfers/12345").status_code == 404


def test_statement_running_balance(client):
    a, b = open_account(client, "A", 10_000), open_account(client, "B", 0)
    send(client, a, b, 3_000)
    send(client, b, a, 1_000)
    s = client.get(f"/accounts/{a}/statement").json()
    assert s["balance_paise"] == 8_000
    # Newest first: +1000 (from B), -3000 (to B), +10000 (opening balance).
    got = [(e["direction"], e["amount_paise"], e["balance_after_paise"]) for e in s["entries"]]
    assert got == [("CREDIT", 1_000, 8_000), ("DEBIT", 3_000, 7_000), ("CREDIT", 10_000, 10_000)]
    # limit keeps the newest lines and their balance_after is still correct.
    [newest] = client.get(f"/accounts/{a}/statement?limit=1").json()["entries"]
    assert newest["balance_after_paise"] == 8_000


def test_statement_of_unknown_account_is_404(client):
    assert client.get("/accounts/999/statement").status_code == 404
