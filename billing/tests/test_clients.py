import pytest
import pytest_asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
import database, main


@pytest_asyncio.fixture
async def auth_http(db_session, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    from models import AdminUser
    from auth import hash_password
    db_session.add(AdminUser(
        username="admin", hashed_password=hash_password("pw"),
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "admin", "password": "pw"})
        yield c


@pytest.mark.asyncio
async def test_create_client(auth_http):
    r = await auth_http.post("/clients", data={
        "name": "Acme Corp", "subdomain": "acme", "plan": "monthly",
    })
    assert r.status_code in (200, 303)


@pytest.mark.asyncio
async def test_client_list_shows_name(auth_http):
    await auth_http.post("/clients", data={"name": "Riverside", "subdomain": "riverside", "plan": "annual"})
    r = await auth_http.get("/")
    assert b"Riverside" in r.content


@pytest.mark.asyncio
async def test_duplicate_subdomain_rejected(auth_http):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "dup", "plan": "monthly"})
    r = await auth_http.post("/clients", data={"name": "Other", "subdomain": "dup", "plan": "monthly"})
    assert r.status_code == 200
    assert b"already exists" in r.content


@pytest.mark.asyncio
async def test_update_client_openwa_config(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme2", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme2"))
    r = await auth_http.post(f"/clients/{client.id}", data={
        "openwa_url": "http://localhost:2001",
        "openwa_session": "acme2",
        "openwa_api_key": "key-123",
        "whatsapp_group_id": "group@g.us",
        "docker_project": "acme2",
    })
    assert r.status_code in (200, 303)
    await db_session.refresh(client)
    assert client.openwa_url == "http://localhost:2001"


@pytest.mark.asyncio
async def test_set_and_read_prices(auth_http):
    r = await auth_http.post("/prices", data={"monthly_amount": "1500.00", "annual_amount": "15000.00"})
    assert r.status_code in (200, 303)
    r2 = await auth_http.get("/prices")
    assert b"1500" in r2.content
    assert b"15000" in r2.content


