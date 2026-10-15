# Database tables, declared as SQLAlchemy ORM classes. These classes ARE the schema:
# init_db() creates the tables from them, so there is no separate .sql file to fall out of sync.
# Money is stored as an integer number of paise (1 rupee = 100 paise), never as a float,
# because floats cannot represent most decimal fractions exactly (0.1 + 0.2 != 0.3).

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    # The database itself refuses a negative balance, even if our Python code has a bug.
    __table_args__ = (CheckConstraint("balance_paise >= 0", name="balance_not_negative"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_name: Mapped[str] = mapped_column(String(100))
    balance_paise: Mapped[int] = mapped_column(BigInteger)
    # server_default: Postgres fills in the time, so all rows use the same (DB server) clock.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
