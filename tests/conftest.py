# Shared pytest fixtures. Tests run against a REAL PostgreSQL (the `ledger_test` database in the
# compose Postgres), because row locks, constraints and transactions are exactly what we test,
# and an in-memory fake would not behave like Postgres.
# Locally the DB tests are SKIPPED if Postgres isn't running; in CI (REQUIRE_DB=1) they FAIL
# instead, so a broken database setup can't hide behind "skipped".

import os

# Must happen BEFORE `import database`: the engine is created from DATABASE_URL at import time.
TEST_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+psycopg://ledger:ledger@localhost:5434/ledger_test")
os.environ["DATABASE_URL"] = TEST_URL
os.environ.setdefault("DB_POOL_SIZE", "30")  # enough connections for the concurrency tests
# The suite fires hundreds of requests from one test client; only test_ratelimit.py tests limits.
os.environ["RATE_LIMIT_PER_SEC"] = "1000000"
os.environ["RATE_LIMIT_BURST"] = "1000000"

import pytest  # noqa: E402  (imports below must come after the environment is set)
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

import database  # noqa: E402
import main  # noqa: E402
import models  # noqa: E402


@pytest.fixture(scope="session")
def schema():
    """Once per test run: rebuild the tables from the current models (drop + create)."""
    try:
        with database.engine.connect():
            pass
    except OperationalError:
        if os.getenv("REQUIRE_DB") == "1":
            raise
        pytest.skip(f"PostgreSQL not reachable at {TEST_URL} (run `docker compose up -d postgres`)")
    # drop_all first: create_all never changes an existing table, so after a model change the
    # test database would otherwise keep the old columns.
    models.Base.metadata.drop_all(database.engine)
    database.init_db()


@pytest.fixture
def clean_db(schema):
    """Before each test: empty every table so tests can't affect each other."""
    tables = ", ".join(t.name for t in models.Base.metadata.sorted_tables)
    with database.engine.begin() as conn:
        # RESTART IDENTITY resets the id counters, so the first account in every test is id 1.
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    database.init_db()


@pytest.fixture
def session(clean_db):
    with database.SessionLocal() as s:
        yield s


@pytest.fixture
def client(clean_db):
    # Used as a context manager so the app's startup code (lifespan) runs, like in production.
    with TestClient(main.app) as c:
        yield c
