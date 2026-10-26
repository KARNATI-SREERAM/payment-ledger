# FastAPI application: the HTTP layer. Each route validates input (via schemas.py), opens a
# database session, calls the business logic in ledger.py, and turns the result into JSON.
# The HTML pages (web.py) are mounted here too; they call the same functions as these routes.
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
import web
from database import get_session
from ratelimit import RateLimiter
from schemas import (
    MAX_ID,
    AccountCreate,
    AccountOut,
    StatementOut,
    TransferCreate,
    transfer_json,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create tables once at startup, not on every request
    yield


app = FastAPI(title="Payment ledger", lifespan=lifespan)
app.include_router(web.router)  # the HTML pages: /, /ui/accounts/..., /ui/send, /ui/transfers/...

# Ids are Postgres BIGINTs. Without these bounds, /accounts/99999999999999999999 would reach the
# database, fail there and come back as a 500 error instead of a clean 422.
AccountId = Annotated[int, Path(ge=1, le=MAX_ID)]
TransferId = Annotated[int, Path(ge=1, le=MAX_ID)]

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


@app.get("/health")
def health(session: Session = Depends(get_session)):
    session.execute(text("SELECT 1"))  # proves the database is reachable, not just the API
    return {"status": "ok"}


@app.get("/stats")
def stats(session: Session = Depends(get_session)):
    # Read-only audit + counts, for dashboards and the LedgerOps Copilot's get_stats tool.
    # It scans every table: fine at this size, but at scale it would be cached or sampled.
    return ledger.stats(session)


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


@app.post("/transfers", status_code=201)
def create_transfer(
    body: TransferCreate,
    # FastAPI reads the "Idempotency-Key" HTTP header into this argument.
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
    session: Session = Depends(get_session),
):
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    # Claim key -> move money -> store response -> commit, all in idempotency.process_transfer().
    status, payload, replayed = idempotency.process_transfer(session, idempotency_key, body)
    headers = {"Idempotent-Replayed": "true"} if replayed else None
    return JSONResponse(payload, status_code=status, headers=headers)


@app.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: TransferId, session: Session = Depends(get_session)):
    found = ledger.get_transfer(session, transfer_id)
    if found is None:
        raise HTTPException(status_code=404, detail="transfer not found")
    return transfer_json(found)
