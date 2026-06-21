from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlanPrice(Base):
    __tablename__ = "plan_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_type: Mapped[str] = mapped_column(String(10), nullable=False)  # "monthly" | "annual"
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(5), nullable=False, default="KES")
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    set_by: Mapped[str] = mapped_column(Text, nullable=False)

    def __init__(self, **kw):
        if "currency" not in kw:
            kw["currency"] = "KES"
        super().__init__(**kw)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    subdomain: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(10), nullable=False)  # "monthly" | "annual"
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="active")
    renewal_date: Mapped[date] = mapped_column(Date, nullable=False)
    grace_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    warning_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    whatsapp_group_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    openwa_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    openwa_session: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    openwa_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    docker_project: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __init__(self, **kw):
        if "status" not in kw:
            kw["status"] = "active"
        super().__init__(**kw)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    mpesa_transaction_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True, unique=True)
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="pending")
    # "pending" | "confirmed" | "failed"
    initiated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    def __init__(self, **kw):
        if "status" not in kw:
            kw["status"] = "pending"
        super().__init__(**kw)


class PaymentSession(Base):
    """Tracks a single in-progress /payment conversation in a WhatsApp group."""
    __tablename__ = "payment_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(25), nullable=False)
    # "awaiting_phone" | "awaiting_stk_confirm"
    phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    checkout_request_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payment_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("payments.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
