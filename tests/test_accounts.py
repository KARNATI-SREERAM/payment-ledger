# Tests for opening and reading accounts: the happy path, input validation, and 404s.

import pytest


def test_create_and_get_account(client):
    r = client.post("/accounts", json={"owner_name": "Asha", "initial_balance_paise": 50_000})
    assert r.status_code == 201
    created = r.json()
    assert created["owner_name"] == "Asha"
    assert created["balance_paise"] == 50_000

    r = client.get(f"/accounts/{created['id']}")
    assert r.status_code == 200
    assert r.json() == created  # reading it back gives exactly what was created


def test_initial_balance_defaults_to_zero(client):
    r = client.post("/accounts", json={"owner_name": "Ravi"})
    assert r.status_code == 201
    assert r.json()["balance_paise"] == 0


@pytest.mark.parametrize("body", [
    {"owner_name": "X", "initial_balance_paise": -1},       # negative money
    {"owner_name": "X", "initial_balance_paise": 10.5},     # fractional paise
    {"owner_name": "X", "initial_balance_paise": "100"},    # string, not a number (strict mode)
    {"owner_name": "", "initial_balance_paise": 100},       # empty name
    {"owner_name": "X", "inital_balance_paise": 100},       # typo'd field (extra="forbid")
    {"initial_balance_paise": 100},                         # missing name
])
def test_invalid_account_requests_are_rejected(client, body):
    assert client.post("/accounts", json=body).status_code == 422


def test_unknown_account_is_404(client):
    assert client.get("/accounts/999999").status_code == 404


def test_out_of_range_account_id_is_422_not_500(client):
    # Bigger than Postgres BIGINT: must be rejected by validation, not crash the database query.
    assert client.get(f"/accounts/{2**63}").status_code == 422


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
