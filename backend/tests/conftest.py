import os

# Set env vars BEFORE any app imports — database.py reads these at module load
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["GATEWAY_SECRET_TOKEN"] = "test-secret"
os.environ["MIN_CONFIDENCE"] = "0.80"
os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only1"

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app
from models import User
from auth import hash_password, require_login

# Pre-hash once at module load — avoids per-test bcrypt cost
_HASHED_TESTPASS = hash_password("testpass")

_test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_schema():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_tables():
    yield
    async with _test_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def client():
    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Seed a test user and log in so dashboard/archive routes pass auth
        async with _TestSession() as session:
            session.add(User(
                username="testadmin",
                hashed_password=_HASHED_TESTPASS,
                created_at=datetime.now(timezone.utc),
                created_by=None,
            ))
            await session.commit()
        await ac.post("/login", data={"username": "testadmin", "password": "testpass"})
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def authenticated_client():
    """Fast client with require_login bypassed via dependency override.

    Use this for any test that needs auth but isn't testing the login flow itself.
    """
    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    async def _override_require_login():
        return "testadmin"

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_login] = _override_require_login
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db_session():
    async with _TestSession() as session:
        yield session
