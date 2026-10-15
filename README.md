# Payment ledger service

A double-entry payment ledger built with FastAPI + PostgreSQL + SQLAlchemy that stays correct
under retries, concurrency and failure. Work in progress: see LEARNING.md for what each phase built.

## Run
```bash
docker compose up --build        # API on http://localhost:8001 (docs: /docs), Postgres on localhost:5434
```

## Test
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
docker compose up -d postgres    # tests use the separate `ledger_test` database inside it
.venv/bin/ruff check . && .venv/bin/pytest -v
```
