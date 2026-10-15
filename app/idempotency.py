# Idempotency keys for POST /transfers: the client sends a unique Idempotency-Key header per
# transfer it wants to make, and reuses it when retrying (after a timeout, a dropped connection,
# a 503...). The FIRST request with a key does the work; every later request with that key gets
# the stored response back. So a retry can never move money twice.
#
# Everything happens inside the caller's database transaction:
#   claim key -> move money -> save response -> COMMIT (all or nothing).
# If the server or database dies before the commit, the key claim is rolled back too, so the
# client's retry simply does the transfer for the first time. No "stuck" keys.

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import IdempotencyKey


def hash_request(body: dict) -> str:
    # sort_keys: the same body always gives the same hash, whatever order the fields arrived in.
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def claim(session: Session, key: str, request_hash: str) -> IdempotencyKey | None:
    """Try to become the owner of `key`. Returns None if we now own it (go ahead and do the
    transfer), or the existing row if the key was already used (replay its response)."""
    # If another request holding the same key is still in progress, this INSERT WAITS on the
    # unique index until that transaction commits or rolls back. Then:
    #   - it committed  -> ON CONFLICT DO NOTHING inserts nothing, and we read its stored result;
    #   - it rolled back -> our INSERT goes through and we own the key.
    # So two identical requests arriving at the same moment never both move money.
    inserted = session.execute(
        text("""INSERT INTO idempotency_keys (key, request_hash) VALUES (:key, :hash)
                ON CONFLICT (key) DO NOTHING RETURNING key"""),
        {"key": key, "hash": request_hash},
    ).first()
    if inserted:
        return None
    # In READ COMMITTED each statement sees all data committed before it started, so this
    # SELECT sees the row that the other transaction just committed.
    return session.get(IdempotencyKey, key)


def save_response(session: Session, key: str, status: int, body: dict) -> None:
    """Store the response on our claimed key. The caller commits."""
    row = session.get(IdempotencyKey, key)
    row.response_status = status
    row.response_body = body
