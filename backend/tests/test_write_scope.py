import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("GATEWAY_SECRET_TOKEN", "test-secret")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only1")

import pytest_asyncio
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app
from models import User, Incident, UserGroup
from auth import hash_password

_ws_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_WSSession = async_sessionmaker(_ws_engine, expire_on_commit=False)
_HASHED = hash_password("pass1234")


@pytest_asyncio.fixture(scope="module", autouse=True)
async def ws_schema():
    async with _ws_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_ws_tables():
    yield
    async with _ws_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def ws_client():
    async def _override_get_db():
        async with _WSSession() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_incident(group_id: str) -> int:
    async with _WSSession() as session:
        inc = Incident(
            group_id=group_id, property_name="Test", reporter_name="R",
            message_body="msg", category="maintenance", severity="low",
            confidence=0.9, status="review", received_at=datetime.now(timezone.utc),
        )
        session.add(inc)
        await session.commit()
        await session.refresh(inc)
        return inc.id


async def _seed_user(username: str, role: str, group_id: str | None = None) -> None:
    async with _WSSession() as session:
        user = User(username=username, hashed_password=_HASHED, created_at=datetime.now(timezone.utc), role=role)
        session.add(user)
        await session.flush()
        if group_id:
            session.add(UserGroup(user_id=user.id, group_id=group_id))
        await session.commit()


async def test_status_change_blocked_for_out_of_scope_group(ws_client):
    inc_id = await _seed_incident("allowed@g.us")
    await _seed_user("limited", "user", "other@g.us")
    await ws_client.post("/login", data={"username": "limited", "password": "pass1234"})
    resp = await ws_client.patch(f"/incidents/{inc_id}/status", json={"status": "acknowledged"})
    assert resp.status_code == 403


async def test_status_change_allowed_for_in_scope_group(ws_client):
    inc_id = await _seed_incident("mygroup@g.us")
    await _seed_user("member", "user", "mygroup@g.us")
    await ws_client.post("/login", data={"username": "member", "password": "pass1234"})
    resp = await ws_client.patch(f"/incidents/{inc_id}/status", json={"status": "acknowledged"})
    assert resp.status_code == 200


async def test_status_change_allowed_for_admin(ws_client):
    inc_id = await _seed_incident("anygroup@g.us")
    await _seed_user("sysadmin", "admin")
    await ws_client.post("/login", data={"username": "sysadmin", "password": "pass1234"})
    resp = await ws_client.patch(f"/incidents/{inc_id}/status", json={"status": "acknowledged"})
    assert resp.status_code == 200


async def test_status_change_allowed_for_api_key(ws_client):
    inc_id = await _seed_incident("apigroup@g.us")
    resp = await ws_client.patch(
        f"/incidents/{inc_id}/status",
        json={"status": "acknowledged"},
        headers={"X-API-Key": "test-secret"},
    )
    assert resp.status_code == 200
