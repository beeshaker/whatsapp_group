"""Focused regression tests for Task 5's template fixes.

These verify the 4 templates updated for group-tier-only billing
(client_detail.html, prices.html, client_form.html, dashboard.html)
render without raising Jinja UndefinedError against the new route data
shapes (current_tier / group_tiers / tiers_by_id / unified_payment_history
dicts), for both the "client has no tier assigned" and "client has a real
tier" cases.

test_clients.py's existing fixtures are broken (they still post the
deleted `plan` field) and are being rewritten in a separate task, so this
file duplicates the same auth_http fixture pattern rather than reusing
that module.
"""
import pytest
import pytest_asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from httpx import AsyncClient, ASGITransport

import database, main
from models import AdminUser, Client, GroupTierPrice, Payment, GroupUpgradeRequest
from auth import hash_password


@pytest_asyncio.fixture
async def auth_http(db_session, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    db_session.add(AdminUser(
        username="admin", hashed_password=hash_password("pw"),
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "admin", "password": "pw"})
        yield c


async def _seed_tiers(db_session):
    now = datetime.now(timezone.utc)
    tiers = [
        GroupTierPrice(name="Starter", min_groups=1, max_groups=5, amount=Decimal("500"), set_at=now, set_by="admin"),
        GroupTierPrice(name="Growth", min_groups=6, max_groups=10, amount=Decimal("1000"), set_at=now, set_by="admin"),
        GroupTierPrice(name="Scale", min_groups=11, max_groups=None, amount=Decimal("2000"), set_at=now, set_by="admin"),
    ]
    db_session.add_all(tiers)
    await db_session.commit()
    for t in tiers:
        await db_session.refresh(t)
    return tiers


async def _make_client(db_session, *, subdomain, tier_id):
    client = Client(
        name=f"Client {subdomain}", subdomain=subdomain, status="active",
        renewal_date=date.today(), created_at=datetime.now(timezone.utc),
        ticket_group_tier_id=tier_id,
    )
    db_session.add(client)
    await db_session.commit()
    await db_session.refresh(client)
    return client


# ---------------------------------------------------------------------------
# client_detail.html
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_detail_renders_with_no_tier_assigned(auth_http, db_session):
    """Legacy-client scenario: ticket_group_tier_id is None. Must render the
    visible warning, not crash with UndefinedError on client.plan."""
    await _seed_tiers(db_session)
    client = await _make_client(db_session, subdomain="notier", tier_id=None)
    r = await auth_http.get(f"/clients/{client.id}")
    assert r.status_code == 200
    assert b"No billing tier assigned" in r.content
    assert b"No payments yet" in r.content


@pytest.mark.asyncio
async def test_client_detail_renders_with_tier_and_mixed_payment_kinds(auth_http, db_session):
    """A tiered client with both a renewal Payment and a tier_upgrade
    GroupUpgradeRequest in its history — exercises unified_payment_history()'s
    exact dict shape (date already formatted, receipt not mpesa_transaction_id)."""
    tiers = await _seed_tiers(db_session)
    client = await _make_client(db_session, subdomain="tiered", tier_id=tiers[0].id)

    db_session.add(Payment(
        client_id=client.id, phone="254712345678", amount=Decimal("500"),
        status="confirmed", initiated_at=datetime.now(timezone.utc),
        mpesa_transaction_id="ABC123",
        period_start=date.today(), period_end=date.today(),
    ))
    db_session.add(GroupUpgradeRequest(
        client_id=client.id, group_id="120363XXX@g.us", target_tier_id=tiers[1].id,
        phone="254712345678", amount=Decimal("1000"), status="confirmed",
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    r = await auth_http.get(f"/clients/{client.id}")
    assert r.status_code == 200
    assert b"Starter (KES 500" in r.content or b"Starter" in r.content
    assert b"Renewal" in r.content
    assert b"Tier Upgrade" in r.content
    assert b"ABC123" in r.content


@pytest.mark.asyncio
async def test_client_detail_tier_select_populated(auth_http, db_session):
    tiers = await _seed_tiers(db_session)
    client = await _make_client(db_session, subdomain="selecttest", tier_id=tiers[1].id)
    r = await auth_http.get(f"/clients/{client.id}")
    assert r.status_code == 200
    assert b'name="ticket_group_tier_id"' in r.content
    assert b"Growth" in r.content and b"Scale" in r.content


# ---------------------------------------------------------------------------
# prices.html
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prices_page_renders_without_prices_key(auth_http, db_session):
    """prices_page() no longer passes a `prices` variable at all — the old
    monthly/annual form must be gone, not silently rendering Undefined."""
    r = await auth_http.get("/prices")
    assert r.status_code == 200
    assert b"prices['monthly']" not in r.content
    assert b'name="monthly_amount"' not in r.content
    assert b'name="annual_amount"' not in r.content
    assert b'name="tier1_name"' in r.content
    assert b'name="tier1_amount"' in r.content
    assert b"Group Tier Prices" in r.content


@pytest.mark.asyncio
async def test_set_group_tier_prices_round_trip(auth_http, db_session):
    await _seed_tiers(db_session)
    r = await auth_http.post("/prices/group-tiers", data={
        "tier1_name": "Basic", "tier1_amount": "600",
        "tier2_name": "Pro", "tier2_amount": "1200",
        "tier3_name": "Enterprise", "tier3_amount": "2500",
    })
    assert r.status_code in (200, 303)
    r2 = await auth_http.get("/prices")
    assert r2.status_code == 200
    assert b"Basic" in r2.content


@pytest.mark.asyncio
async def test_set_group_tier_prices_duplicate_name_shows_error(auth_http, db_session):
    await _seed_tiers(db_session)
    r = await auth_http.post("/prices/group-tiers", data={
        "tier1_name": "Same", "tier1_amount": "600",
        "tier2_name": "Same", "tier2_amount": "1200",
        "tier3_name": "Enterprise", "tier3_amount": "2500",
    })
    assert r.status_code == 200
    assert b"must be unique" in r.content


# ---------------------------------------------------------------------------
# client_form.html
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_form_renders_with_group_tiers(auth_http, db_session):
    await _seed_tiers(db_session)
    r = await auth_http.get("/clients/new")
    assert r.status_code == 200
    assert b'name="ticket_group_tier_id"' in r.content
    assert b"Starting Tier" in r.content
    assert b"Starter" in r.content


@pytest.mark.asyncio
async def test_create_client_with_tier_id(auth_http, db_session):
    tiers = await _seed_tiers(db_session)
    r = await auth_http.post("/clients", data={
        "name": "Acme Corp", "subdomain": "acme-tier-test",
        "ticket_group_tier_id": str(tiers[1].id),
    })
    assert r.status_code in (200, 303)


# ---------------------------------------------------------------------------
# dashboard.html
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_renders_client_with_no_tier(auth_http, db_session):
    await _seed_tiers(db_session)
    await _make_client(db_session, subdomain="dashnotier", tier_id=None)
    r = await auth_http.get("/")
    assert r.status_code == 200
    assert b"No tier" in r.content


@pytest.mark.asyncio
async def test_dashboard_renders_client_with_tier(auth_http, db_session):
    tiers = await _seed_tiers(db_session)
    await _make_client(db_session, subdomain="dashtiered", tier_id=tiers[2].id)
    r = await auth_http.get("/")
    assert r.status_code == 200
    assert b"Scale" in r.content
