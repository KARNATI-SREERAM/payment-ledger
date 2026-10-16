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

import ledger
from models import IdempotencyKey
from schemas import TransferCreate, transfer_json


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


# Business-rule failures from ledger.py -> HTTP status + message. 422 = "understood, but refused".
LEDGER_ERRORS = {
    ledger.AccountNotFound: (404, "account not found"),
    ledger.InsufficientFunds: (422, "insufficient funds"),
    ledger.SystemAccountNotAllowed: (422, "the system account cannot be used in transfers"),
}


def process_transfer(session: Session, key: str, body: TransferCreate) -> tuple[int, dict, bool]:
    """The one path money takes, for both the JSON API and the HTML form.
    Returns (http_status, response_body, replayed). replayed=True means this key was used
    before and the stored answer is returned: no money moved this time."""
    request_hash = hash_request(body.model_dump())

    # Steps 1-4 all run in ONE database transaction; only step 4 commits.
    # 1. Claim the key, or find the response we already gave for it.
    previous = claim(session, key, request_hash)
    if previous is not None:
        if previous.request_hash != request_hash:
            # Same key, different transfer: almost certainly a client bug. Refuse loudly.
            detail = "Idempotency-Key was already used for a different request"
            return 422, {"detail": detail}, False
        return previous.response_status, previous.response_body, True

    # 2. Do the transfer (not committed yet).
    try:
        t = ledger.transfer(session, body.from_account_id, body.to_account_id, body.amount_paise)
        status, payload = 201, transfer_json(t)
    except ledger.LedgerError as e:
        # A refused transfer is ALSO a final answer for this key: a retry gets the same refusal,
        # even if the balance has changed since. (Nothing was written, so no rollback needed.)
        status, message = LEDGER_ERRORS[type(e)]
        payload = {"detail": message}

    # 3. Remember the response under the key.  4. Commit everything together.
    save_response(session, key, status, payload)
    session.commit()
    return status, payload, False
