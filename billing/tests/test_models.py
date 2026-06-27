from datetime import date, datetime, timezone
from decimal import Decimal
import pytest
from models import AdminUser, Client, Payment, PlanPrice, PaymentSession


def test_admin_user_fields():
    u = AdminUser(username="admin", hashed_password="hashed", created_at=datetime.now(timezone.utc))
    assert u.username == "admin"


def test_client_default_status():
    c = Client(
        name="Acme", subdomain="acme", plan="monthly",
        renewal_date=date.today(), created_at=datetime.now(timezone.utc),
    )
    assert c.status == "active"


def test_payment_status_values():
    valid = {"pending", "confirmed", "failed"}
    assert "pending" in valid and "confirmed" in valid and "failed" in valid


def test_payment_session_states():
    valid = {"awaiting_phone", "awaiting_stk_confirm"}
    assert "awaiting_phone" in valid and "awaiting_stk_confirm" in valid


@pytest.mark.asyncio
async def test_client_has_new_billing_columns(db_session):
    now = datetime.now(timezone.utc)
    c = Client(
        name="Test", subdomain="test", plan="monthly",
        status="active", renewal_date=date.today(),
        created_at=now,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    assert c.billing_only_started_at is None
    assert c.last_warning_sent_at is None
    assert c.data_retention_days == 90
    assert c.pre_expiry_14_warned is False
    assert c.pre_expiry_2_warned is False
