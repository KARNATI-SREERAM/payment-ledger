# FastAPI application: the HTTP layer. Each route validates input (via schemas.py), opens a
# database session, calls the business logic in ledger.py, and turns the result into JSON.
# Run locally: uvicorn main:app --app-dir app   (in compose: the `api` service, localhost:8001)

import math
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import database
import idempotency
import ledger
from ratelimit import RateLimiter
from schemas import (
    MAX_ID,
    AccountCreate,
    AccountOut,
    StatementOut,
    TransferCreate,
    TransferOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create tables once at startup, not on every request
    yield


app = FastAPI(title="Payment ledger", lifespan=lifespan)

# Ids are Postgres BIGINTs. Without these bounds, /accounts/99999999999999999999 would reach the
# database, fail there and come back as a 500 error instead of a clean 422.
AccountId = Annotated[int, Path(ge=1, le=MAX_ID)]
TransferId = Annotated[int, Path(ge=1, le=MAX_ID)]

# Business-rule failures from ledger.py -> HTTP status + message. 422 = "understood, but refused".
LEDGER_ERRORS = {
    ledger.AccountNotFound: (404, "account not found"),
    ledger.InsufficientFunds: (422, "insufficient funds"),
    ledger.SystemAccountNotAllowed: (422, "the system account cannot be used in transfers"),
}


# Per-client limits. Defaults allow a 100-request burst, then 50 requests/s on average.
limiter = RateLimiter(rate_per_sec=float(os.getenv("RATE_LIMIT_PER_SEC", "50")),
                      burst=int(os.getenv("RATE_LIMIT_BURST", "100")))
NOT_LIMITED = {"/health", "/docs", "/openapi.json"}  # health checks must never be throttled


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    # Runs before every route. It is async, so it runs on the single event-loop thread, one request
    # at a time: the limiter's dict needs no lock. (The sync routes run in a thread pool after it.)
    if request.url.path not in NOT_LIMITED:
        # Who is "a client"? The X-Client-Id header, else the caller's IP address. A real system
        # would use the authenticated API key instead, because a header can be faked.
        client_id = request.headers.get("X-Client-Id") or request.client.host
        allowed, retry_after = limiter.allow(client_id)
        if not allowed:
            return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"},
                                headers={"Retry-After": str(math.ceil(retry_after))})
    return await call_next(request)


@app.exception_handler(OperationalError)  # DB down, connection lost, or pool timeout
def database_unavailable(request: Request, exc: OperationalError):
    # A database outage means "service unavailable, retry later" (503), not a bug in our code (500).
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def get_session():
    # One session per request; `with` closes it (returning the connection to the pool) even if
    # the route raises an exception. Anything not committed is rolled back on close.
    with database.SessionLocal() as session:
        yield session


@app.get("/health")
def health(session: Session = Depends(get_session)):
    session.execute(text("SELECT 1"))  # proves the database is reachable, not just the API
    return {"status": "ok"}


@app.post("/accounts", status_code=201, response_model=AccountOut)
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    return ledger.create_account(session, body.owner_name, body.initial_balance_paise)


@app.get("/accounts/{account_id}", response_model=AccountOut)
def get_account(account_id: AccountId, session: Session = Depends(get_session)):
    account = ledger.get_account(session, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return account


@app.get("/accounts/{account_id}/statement", response_model=StatementOut)
def get_statement(account_id: AccountId, limit: int = Query(50, ge=1, le=500),
                  session: Session = Depends(get_session)):
    account = ledger.get_account(session, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"account_id": account.id, "balance_paise": account.balance_paise,
            "entries": ledger.statement(session, account_id, limit)}


def transfer_json(transfer) -> dict:
    # mode="json": datetimes become strings, so the result can also be stored as JSON (Phase 3).
    return TransferOut.model_validate(transfer).model_dump(mode="json")


@app.post("/transfers", status_code=201)
def create_transfer(
    body: TransferCreate,
    # FastAPI reads the "Idempotency-Key" HTTP header into this argument.
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
    session: Session = Depends(get_session),
):
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    request_hash = idempotency.hash_request(body.model_dump())

    # Steps 1-4 all run in ONE database transaction; only step 4 commits.
    # 1. Claim the key, or find the response we already gave for it.
    previous = idempotency.claim(session, idempotency_key, request_hash)
    if previous is not None:
        if previous.request_hash != request_hash:
            # Same key, different transfer: almost certainly a client bug. Refuse loudly.
            raise HTTPException(status_code=422,
                                detail="Idempotency-Key was already used for a different request")
        return JSONResponse(previous.response_body, status_code=previous.response_status,
                            headers={"Idempotent-Replayed": "true"})

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
    idempotency.save_response(session, idempotency_key, status, payload)
    session.commit()
    return JSONResponse(payload, status_code=status)


@app.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: TransferId, session: Session = Depends(get_session)):
    found = ledger.get_transfer(session, transfer_id)
    if found is None:
        raise HTTPException(status_code=404, detail="transfer not found")
    return transfer_json(found)