@pytest.mark.asyncio
async def test_status_endpoint_returns_active(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acmestatus", "plan": "monthly"})
    r = await auth_http.get("/api/clients/acmestatus/status")
    assert r.status_code == 200
    assert r.json()["status"] == "active"


@pytest.mark.asyncio
async def test_status_endpoint_returns_billing_only(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Billing", "subdomain": "billingonly", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "billingonly"))
    client.status = "billing_only"
    await db_session.commit()
    r = await auth_http.get("/api/clients/billingonly/status")
    assert r.status_code == 200
    assert r.json()["status"] == "billing_only"


@pytest.mark.asyncio
async def test_close_endpoint_sets_status_closed(auth_http, db_session):
    from unittest.mock import patch, AsyncMock
    await auth_http.post("/clients", data={"name": "Close Me", "subdomain": "closeme", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "closeme"))
    with patch("main.stop_client", new=AsyncMock()):
        with patch("main.send_to_group", new=AsyncMock()):
            r = await auth_http.post(f"/clients/{client.id}/close")
    assert r.status_code in (200, 303)
    await db_session.refresh(client)
    assert client.status == "closed"


@pytest.mark.asyncio
async def test_auth_check_blocks_billing_only(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "BlockMe", "subdomain": "blockme", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "blockme"))
    client.status = "billing_only"
    await db_session.commit()
    r = await auth_http.get("/internal/auth-check", headers={"X-Client-Subdomain": "blockme"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_auth_check_blocks_closed(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Closed", "subdomain": "closedclient", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "closedclient"))
    client.status = "closed"
    await db_session.commit()
    r = await auth_http.get("/internal/auth-check", headers={"X-Client-Subdomain": "closedclient"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_auth_check_blocks_grace(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Grace", "subdomain": "graceclient", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "graceclient"))
    client.status = "grace"
    await db_session.commit()
    r = await auth_http.get("/internal/auth-check", headers={"X-Client-Subdomain": "graceclient"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_prices_page_shows_group_tiers(auth_http):
    r = await auth_http.get("/prices")
    assert r.status_code == 200
    assert b"1" in r.content and b"5" in r.content  # tier boundaries rendered


@pytest.mark.asyncio
async def test_set_group_tier_prices(auth_http, db_session):
    r = await auth_http.post("/prices/group-tiers", data={
        "tier1_amount": "500.00", "tier2_amount": "1200.00", "tier3_amount": "2500.00",
    })
    assert r.status_code in (200, 303)
    from models import GroupTierPrice
    from sqlalchemy import select
    tiers = (await db_session.execute(
        select(GroupTierPrice).order_by(GroupTierPrice.min_groups)
    )).scalars().all()
    assert [str(t.amount) for t in tiers] == ["500.00", "1200.00", "2500.00"]
    assert all(t.set_by == "admin" for t in tiers)


@pytest.mark.asyncio
async def test_admin_add_ticket_group_opts_in_and_sets_base_tier(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-groups", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-groups"))
    assert client.allowed_ticket_groups is None

    r = await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "120363111@g.us"})
    assert r.status_code in (200, 303)
    await db_session.refresh(client)
    import json
    assert json.loads(client.allowed_ticket_groups) == ["120363111@g.us"]
    assert client.ticket_group_tier_id is not None
    from models import GroupTierPrice
    tier = await db_session.get(GroupTierPrice, client.ticket_group_tier_id)
    assert tier.min_groups == 1


@pytest.mark.asyncio
async def test_admin_add_duplicate_group_is_noop(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-dup", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-dup"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})
    await db_session.refresh(client)
    import json
    assert json.loads(client.allowed_ticket_groups) == ["g1@g.us"]


@pytest.mark.asyncio
async def test_admin_add_beyond_tier_limit_is_unrestricted(auth_http, db_session):
    """Billing admin bypasses the tier limit entirely — 6 groups on a 1-5 tier is allowed."""
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-many", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-many"))
    for i in range(6):
        r = await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": f"g{i}@g.us"})
        assert r.status_code in (200, 303)
    await db_session.refresh(client)
    import json
    assert len(json.loads(client.allowed_ticket_groups)) == 6


@pytest.mark.asyncio
async def test_admin_remove_ticket_group(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-rm", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-rm"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g2@g.us"})
    r = await auth_http.post(f"/clients/{client.id}/ticket-groups/remove", data={"group_id": "g1@g.us"})
    assert r.status_code in (200, 303)
    await db_session.refresh(client)
    import json
    assert json.loads(client.allowed_ticket_groups) == ["g2@g.us"]


@pytest.mark.asyncio
async def test_admin_remove_ticket_group_never_opted_in_is_noop(auth_http, db_session):
    """Removing a group from a client that never opted in must not opt them in.

    Regression test: allowed_ticket_groups must stay None (not flip to "[]"),
    and ticket_group_tier_id must stay None, preserving the invariant that
    once allowed_ticket_groups is non-None the client has a tier assigned.
    """
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-rm-noop", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-rm-noop"))
    assert client.allowed_ticket_groups is None
    assert client.ticket_group_tier_id is None

    r = await auth_http.post(f"/clients/{client.id}/ticket-groups/remove", data={"group_id": "g1@g.us"})
    assert r.status_code in (200, 303)
    await db_session.refresh(client)
    assert client.allowed_ticket_groups is None
    assert client.ticket_group_tier_id is None


@pytest.mark.asyncio
async def test_ticket_groups_endpoint_unrestricted_client(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-unrestricted", "plan": "monthly"})
    r = await auth_http.get("/api/clients/acme-unrestricted/ticket-groups")
    assert r.status_code == 200
    assert r.json() == {"allowed_groups": None, "tier_limit": None}


@pytest.mark.asyncio
async def test_ticket_groups_endpoint_restricted_client(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-restricted", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-restricted"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})
    r = await auth_http.get("/api/clients/acme-restricted/ticket-groups")
    assert r.status_code == 200
    body = r.json()
    assert body["allowed_groups"] == ["g1@g.us"]
    assert body["tier_limit"] == 5


@pytest.mark.asyncio
async def test_self_service_add_under_limit_succeeds(auth_http, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-self1", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-self1"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})

    r = await auth_http.post(
        "/api/clients/acme-self1/ticket-groups/add",
        json={"group_id": "g2@g.us"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["added"] is True
    await db_session.refresh(client)
    import json as _json
    assert "g2@g.us" in _json.loads(client.allowed_ticket_groups)


@pytest.mark.asyncio
async def test_self_service_add_duplicate_is_noop(auth_http, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-self2", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-self2"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})

    r = await auth_http.post(
        "/api/clients/acme-self2/ticket-groups/add",
        json={"group_id": "g1@g.us"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "added": False}


@pytest.mark.asyncio
async def test_self_service_add_beyond_limit_returns_limit_reached(auth_http, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-self3", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-self3"))
    for i in range(5):
        await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": f"g{i}@g.us"})
    await auth_http.post("/prices/group-tiers", data={
        "tier1_amount": "500.00", "tier2_amount": "1200.00", "tier3_amount": "2500.00",
    })

    r = await auth_http.post(
        "/api/clients/acme-self3/ticket-groups/add",
        json={"group_id": "g-extra@g.us"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "limit_reached"
    assert body["next_tier_amount"] == "1200.00"
    assert body["next_tier_max"] == 10
    await db_session.refresh(client)
    import json as _json
    assert len(_json.loads(client.allowed_ticket_groups)) == 5  # not added


@pytest.mark.asyncio
async def test_self_service_only_bootstraps_tier_id(auth_http, db_session, monkeypatch):
    """A client already opted in (allowed_ticket_groups non-None) but somehow
    missing a tier assignment (e.g. legacy data) still gets one bootstrapped
    on self-service add."""
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-self4", "plan": "monthly"})
    from models import Client, GroupTierPrice
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-self4"))
    client.allowed_ticket_groups = "[]"
    client.ticket_group_tier_id = None
    await db_session.commit()

    r = await auth_http.post(
        "/api/clients/acme-self4/ticket-groups/add",
        json={"group_id": "g1@g.us"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["added"] is True

    await db_session.refresh(client)
    assert client.ticket_group_tier_id is not None
    tier = await db_session.get(GroupTierPrice, client.ticket_group_tier_id)
    assert tier.min_groups == 1


@pytest.mark.asyncio
async def test_self_service_add_rejected_for_unrestricted_client(auth_http, db_session, monkeypatch):
    """Regression (Finding 1): self-service add must not silently opt in an
    unrestricted client. allowed_ticket_groups is None means the client has no
    cap and no billing tie-in — self-service add is only for clients a billing
    admin has already opted in via admin_add_ticket_group."""
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-self5", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-self5"))
    assert client.allowed_ticket_groups is None

    r = await auth_http.post(
        "/api/clients/acme-self5/ticket-groups/add",
        json={"group_id": "g1@g.us"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 403
    await db_session.refresh(client)
    assert client.allowed_ticket_groups is None
    assert client.ticket_group_tier_id is None


@pytest.mark.asyncio
async def test_admin_reset_ticket_groups_unrestricted(auth_http, db_session):
    """Regression (Finding 2): a billing admin can reset an opted-in client
    back to fully unrestricted (allowed_ticket_groups and ticket_group_tier_id
    both None), which is not achievable by removing every group (that leaves
    an empty-list "restricted to zero groups" state instead)."""
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-reset", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-reset"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})
    await db_session.refresh(client)
    assert client.allowed_ticket_groups is not None
    assert client.ticket_group_tier_id is not None

    r = await auth_http.post(f"/clients/{client.id}/ticket-groups/reset-unrestricted")
    assert r.status_code in (200, 303)
    await db_session.refresh(client)
    assert client.allowed_ticket_groups is None
    assert client.ticket_group_tier_id is None

    r2 = await auth_http.get("/api/clients/acme-reset/ticket-groups")
    assert r2.status_code == 200
    assert r2.json() == {"allowed_groups": None, "tier_limit": None}
