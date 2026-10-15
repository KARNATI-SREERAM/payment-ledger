# Database plumbing: ONE SQLAlchemy engine (a pool of PostgreSQL connections) for the whole
# process, a session factory, and init_db() which creates the tables at startup.
# Kept separate from the business logic so tests can point it at a different database
# simply by setting DATABASE_URL before this module is imported.

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models

# "postgresql+psycopg" = PostgreSQL dialect, using the psycopg 3 driver underneath.
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://ledger:ledger@localhost:5434/ledger")

engine = create_engine(
    DATABASE_URL,
    # At most pool_size + max_overflow open connections; a request waits pool_timeout seconds
    # for a free one and then fails (we turn that into a 503), instead of hanging forever.
    pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
    max_overflow=5,
    pool_timeout=5,
    # Test each pooled connection with a cheap ping before using it. After Postgres restarts,
    # every pooled connection is dead; this makes the API reconnect instead of failing forever.
    pool_pre_ping=True,
    connect_args={"connect_timeout": 5},
)

# expire_on_commit=False: objects stay readable after commit (we return them as JSON afterwards).
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Create any missing tables. Safe to run on every startup (existing tables are left alone)."""
    models.Base.metadata.create_all(engine)
