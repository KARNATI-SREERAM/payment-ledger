# Business logic of the ledger, with no HTTP in it: every function takes a database session.
# Keeping it free of FastAPI means tests can call it directly (e.g. from 100 threads at once).
#
# The one rule everything here follows: money only ever moves through _move_money(), which
# writes a DEBIT entry, a CREDIT entry and both balance changes in the SAME database transaction.
# Either all four writes are committed or none are (atomicity), so money can't be lost half-way.
#
# Concurrency: before reading a balance it is about to change, a transaction LOCKS that account
# row (SELECT ... FOR UPDATE). Any other transaction that wants the same row waits until the
# first one commits, then reads the new balance. Locks are always taken in ascending account-id
# order, so two transactions can never each hold one lock while waiting for the other (deadlock).

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import Account, LedgerEntry, Transfer


class LedgerError(Exception):
    """A business-rule failure (not a bug): the API turns each subclass into an HTTP status."""


class AccountNotFound(LedgerError):
    pass


class InsufficientFunds(LedgerError):
    pass


class SystemAccountNotAllowed(LedgerError):
    pass


def seed_system_account(session: Session) -> None:
    """Create the single 'external_funding' system account if it doesn't exist yet."""
    # ON CONFLICT ... DO NOTHING: running this twice (or from two processes) is harmless.
    session.execute(text("""
        INSERT INTO accounts (owner_name, balance_paise, is_system)
        VALUES ('external_funding', 0, true)
        ON CONFLICT (is_system) WHERE is_system DO NOTHING
    """))
    session.commit()


def system_account_id(session: Session) -> int:
    return session.execute(text("SELECT id FROM accounts WHERE is_system")).scalar_one()


def _lock_account(session: Session, account_id: int) -> Account | None:
    """SELECT ... FOR UPDATE: read the row AND lock it until our transaction ends."""
    # populate_existing: overwrite any stale copy SQLAlchemy cached earlier with the fresh,
    # locked values from the database.
    return session.get(Account, account_id, with_for_update=True, populate_existing=True)


def _lock_accounts_in_order(session: Session, *account_ids: int) -> dict[int, Account | None]:
    """Lock several accounts, always lowest id first. This consistent order is what prevents
    deadlocks: A->B and B->A transfers both lock min(A, B) first, so one simply waits."""
    return {i: _lock_account(session, i) for i in sorted(account_ids)}


def _move_money(session: Session, src: Account, dst: Account, amount_paise: int) -> Transfer:
    """Record one transfer as double-entry. Does NOT commit: the caller decides when."""
    # Building the entries inside the Transfer lets SQLAlchemy insert the transfer first and fill
    # in each entry's transfer_id automatically.
    transfer = Transfer(
        from_account_id=src.id, to_account_id=dst.id, amount_paise=amount_paise,
        entries=[
            LedgerEntry(account_id=src.id, direction="DEBIT", amount_paise=amount_paise),
            LedgerEntry(account_id=dst.id, direction="CREDIT", amount_paise=amount_paise),
        ])
    session.add(transfer)
    src.balance_paise -= amount_paise
    dst.balance_paise += amount_paise
    return transfer


def create_account(session: Session, owner_name: str, initial_balance_paise: int) -> Account:
    """Open an account. An opening balance is a real transfer from the system account, so even
    the first rupee in every account is backed by a pair of ledger entries."""
    account = Account(owner_name=owner_name, balance_paise=0)
    session.add(account)
    session.flush()  # get account.id
    if initial_balance_paise > 0:
        # The new account is invisible to others until we commit, so only the shared system
        # account needs a lock. (Every funded opening waits its turn on this one row.)
        funding = _lock_account(session, system_account_id(session))
        _move_money(session, funding, account, initial_balance_paise)
    session.commit()  # the account and its funding transfer appear together, or not at all
    return account


def get_account(session: Session, account_id: int) -> Account | None:
    return session.get(Account, account_id)  # SELECT ... WHERE id = :id (None if no such row)


