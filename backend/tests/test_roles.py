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
from auth import hash_password, require_admin

_roles_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_RolesSession = async_sessionmaker(_roles_engine, expire_on_commit=False)
_HASHED = hash_password("pass1234")


@pytest_asyncio.fixture(scope="module", autouse=True)
async def roles_schema():
    async with _roles_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_roles_tables():
    yield
    async with _roles_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def roles_client():
    async def _override_get_db():
        async with _RolesSession() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_require_admin_no_session_redirects(roles_client):
    resp = await roles_client.get("/users", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


async def test_require_admin_user_role_returns_403(roles_client):
    async with _RolesSession() as session:
        session.add(User(
            username="normaluser",
            hashed_password=_HASHED,
            created_at=datetime.now(timezone.utc),
            role="user",
        ))
        await session.commit()
    await roles_client.post("/login", data={"username": "normaluser", "password": "pass1234"})
    resp = await roles_client.get("/users", follow_redirects=False)
    assert resp.status_code == 403


async def test_require_admin_admin_role_passes(roles_client):
    async with _RolesSession() as session:
        session.add(User(
            username="adminuser",
            hashed_password=_HASHED,
            created_at=datetime.now(timezone.utc),
            role="admin",
        ))
        await session.commit()
    await roles_client.post("/login", data={"username": "adminuser", "password": "pass1234"})
    resp = await roles_client.get("/users", follow_redirects=False)
    assert resp.status_code == 200
