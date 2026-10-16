from datetime import date, datetime, time

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    # None until onboarding sets it
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Loan reminders and payday questions
    remind_time: Mapped[time] = mapped_column(Time, default=time(10, 0))
    # "Send me your balance" if none came today
    evening_time: Mapped[time] = mapped_column(Time, default=time(21, 0))
    # Shift pattern work/off days; None until set
    shift_work: Mapped[int | None] = mapped_column(Integer)
    shift_off: Mapped[int | None] = mapped_column(Integer)
    shift_anchor: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncomeRule(Base):
    """A regular payday: salary, advance."""

    __tablename__ = "income_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(50))
    day: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(BigInteger)


class Payday(Base):
    """What happened to one scheduled payday: asked about, received or delayed."""

    __tablename__ = "paydays"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("income_rules.id", ondelete="CASCADE"))
    scheduled: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10))  # expected / received / delayed
    delayed_until: Mapped[date | None] = mapped_column(Date)
    income_id: Mapped[int | None] = mapped_column(ForeignKey("incomes.id", ondelete="SET NULL"))

    rule: Mapped[IncomeRule] = relationship()

    __table_args__ = (UniqueConstraint("rule_id", "scheduled"),)


class Income(Base):
    __tablename__ = "incomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    on_date: Mapped[date] = mapped_column(Date)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(10))  # salary / other
    comment: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BalanceSnapshot(Base):
    """The balance of an account at the end of a day; a later entry the same day replaces it."""

    __tablename__ = "balances"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account: Mapped[str] = mapped_column(String(10))  # card / cash
    on_date: Mapped[date] = mapped_column(Date)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("user_id", "account", "on_date"),)


class ExpenseNote(Base):
    """Optional '500 еда' notes; they don't change any calculation."""

    __tablename__ = "expense_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    on_date: Mapped[date] = mapped_column(Date)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    category: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    lender: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(12))  # bank / mfo / card / installment
    status: Mapped[str] = mapped_column(String(10), default="active")  # active / closed
    principal_minor: Mapped[int] = mapped_column(BigInteger)
    # Interest accrued before interest_from and not paid yet
    unpaid_interest_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    interest_from: Mapped[date] = mapped_column(Date)
    # Decimal percent as text: SQLite has no exact decimal type
    rate: Mapped[str] = mapped_column(String(20), default="0")
    rate_unit: Mapped[str] = mapped_column(String(4), default="year")  # year / day
    schedule: Mapped[str] = mapped_column(String(8))  # monthly / once
    payment_minor: Mapped[int] = mapped_column(BigInteger)
    # Insurance and fees inside the payment; they never reduce the debt
    insurance_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    payment_day: Mapped[int | None] = mapped_column(Integer)
    # First due date to generate (monthly) or the single due date (once)
    first_due: Mapped[date] = mapped_column(Date)
    pay_method: Mapped[str] = mapped_column(String(6))  # auto / manual
    # Annual percent for late payments (пеня); None if unknown
    penalty_rate: Mapped[str | None] = mapped_column(String(20))
    credit_limit_minor: Mapped[int | None] = mapped_column(BigInteger)
    grace_end: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    dues: Mapped[list["LoanDue"]] = relationship(
        back_populates="loan", cascade="all, delete-orphan", order_by="LoanDue.due_date"
    )


class LoanDue(Base):
    """One scheduled payment of a loan."""

    __tablename__ = "loan_dues"

    id: Mapped[int] = mapped_column(primary_key=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id", ondelete="CASCADE"), index=True)
    due_date: Mapped[date] = mapped_column(Date)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    paid_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    # expected / paid / not_debited (auto-debit that the user said didn't happen)
    status: Mapped[str] = mapped_column(String(12), default="expected")

    loan: Mapped[Loan] = relationship(back_populates="dues")

    __table_args__ = (UniqueConstraint("loan_id", "due_date"),)


class LoanPayment(Base):
    """Money that actually went to a lender."""

    __tablename__ = "loan_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id", ondelete="CASCADE"), index=True)
    due_id: Mapped[int | None] = mapped_column(ForeignKey("loan_dues.id", ondelete="SET NULL"))
    paid_on: Mapped[date] = mapped_column(Date)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(10))  # regular / early
    # auto: assumed from an auto-debit date; manual: confirmed by the user
    source: Mapped[str] = mapped_column(String(6))
    interest_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    principal_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    insurance_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DebtHistory(Base):
    """Total debt per day, for progress and charts; can't be rebuilt later."""

    __tablename__ = "debt_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    on_date: Mapped[date] = mapped_column(Date)
    total_minor: Mapped[int] = mapped_column(BigInteger)

    __table_args__ = (UniqueConstraint("user_id", "on_date"),)


class Notification(Base):
    """Every bot-initiated message; the unique key makes delivery at-most-once."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # loan_3 / loan_1 (ref: due id), balance (ref 0), payday (ref: payday id)
    kind: Mapped[str] = mapped_column(String(12))
    ref_id: Mapped[int] = mapped_column(Integer)
    for_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending / sent / skipped
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("user_id", "kind", "ref_id", "for_date"),)


class FxRate(Base):
    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    rate_date: Mapped[date] = mapped_column(Date)
    # RUB per one unit, Decimal as text
    rate: Mapped[str] = mapped_column(String(40))

    __table_args__ = (UniqueConstraint("currency", "rate_date"),)


class ApiCache(Base):
    """Last successful responses of an external API, so the bot works while it is down."""

    __tablename__ = "api_cache"

    key: Mapped[str] = mapped_column(String(30), primary_key=True)
    payload: Mapped[object] = mapped_column(JSON)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Deployment(Base):
    """A bot version that started on the server (ok) or failed to start and was rolled back."""

    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(40))  # git sha
    title: Mapped[str | None] = mapped_column(String(200))  # commit subject
    status: Mapped[str] = mapped_column(String(10))  # ok / failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
