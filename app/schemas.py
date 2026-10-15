# Request and response shapes for the API (Pydantic models). FastAPI uses them to validate
# incoming JSON (bad input -> automatic 422) and to build the JSON it sends back.

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

MAX_PAISE = 10**12  # ₹1,000 crore: a sanity cap so absurd numbers are rejected at the door


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
    created_at: datetime
