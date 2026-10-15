# LEARNING.md: Payment ledger (built phase by phase)

## Phase 1: Models + accounts endpoints, Postgres in docker compose

**What was built**
- `docker-compose.yml`: Postgres 16 + the FastAPI service. The API waits for Postgres's healthcheck.
- `app/models.py`: the `accounts` table as a SQLAlchemy class, with a `CHECK (balance_paise >= 0)` constraint.
- `app/database.py`: one engine (a connection pool) per process and `init_db()` to create tables.
- `app/main.py`: `POST /accounts`, `GET /accounts/{id}`, `GET /health`, and a 503 handler for DB outages.
- `tests/`: real-Postgres tests against a separate `ledger_test` database, emptied before every test.

**Key concepts**
- *Money as integers.* Balances are stored as paise in a BIGINT. Floats can't represent 0.1 exactly.
- *Validation at the edge.* Pydantic `strict=True` rejects `"100"` and `10.5`. Bad input never reaches the DB.
- *Constraints as the last line of defence.* Even if Python has a bug, Postgres refuses a negative balance.
- *Connection pooling.* Opening a Postgres connection is slow, so the engine keeps up to 15 open and reuses them.

**Break it to learn it**
1. Change `balance_paise` to a `Float` column and `initial_balance_paise` to `float`. Create an
   account with 0.1 and add 0.2 in Python. Compare with 0.3 and see why money is never a float.
2. Remove `strict=True` from `AccountCreate`, then POST `"initial_balance_paise": "100"`. It is
   silently accepted as 100. Is that what a payments API should do?
3. Run `docker compose stop postgres` and call `GET /accounts/1`. You get a 503, not a crash. Start
   Postgres again: the next request works, because `pool_pre_ping` throws away the dead connections.

## Phase 2: Transfers + double-entry bookkeeping

**What was built**
- `transfers` and `ledger_entries` tables. Each transfer has exactly one DEBIT (sender) and one CREDIT (receiver) entry.
- A seeded system account `external_funding` (id 1). Opening an account with money is a transfer
  *from* it, so every paisa in the system is backed by ledger entries. It is the only account that may go negative.
- `POST /transfers`, `GET /transfers/{id}`, `GET /accounts/{id}/statement` (with a running balance).
- `ledger.check_invariants()`: an audit that must always return three zeros:
  1. the sum of all balances,
  2. accounts whose balance ≠ credits − debits,
  3. transfers without a matching DEBIT/CREDIT pair.

**Key concepts**
- *Double-entry.* Money is never created or destroyed, only moved. With two legs per transfer, you can
  *prove* this from the data (the sum is always 0) and rebuild any balance from its history.
- *Atomic transaction.* `_move_money()` does 4 writes (transfer, 2 entries, 2 balances), but there is only
  one `commit()`. If anything fails before it, Postgres rolls back all of them.
- *Window function.* `SUM(...) OVER (ORDER BY e.id)` produces the running balance in the statement.
- *Business errors vs bugs.* `LedgerError` subclasses become 404/422. Real failures stay 500/503.

**Known problem left on purpose for Phase 4.** `transfer()` reads the balance, checks it in Python,
then writes a new value. Two requests at the same time can both read ₹100, both pass the check, and both
subtract. That is a *race condition* (a lost update, or a negative balance stopped only by the CHECK constraint).

**Break it to learn it**
1. In `_move_money()`, comment out the CREDIT entry. Run `pytest tests/test_invariants.py`: the audit
   reports `transfers_unbalanced` and `accounts_mismatched`. This is exactly why we keep two legs.
2. In `ledger.transfer()`, raise an exception right after `_move_money()` and before `commit()`.
   Call the API, then check balances: nothing changed. That is atomicity.
3. Remove the `src.is_system or dst.is_system` check and POST a transfer from account 1. You have
   just printed money. Then run `check_invariants`: why does the sum stay 0 even so?
