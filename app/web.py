# HTML pages for the ledger, rendered on the server with Jinja2 templates (no JavaScript).
# Why a separate file: main.py stays the JSON API; this file is the browser front end. Both call
# the same ledger.py functions and the same idempotency.process_transfer(), so a payment made from
# the web form follows exactly the same rules (locking, double-entry, idempotency) as the API.
#
# Forms that CHANGE data use POST, and on success answer with a 303 redirect to a page that only
# READS data (Post/Redirect/Get). So pressing refresh re-loads a page; it never re-sends a payment.

import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

import idempotency
import ledger
from database import get_session
from schemas import MAX_ID, AccountCreate, TransferCreate

router = APIRouter()
# Relative to this file, so it works from the repo root and in Docker.
# Jinja2Templates autoescapes .html files: an owner name like "<script>" is shown as text (XSS).
templates = Jinja2Templates(directory=FilePath(__file__).parent / "templates")

AccountId = Annotated[int, Path(ge=1, le=MAX_ID)]
TransferId = Annotated[int, Path(ge=1, le=MAX_ID)]


def rupees(paise: int) -> str:
    """12345 paise -> "123.45". Integer maths only, so no rounding errors."""
    # int(): Postgres SUM() over BIGINT returns NUMERIC, so the statement's running balance
    # arrives as a Decimal (the JSON API's schema converts it to int; templates don't).
    paise = int(paise)
    sign = "-" if paise < 0 else ""
    whole, rest = divmod(abs(paise), 100)
    return f"{sign}{whole:,}.{rest:02d}"


templates.env.filters["rupees"] = rupees  # lets templates write {{ account.balance_paise|rupees }}


def parse_rupees(text: str) -> int:
    """"125.50" -> 12550 paise. Decimal, not float: float("0.29") * 100 is 28.999999999999996."""
    try:
        amount = Decimal(text.strip())
    except InvalidOperation:
        raise ValueError("amount must be a number in rupees, like 125.50")
    if not amount.is_finite():  # Decimal accepts "NaN" and "Infinity"
        raise ValueError("amount must be a number in rupees, like 125.50")
    paise = amount * 100
    if paise != paise.to_integral_value():
        raise ValueError("amount can have at most 2 decimal places (paise)")
    return int(paise)  # range checks (> 0, not too large) are left to the Pydantic schemas


def parse_id(text: str, field: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise ValueError(f"{field} must be an account number, like 2")


def error_message(e: ValueError) -> str:
    """A short, readable message for the page. (Pydantic's ValidationError is a ValueError.)"""
    if isinstance(e, ValidationError):
        first = e.errors()[0]
        field = ".".join(str(part) for part in first["loc"])
        return f"{field}: {first['msg']}" if field else first["msg"]
    return str(e)


def new_key() -> str:
    # A fresh Idempotency-Key for each form the server renders. Submitting the same rendered form
    # twice (double click, browser resubmit) sends the same key, so money moves only once.
    return str(uuid.uuid4())


# ---------------- Home: ledger health, accounts, open-account form ----------------

def render_home(request: Request, session: Session, form: dict, error: str | None = None,
                status_code: int = 200):
    return templates.TemplateResponse(request, "home.html", {
        "health": ledger.check_invariants(session),
        "accounts": ledger.list_accounts(session),
        "form": form, "error": error,
    }, status_code=status_code)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)):
    return render_home(request, session, form={"owner_name": "", "initial_balance": ""})


@router.post("/ui/accounts")
def open_account(request: Request, owner_name: Annotated[str, Form()] = "",
                 initial_balance: Annotated[str, Form()] = "",
                 session: Session = Depends(get_session)):
    # Form fields arrive as text and are typed as str on purpose: we convert them ourselves so a
    # mistake is shown on the page instead of as FastAPI's raw JSON 422 error.
    form = {"owner_name": owner_name, "initial_balance": initial_balance}
    try:
        body = AccountCreate(owner_name=owner_name.strip(),
                             initial_balance_paise=parse_rupees(initial_balance or "0"))
    except ValueError as e:
        return render_home(request, session, form, error_message(e), status_code=422)
    account = ledger.create_account(session, body.owner_name, body.initial_balance_paise)
    # 303 See Other: "the POST worked, now GET this page". A refresh re-GETs, it doesn't re-POST.
    return RedirectResponse(f"/ui/accounts/{account.id}", status_code=303)


# ---------------- One account: balance and statement ----------------

@router.get("/ui/accounts/{account_id}", response_class=HTMLResponse)
def account_page(request: Request, account_id: AccountId,
                 session: Session = Depends(get_session)):
    account = ledger.get_account(session, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return templates.TemplateResponse(request, "account.html", {
        "account": account, "entries": ledger.statement(session, account_id, 50)})


# ---------------- Send money ----------------

def render_send(request: Request, form: dict, error: str | None = None, status_code: int = 200):
    # Always a NEW key: after a refusal, the old key already holds that refusal as its final
    # answer, so a corrected amount must be a new request with a new key.
    return templates.TemplateResponse(request, "send.html",
                                      {"form": form, "key": new_key(), "error": error},
                                      status_code=status_code)


@router.get("/ui/send", response_class=HTMLResponse)
def send_page(request: Request, from_account: Annotated[str, Query(alias="from")] = ""):
    return render_send(request, {"from_account_id": from_account, "to_account_id": "",
                                 "amount": ""})


@router.post("/ui/transfers")
def send_money(request: Request, from_account_id: Annotated[str, Form()] = "",
               to_account_id: Annotated[str, Form()] = "", amount: Annotated[str, Form()] = "",
               idempotency_key: Annotated[str, Form()] = "",
               session: Session = Depends(get_session)):
    form = {"from_account_id": from_account_id, "to_account_id": to_account_id, "amount": amount}
    if not idempotency_key:
        return render_send(request, form, "missing idempotency key: reload the form",
                           status_code=400)
    try:
        body = TransferCreate(from_account_id=parse_id(from_account_id, "from account"),
                              to_account_id=parse_id(to_account_id, "to account"),
                              amount_paise=parse_rupees(amount))
    except ValueError as e:
        return render_send(request, form, error_message(e), status_code=422)

    # The same function the JSON API uses: claim key -> lock -> double-entry -> commit.
    status, payload, replayed = idempotency.process_transfer(session, idempotency_key, body)
    if status == 201:
        url = f"/ui/transfers/{payload['id']}" + ("?replayed=1" if replayed else "")
        return RedirectResponse(url, status_code=303)
    error = payload["detail"]
    if replayed:
        error = f"This form was already submitted. Original answer: {error}. No money moved."
    return render_send(request, form, error, status_code=status)


@router.get("/ui/transfers/{transfer_id}", response_class=HTMLResponse)
def transfer_page(request: Request, transfer_id: TransferId, replayed: bool = False,
                  session: Session = Depends(get_session)):
    transfer = ledger.get_transfer(session, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="transfer not found")
    return templates.TemplateResponse(request, "transfer.html",
                                      {"transfer": transfer, "replayed": replayed})
