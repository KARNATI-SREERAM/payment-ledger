# FastAPI application: the HTTP layer. Each route validates input (via schemas.py), opens a
# database session, calls the business logic in ledger.py, and turns the result into JSON.
# Run locally: uvicorn main:app --app-dir app   (in compose: the `api` service, localhost:8001)

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import database
import ledger
from schemas import AccountCreate, AccountOut


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create tables once at startup, not on every request
    yield


app = FastAPI(title="Payment ledger", lifespan=lifespan)

# Ids are Postgres BIGINTs. Without these bounds, /accounts/99999999999999999999 would reach the
# database, fail there and come back as a 500 error instead of a clean 422.
AccountId = Annotated[int, Path(ge=1, le=2**63 - 1)]


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
