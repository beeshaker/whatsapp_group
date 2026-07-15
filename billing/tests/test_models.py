from datetime import date, datetime, timezone
from decimal import Decimal
import pytest
from models import AdminUser, Client, Payment, PaymentSession


def test_admin_user_fields():
    u = AdminUser(username="admin", hashed_password="hashed", created_at=datetime.now(timezone.utc))
    assert u.username == "admin"


def test_client_default_status():
    c = Client(
        name="Acme", subdomain="acme",
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
        name="Test", subdomain="test",
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


@pytest.mark.asyncio
async def test_client_ticket_group_columns_default_none(db_session):
    from models import Client
    from datetime import date, datetime, timezone
    c = Client(
        name="Acme", subdomain="acme-tg",
        renewal_date=date.today(), created_at=datetime.now(timezone.utc),
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    assert c.allowed_ticket_groups is None
    assert c.ticket_group_tier_id is None


@pytest.mark.asyncio
async def test_group_upgrade_request_defaults_to_pending(db_session):
    from models import Client, GroupTierPrice, GroupUpgradeRequest
    from datetime import date, datetime, timezone
    from decimal import Decimal
    c = Client(
        name="Acme", subdomain="acme-tg2",
        renewal_date=date.today(), created_at=datetime.now(timezone.utc),
    )
    db_session.add(c)
    await db_session.flush()
    tier = GroupTierPrice(
        name="Tier 2", min_groups=6, max_groups=10, amount=Decimal("500"),
        set_at=datetime.now(timezone.utc), set_by="admin",
    )
    db_session.add(tier)
    await db_session.flush()
    req = GroupUpgradeRequest(
        client_id=c.id, group_id="120363XXXXXXXXXX@g.us",
        target_tier_id=tier.id, phone="254712345678", amount=Decimal("500"),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    await db_session.commit()
    await db_session.refresh(req)
    assert req.status == "pending"


@pytest.mark.asyncio
async def test_seed_group_tier_prices_creates_three_non_overlapping_tiers(db_session, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import select
    from models import GroupTierPrice
    import main
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(main, "AsyncSessionLocal", factory)

    await main._seed_group_tier_prices()

    tiers = (await db_session.execute(
        select(GroupTierPrice).order_by(GroupTierPrice.min_groups)
    )).scalars().all()
    assert len(tiers) == 3
    assert (tiers[0].min_groups, tiers[0].max_groups) == (1, 5)
    assert (tiers[1].min_groups, tiers[1].max_groups) == (6, 10)
    assert (tiers[2].min_groups, tiers[2].max_groups) == (11, None)


@pytest.mark.asyncio
async def test_seed_group_tier_prices_is_idempotent(db_session, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import select
    from models import GroupTierPrice
    import main
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(main, "AsyncSessionLocal", factory)

    await main._seed_group_tier_prices()
    await main._seed_group_tier_prices()

    tiers = (await db_session.execute(select(GroupTierPrice))).scalars().all()
    assert len(tiers) == 3
