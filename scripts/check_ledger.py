# Audits the running ledger database from your Mac and prints one line of facts: the three
# invariants from ledger.check_invariants() plus some counts. Exits with status 1 if any
# invariant is broken, so scripts can stop on it. Used by scripts/measure_all.sh.
# Run: .venv/bin/python scripts/check_ledger.py   (DATABASE_URL defaults to localhost:5434/ledger)

import os
import sys

# Reuse the app's own audit query instead of copying it (one source of truth).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from sqlalchemy import text  # noqa: E402

import database  # noqa: E402
import ledger  # noqa: E402

COUNTS_SQL = text("""
    SELECT (SELECT count(*) FROM accounts WHERE NOT is_system) AS customer_accounts,
           (SELECT count(*) FROM transfers t JOIN accounts a ON a.id = t.from_account_id
             WHERE NOT a.is_system) AS customer_transfers,
           (SELECT count(*) FROM ledger_entries) AS ledger_entries,
           (SELECT count(*) FROM idempotency_keys) AS idempotency_keys,
           (SELECT COALESCE(min(balance_paise), 0) FROM accounts WHERE NOT is_system)
             AS min_customer_balance_paise
""")


def main() -> int:
    with database.SessionLocal() as session:
        facts = {**ledger.check_invariants(session),
                 **dict(session.execute(COUNTS_SQL).one()._mapping)}
    print(" ".join(f"{k}={v}" for k, v in facts.items()))
    healthy = (facts["total_balance_paise"] == 0 and facts["accounts_mismatched"] == 0
               and facts["transfers_unbalanced"] == 0)
    print("PASS  ledger invariants hold" if healthy else "FAIL  ledger invariants BROKEN")
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
