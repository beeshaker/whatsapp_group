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