def transfer(session: Session, from_id: int, to_id: int, amount_paise: int) -> Transfer:
    """Move money between two customer accounts, or raise a LedgerError (nothing is written).
    Does NOT commit: the API commits once, together with the idempotency record (Phase 3)."""
    locked = _lock_accounts_in_order(session, from_id, to_id)
    src, dst = locked[from_id], locked[to_id]
    if src is None or dst is None:
        raise AccountNotFound()
    if src.is_system or dst.is_system:
        # Otherwise anyone could "transfer" from external_funding and create money from nothing.
        raise SystemAccountNotAllowed()
    if src.balance_paise < amount_paise:
        raise InsufficientFunds()
    result = _move_money(session, src, dst, amount_paise)
    session.flush()  # send the INSERTs/UPDATEs now so result.id is known (still uncommitted)
    return result


def get_transfer(session: Session, transfer_id: int) -> Transfer | None:
    return session.get(Transfer, transfer_id)


# A window function: SUM(...) OVER (ORDER BY e.id) adds up every entry up to and including the
# current row, which gives the balance right after each entry (a "running balance").
# The window is computed over ALL the account's entries first; ORDER BY ... DESC LIMIT then picks
# the newest ones, so balance_after is still correct for them.
STATEMENT_SQL = text("""
    SELECT e.id AS entry_id,
           e.transfer_id,
           e.direction,
           e.amount_paise,
           CASE WHEN e.direction = 'DEBIT' THEN t.to_account_id ELSE t.from_account_id END
               AS counterparty_account_id,
           SUM(CASE WHEN e.direction = 'CREDIT' THEN e.amount_paise ELSE -e.amount_paise END)
               OVER (ORDER BY e.id) AS balance_after_paise,
           e.created_at
    FROM ledger_entries e
    JOIN transfers t ON t.id = e.transfer_id
    WHERE e.account_id = :account_id
    ORDER BY e.id DESC
    LIMIT :limit
""")


def statement(session: Session, account_id: int, limit: int) -> list[dict]:
    rows = session.execute(STATEMENT_SQL, {"account_id": account_id, "limit": limit})
    return [dict(row._mapping) for row in rows]


INVARIANTS_SQL = text("""
    SELECT
      -- 1. Money is only moved, never created or destroyed: all balances sum to exactly 0
      --    (customer money is balanced by the system account's negative balance).
      (SELECT COALESCE(SUM(balance_paise), 0) FROM accounts) AS total_balance_paise,
      -- 2. Accounts whose cached balance differs from what their ledger entries add up to.
      (SELECT count(*) FROM accounts a
         LEFT JOIN (SELECT account_id,
                           SUM(CASE WHEN direction = 'CREDIT' THEN amount_paise
                                    ELSE -amount_paise END) AS total
                    FROM ledger_entries GROUP BY account_id) e ON e.account_id = a.id
       WHERE a.balance_paise <> COALESCE(e.total, 0)) AS accounts_mismatched,
      -- 3. Transfers that don't have exactly 2 entries: one DEBIT on the sender and one
      --    CREDIT on the receiver, both for the transfer's amount.
      (SELECT count(*) FROM transfers t
       WHERE (SELECT count(*) FROM ledger_entries e WHERE e.transfer_id = t.id) <> 2
          OR NOT EXISTS (SELECT 1 FROM ledger_entries e WHERE e.transfer_id = t.id
                         AND e.direction = 'DEBIT' AND e.account_id = t.from_account_id
                         AND e.amount_paise = t.amount_paise)
          OR NOT EXISTS (SELECT 1 FROM ledger_entries e WHERE e.transfer_id = t.id
                         AND e.direction = 'CREDIT' AND e.account_id = t.to_account_id
                         AND e.amount_paise = t.amount_paise)) AS transfers_unbalanced
""")


def check_invariants(session: Session) -> dict:
    """Audit the whole ledger. A healthy ledger returns all three numbers as 0."""
    return dict(session.execute(INVARIANTS_SQL).one()._mapping)
