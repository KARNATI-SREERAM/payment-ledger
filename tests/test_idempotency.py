# Tests for Idempotency-Key on POST /transfers: a retry returns the original result and never
# moves money twice, misuse of a key is refused, and a crash before commit leaves no trace.

import idempotency
from helpers import balance, open_account, send


def test_header_is_required(client):
    a, b = open_account(client, "A", 1_000), open_account(client, "B", 0)
    r = client.post("/transfers", json={"from_account_id": a, "to_account_id": b,
                                        "amount_paise": 100})  # no Idempotency-Key header
    assert r.status_code == 400
    assert balance(client, a) == 1_000


def test_retry_returns_original_result_and_moves_money_once(client):
    a, b = open_account(client, "A", 10_000), open_account(client, "B", 0)
    first = send(client, a, b, 1_000, key="order-42")
    retries = [send(client, a, b, 1_000, key="order-42") for _ in range(5)]

    assert first.status_code == 201
    assert "Idempotent-Replayed" not in first.headers
    for r in retries:
        assert r.status_code == 201
        assert r.content == first.content          # byte-for-byte the same response
        assert r.headers["Idempotent-Replayed"] == "true"
    assert balance(client, a) == 9_000             # moved ONCE, not six times
    assert balance(client, b) == 1_000


def test_same_key_with_different_body_is_refused(client):
    a, b = open_account(client, "A", 10_000), open_account(client, "B", 0)
    send(client, a, b, 1_000, key="k1")
    r = send(client, a, b, 9_000, key="k1")        # same key, different amount
    assert r.status_code == 422
    assert balance(client, a) == 9_000             # the second transfer did not happen


def test_a_refusal_is_replayed_too(client):
    # Insufficient funds is a final answer for that key, even after the account is topped up.
    # Otherwise an old retry could suddenly go through hours later, which would surprise the user.
    a, b = open_account(client, "A", 100), open_account(client, "B", 5_000)
    assert send(client, a, b, 500, key="k2").status_code == 422
    send(client, b, a, 1_000)                      # A now has enough money
    r = send(client, a, b, 500, key="k2")
    assert r.status_code == 422
    assert r.headers["Idempotent-Replayed"] == "true"
    assert balance(client, a) == 1_100


def test_different_keys_are_different_transfers(client):
    a, b = open_account(client, "A", 10_000), open_account(client, "B", 0)
    send(client, a, b, 1_000, key="k-a")
    send(client, a, b, 1_000, key="k-b")
    assert balance(client, a) == 8_000


def test_key_too_long_is_422(client):
    a, b = open_account(client, "A", 1_000), open_account(client, "B", 0)
    assert send(client, a, b, 1, key="x" * 256).status_code == 422


def test_crash_before_commit_leaves_the_key_free(client, session):
    # Simulates the server dying mid-request: the key is claimed but the transaction never
    # commits. Postgres rolls it back, so the client's retry runs the transfer normally.
    a, b = open_account(client, "A", 1_000), open_account(client, "B", 0)
    body = {"from_account_id": a, "to_account_id": b, "amount_paise": 300}
    assert idempotency.claim(session, "k3", idempotency.hash_request(body)) is None
    session.rollback()                             # the "crash"
    r = send(client, a, b, 300, key="k3")
    assert r.status_code == 201
    assert "Idempotent-Replayed" not in r.headers
    assert balance(client, a) == 700


def test_hash_ignores_field_order():
    assert idempotency.hash_request({"a": 1, "b": 2}) == idempotency.hash_request({"b": 2, "a": 1})
