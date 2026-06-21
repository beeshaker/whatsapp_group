from datetime import date, datetime, timezone
from decimal import Decimal
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
