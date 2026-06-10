import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("GATEWAY_SECRET_TOKEN", "test-secret")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only1")

from datetime import datetime, timezone
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app, require_login
from models import User
from auth import hash_password

_users_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_UsersSession = async_sessionmaker(_users_engine, expire_on_commit=False)

_HASHED = hash_password("adminpass")


@pytest_asyncio.fixture(scope="module", autouse=True)
async def users_schema():
    async with _users_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_users_tables():
    yield
    async with _users_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def users_client():
    async def _override_get_db():
        async with _UsersSession() as session:
            yield session

    async def _override_require_login():
        return "testadmin"

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_login] = _override_require_login
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        async with _UsersSession() as session:
            session.add(User(
                username="testadmin",
                hashed_password=_HASHED,
                created_at=datetime.now(timezone.utc),
                created_by=None,
            ))
            await session.commit()
        yield ac
    app.dependency_overrides.clear()


async def test_list_users_returns_seeded_user(users_client):
    resp = await users_client.get("/users")
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) == 1
    assert users[0]["username"] == "testadmin"


async def test_create_user_returns_201(users_client):
    resp = await users_client.post("/users", json={"username": "newuser", "password": "securepass"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "newuser"
    assert data["created_by"] == "testadmin"


async def test_create_user_duplicate_returns_409(users_client):
    await users_client.post("/users", json={"username": "dup", "password": "password1"})
    resp = await users_client.post("/users", json={"username": "dup", "password": "password2"})
    assert resp.status_code == 409


async def test_create_user_short_password_returns_422(users_client):
    resp = await users_client.post("/users", json={"username": "u", "password": "short"})
    assert resp.status_code == 422


async def test_create_user_empty_username_returns_422(users_client):
    resp = await users_client.post("/users", json={"username": "  ", "password": "goodpassword"})
    assert resp.status_code == 422


async def test_create_user_username_too_long_returns_422(users_client):
    resp = await users_client.post("/users", json={"username": "a" * 65, "password": "goodpassword"})
    assert resp.status_code == 422


async def test_delete_user_returns_ok(users_client):
    create_resp = await users_client.post("/users", json={"username": "todelete", "password": "goodpassword"})
    user_id = create_resp.json()["id"]
    resp = await users_client.post(f"/users/{user_id}/delete")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == user_id


async def test_delete_own_account_returns_400(users_client):
    users = (await users_client.get("/users")).json()
    own_id = next(u["id"] for u in users if u["username"] == "testadmin")
    resp = await users_client.post(f"/users/{own_id}/delete")
    assert resp.status_code == 400


async def test_delete_nonexistent_user_returns_404(users_client):
    resp = await users_client.post("/users/99999/delete")
    assert resp.status_code == 404


async def test_list_users_requires_login():
    async def _override_get_db():
        async with _UsersSession() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    # No require_login override — this should trigger auth redirect
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/users", follow_redirects=False)
        assert resp.status_code == 302
    finally:
        app.dependency_overrides.clear()
