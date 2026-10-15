# Business logic of the ledger, with no HTTP in it: every function takes a database session.
# Keeping it free of FastAPI means tests can call it directly (e.g. from 100 threads at once).

from sqlalchemy.orm import Session

from models import Account


def create_account(session: Session, owner_name: str, initial_balance_paise: int) -> Account:
    account = Account(owner_name=owner_name, balance_paise=initial_balance_paise)
    session.add(account)
    session.commit()  # the INSERT happens here; Postgres assigns account.id
    return account


def get_account(session: Session, account_id: int) -> Account | None:
    return session.get(Account, account_id)  # SELECT ... WHERE id = :id (None if no such row)
