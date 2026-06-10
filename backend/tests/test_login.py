import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("GATEWAY_SECRET_TOKEN", "test-secret")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only1")

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app
from models import User
from auth import hash_password

_login_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_LoginSession = async_sessionmaker(_login_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def login_schema():
    async with _login_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_login_tables():
    yield
    async with _login_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def login_client():
    async def _override_get_db():
        async with _LoginSession() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_user(username="alice", password="secret"):
    async with _LoginSession() as session:
        session.add(User(
            username=username,
            hashed_password=hash_password(password),
            created_at=datetime.now(timezone.utc),
            created_by=None,
        ))
        await session.commit()


async def test_login_get_returns_200(login_client):
    r = await login_client.get("/login")
    assert r.status_code == 200
    assert b"form" in r.content.lower()


async def test_login_wrong_password_returns_401(login_client):
    await _seed_user()
    r = await login_client.post("/login", data={"username": "alice", "password": "wrong"})
    assert r.status_code == 401


async def test_login_correct_redirects_to_root(login_client):
    await _seed_user()
    r = await login_client.post(
        "/login",
        data={"username": "alice", "password": "secret"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"


async def test_logout_redirects_to_login(login_client):
    await _seed_user()
    await login_client.post("/login", data={"username": "alice", "password": "secret"})
    r = await login_client.post("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


async def test_dashboard_redirects_when_not_logged_in(login_client):
    r = await login_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


async def test_archive_redirects_when_not_logged_in(login_client):
    r = await login_client.get("/archive", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]
