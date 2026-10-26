# Audits the running ledger database from your Mac and prints one line of facts: the three
# invariants plus some counts, from ledger.stats(). Exits with status 1 if any invariant
# is broken, so scripts can stop on it. Used by scripts/measure_all.sh.
# Run: .venv/bin/python scripts/check_ledger.py   (DATABASE_URL defaults to localhost:5434/ledger)

import os
import sys

# Reuse the app's own audit query instead of copying it (one source of truth).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import database  # noqa: E402
import ledger  # noqa: E402


def main() -> int:
    with database.SessionLocal() as session:
        facts = ledger.stats(session)
    healthy = facts.pop("healthy")
    print(" ".join(f"{k}={v}" for k, v in facts.items()))
    print("PASS  ledger invariants hold" if healthy else "FAIL  ledger invariants BROKEN")
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
