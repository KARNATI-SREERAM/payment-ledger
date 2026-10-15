-- Runs once, the first time the Postgres container starts with an empty volume.
-- Creates a SEPARATE database for pytest, because the tests wipe their tables before every test
-- and must never touch the data in the main `ledger` database.
CREATE DATABASE ledger_test OWNER ledger;
