# Request and response shapes for the API (Pydantic models). FastAPI uses them to validate
# incoming JSON (bad input -> automatic 422) and to build the JSON it sends back.

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_PAISE = 10**12  # ₹1,000 crore: a sanity cap so absurd numbers are rejected at the door
MAX_ID = 2**63 - 1  # largest Postgres BIGINT


class AccountCreate(BaseModel):
    # strict: "100" or 100.5 or true are NOT silently turned into an integer amount.
    # extra="forbid": a typo like "inital_balance" is an error, not silently ignored.
    model_config = ConfigDict(strict=True, extra="forbid")
    owner_name: str = Field(min_length=1, max_length=100)
    initial_balance_paise: int = Field(default=0, ge=0, le=MAX_PAISE)


class AccountOut(BaseModel):
    # from_attributes: build this directly from an ORM object (reads account.id, .owner_name...)
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner_name: str
    balance_paise: int
    is_system: bool
    created_at: datetime


class TransferCreate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    from_account_id: int = Field(ge=1, le=MAX_ID)
    to_account_id: int = Field(ge=1, le=MAX_ID)
    amount_paise: int = Field(gt=0, le=MAX_PAISE)

    @model_validator(mode="after")
    def different_accounts(self):
        if self.from_account_id == self.to_account_id:
            raise ValueError("from_account_id and to_account_id must be different")
        return self


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    account_id: int
    direction: str
    amount_paise: int


class TransferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    from_account_id: int
    to_account_id: int
    amount_paise: int
    created_at: datetime
    entries: list[EntryOut]  # the two double-entry legs of this transfer


class StatementLine(BaseModel):
    entry_id: int
    transfer_id: int
    direction: str
    amount_paise: int
    counterparty_account_id: int
    balance_after_paise: int
    created_at: datetime


class StatementOut(BaseModel):
    account_id: int
    balance_paise: int
    entries: list[StatementLine]  # newest first


def transfer_json(transfer) -> dict:
    # mode="json": datetimes become strings, so the result can also be stored as JSON
    # (the idempotency record keeps exactly this dict).
    return TransferOut.model_validate(transfer).model_dump(mode="json")
