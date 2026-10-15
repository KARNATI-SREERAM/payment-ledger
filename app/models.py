# Database tables, declared as SQLAlchemy ORM classes. These classes ARE the schema:
# init_db() creates the tables from them, so there is no separate .sql file to fall out of sync.
# Money is stored as an integer number of paise (1 rupee = 100 paise), never as a float,
# because floats cannot represent most decimal fractions exactly (0.1 + 0.2 != 0.3).
#
# Double-entry bookkeeping: every transfer writes exactly two ledger entries, a DEBIT on the
# sender and a CREDIT on the receiver, for the same amount. `accounts.balance_paise` is a cached
# total that must always equal (credits - debits) for that account; ledger.check_invariants()
# verifies this.

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    false,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # The database itself refuses a negative balance, even if our Python code has a bug.
        # Only the system account may go negative (see is_system below).
        CheckConstraint("balance_paise >= 0 OR is_system", name="balance_not_negative"),
        # A "partial unique index": at most ONE row may have is_system = true.
        Index("one_system_account", "is_system", unique=True, postgresql_where="is_system"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_name: Mapped[str] = mapped_column(String(100))
    balance_paise: Mapped[int] = mapped_column(BigInteger)
    # The system account ("external_funding") stands for money entering from outside, e.g. a
    # deposit from a bank. Its balance is the negative of all money in customer accounts, so
    # the sum of ALL balances is always exactly 0.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # server_default: Postgres fills in the time, so all rows use the same (DB server) clock.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        CheckConstraint("from_account_id <> to_account_id", name="different_accounts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    from_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    to_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Not a column: lets Python code use transfer.entries (SQLAlchemy loads the matching rows).
    entries: Mapped[list["LedgerEntry"]] = relationship(order_by="LedgerEntry.id")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("direction IN ('DEBIT', 'CREDIT')", name="valid_direction"),
        CheckConstraint("amount_paise > 0", name="entry_amount_positive"),
        # Statements read "all entries of one account, in order": this index makes that fast.
        Index("entries_by_account", "account_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transfer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("transfers.id"), index=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"))
    direction: Mapped[str] = mapped_column(String(6))  # DEBIT = money out, CREDIT = money in
    amount_paise: Mapped[int] = mapped_column(BigInteger)  # always positive; direction gives sign
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
