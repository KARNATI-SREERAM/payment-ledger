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
