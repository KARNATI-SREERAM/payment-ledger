# Tests for GET /stats: the read-only audit snapshot (invariants + counts) used by dashboards
# and by the LedgerOps Copilot's get_stats tool.

from helpers import open_account, send


def test_stats_on_an_empty_ledger(client):
    body = client.get("/stats").json()
    assert body["healthy"] is True
    assert body["customer_accounts"] == 0
    assert body["customer_transfers"] == 0


def test_stats_counts_customers_and_their_transfers(client):
    a = open_account(client, "Asha", 50_000)
    b = open_account(client, "Ravi", 0)
    assert send(client, a, b, 12_000).status_code == 201
    body = client.get("/stats").json()
    assert body["healthy"] is True
    assert body["total_balance_paise"] == 0
    assert body["customer_accounts"] == 2
    assert body["customer_transfers"] == 1     # the opening balance (system account) is not counted
    assert body["ledger_entries"] == 4         # 2 for Asha's funding + 2 for the transfer
    assert body["min_customer_balance_paise"] == 12_000
