# Tests for the HTML pages in app/web.py: they render, forms move money through the same rules as
# the API (double-entry, idempotency, no overdraft), bad input is explained on the page, and the
# ledger invariants still hold afterwards.

import re

import pytest

import ledger
import web
from helpers import balance, open_account


def form_key(html: str) -> str:
    """The hidden idempotency key the server put into a rendered send-money form."""
    return re.search(r'name="idempotency_key" value="([^"]+)"', html).group(1)


def submit(client, src, dst, amount: str, key: str):
    return client.post("/ui/transfers", follow_redirects=False,
                       data={"from_account_id": str(src), "to_account_id": str(dst),
                             "amount": amount, "idempotency_key": key})


def test_home_shows_health_and_accounts(client):
    open_account(client, "Asha", 50_000)
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "BALANCED" in r.text
    assert "Asha" in r.text and "500.00" in r.text  # 50,000 paise shown as rupees


def test_open_account_via_form_redirects_to_its_page(client):
    r = client.post("/ui/accounts", data={"owner_name": "Ravi", "initial_balance": "1234.50"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"])
    assert "Ravi" in page.text and "1,234.50" in page.text
    assert "CREDIT" in page.text  # the opening balance is a real transfer from the system account


def test_transfer_via_form_writes_debit_and_credit(client):
    a, b = open_account(client, "A", 10_000), open_account(client, "B", 0)
    key = form_key(client.get(f"/ui/send?from={a}").text)
    r = submit(client, a, b, "25.75", key)
    assert r.status_code == 303 and "replayed" not in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "DEBIT" in page and "CREDIT" in page and "25.75" in page
    assert balance(client, a) == 10_000 - 2_575 and balance(client, b) == 2_575


def test_submitting_the_same_form_twice_moves_money_once(client):
    a, b = open_account(client, "A", 10_000), open_account(client, "B", 0)
    key = form_key(client.get("/ui/send").text)
    first = submit(client, a, b, "10", key)
    second = submit(client, a, b, "10", key)  # double click / browser resubmit
    assert first.status_code == second.status_code == 303
    assert second.headers["location"] == first.headers["location"] + "?replayed=1"
    assert "no money moved again" in client.get(second.headers["location"]).text
    assert balance(client, a) == 9_000 and balance(client, b) == 1_000


def test_overdraft_is_refused_and_form_gets_a_new_key(client):
    a, b = open_account(client, "A", 500), open_account(client, "B", 0)
    key = form_key(client.get("/ui/send").text)
    r = submit(client, a, b, "6", key)  # ₹6 = 600 paise > 500
    assert r.status_code == 422 and "insufficient funds" in r.text
    assert form_key(r.text) != key  # the old key now holds the refusal as its final answer
    assert balance(client, a) == 500 and balance(client, b) == 0


@pytest.mark.parametrize("amount, message", [
    ("1.005", "at most 2 decimal places"),
    ("abc", "must be a number"),
    ("NaN", "must be a number"),
    ("-5", "greater than 0"),
    ("0", "greater than 0"),
])
def test_bad_amounts_are_explained_and_nothing_moves(client, amount, message):
    a, b = open_account(client, "A", 10_000), open_account(client, "B", 0)
    r = submit(client, a, b, amount, "key-" + amount)
    assert r.status_code == 422 and message in r.text
    assert balance(client, a) == 10_000


def test_parse_rupees_is_exact():
    assert web.parse_rupees("0.10") == 10
    assert web.parse_rupees("0.29") == 29  # int(float("0.29") * 100) would give 28
    assert web.parse_rupees(" 1234.5 ") == 123_450


def test_owner_name_is_escaped(client):
    open_account(client, "<script>alert(1)</script>", 0)
    html = client.get("/").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_invariants_hold_after_using_the_forms(client, session):
    a, b = open_account(client, "A", 10_000), open_account(client, "B", 0)
    for amount in ["1", "2.50", "99999"]:  # the last one is refused
        submit(client, a, b, amount, form_key(client.get("/ui/send").text))
    assert ledger.check_invariants(session) == {
        "total_balance_paise": 0, "accounts_mismatched": 0, "transfers_unbalanced": 0}
