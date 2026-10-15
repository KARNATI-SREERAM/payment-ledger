# Tests for the ledger audit (ledger.check_invariants): it reports a healthy ledger as all zeros
# after many random transfers, and it really does catch corruption when we tamper with the data.

import random

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import ledger

HEALTHY = {"total_balance_paise": 0, "accounts_mismatched": 0, "transfers_unbalanced": 0}


def test_invariants_hold_after_many_random_transfers(session):
    rng = random.Random(7)  # fixed seed: the same "random" transfers on every run
    ids = [ledger.create_account(session, f"user{i}", rng.randint(0, 50_000)).id
           for i in range(20)]
    customer_total = sum(ledger.get_account(session, i).balance_paise for i in ids)
    for _ in range(500):
        src, dst = rng.sample(ids, 2)
        try:
            ledger.transfer(session, src, dst, rng.randint(1, 20_000))
        except ledger.InsufficientFunds:
            session.rollback()
    assert ledger.check_invariants(session) == HEALTHY
    # Transfers between customers never change the total customer money.
    assert sum(ledger.get_account(session, i).balance_paise for i in ids) == customer_total


def test_audit_detects_a_balance_that_disagrees_with_its_entries(session):
    a = ledger.create_account(session, "A", 5_000)
    # Simulate a bug that updates a balance without writing a ledger entry.
    session.execute(text("UPDATE accounts SET balance_paise = balance_paise + 1 WHERE id = :id"),
                    {"id": a.id})
    result = ledger.check_invariants(session)
    assert result["accounts_mismatched"] == 1
    assert result["total_balance_paise"] == 1  # one paisa appeared from nowhere


def test_audit_detects_a_transfer_with_a_missing_leg(session):
    a, b = ledger.create_account(session, "A", 5_000), ledger.create_account(session, "B", 0)
    t = ledger.transfer(session, a.id, b.id, 100)
    session.execute(
        text("DELETE FROM ledger_entries WHERE transfer_id = :t AND direction = 'CREDIT'"),
        {"t": t.id})
    assert ledger.check_invariants(session)["transfers_unbalanced"] == 1


def test_database_refuses_a_negative_customer_balance(session):
    # Even if application code skipped the funds check, the CHECK constraint would stop it.
    a = ledger.create_account(session, "A", 0)
    with pytest.raises(IntegrityError):
        session.execute(text("UPDATE accounts SET balance_paise = -1 WHERE id = :id"), {"id": a.id})
