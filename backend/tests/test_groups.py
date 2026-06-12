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

_groups_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_GroupsSession = async_sessionmaker(_groups_engine, expire_on_commit=False)
_HASHED = hash_password("pass1234")


@pytest_asyncio.fixture(scope="module", autouse=True)
async def groups_schema():
    async with _groups_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_groups_tables():
    yield
    async with _groups_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def groups_client():
    async def _override_get_db():
        async with _GroupsSession() as session:
            yield session

    async def _override_require_admin():
        return "testadmin"

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_admin] = _override_require_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        async with _GroupsSession() as session:
            session.add(User(
                username="testadmin",
                hashed_password=_HASHED,
                created_at=datetime.now(timezone.utc),
                role="admin",
            ))
            await session.commit()
        yield ac
    app.dependency_overrides.clear()


async def test_list_groups_empty(groups_client):
    resp = await groups_client.get("/api/groups")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_groups_returns_distinct_groups(groups_client):
    async with _GroupsSession() as session:
        for body in ["first", "second"]:
            session.add(Incident(
                group_id="aaa@g.us", property_name="Alpha", reporter_name="R",
                message_body=body, category="maintenance", severity="low",
                confidence=0.9, status="review", received_at=datetime.now(timezone.utc),
            ))
        session.add(Incident(
            group_id="bbb@g.us", property_name="Beta", reporter_name="R",
            message_body="third", category="maintenance", severity="low",
            confidence=0.9, status="review", received_at=datetime.now(timezone.utc),
        ))
        await session.commit()
    resp = await groups_client.get("/api/groups")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert {g["group_id"] for g in data} == {"aaa@g.us", "bbb@g.us"}


async def test_get_user_groups_empty(groups_client):
    async with _GroupsSession() as session:
        user = User(username="member", hashed_password=_HASHED, created_at=datetime.now(timezone.utc), role="user")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        uid = user.id
    resp = await groups_client.get(f"/users/{uid}/groups")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_set_and_get_user_groups(groups_client):
    async with _GroupsSession() as session:
        user = User(username="member2", hashed_password=_HASHED, created_at=datetime.now(timezone.utc), role="user")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        uid = user.id
    resp = await groups_client.post(f"/users/{uid}/groups", json={"group_ids": ["aaa@g.us", "bbb@g.us"]})
    assert resp.status_code == 200
    assert set(resp.json()["group_ids"]) == {"aaa@g.us", "bbb@g.us"}
    get_resp = await groups_client.get(f"/users/{uid}/groups")
    assert set(get_resp.json()) == {"aaa@g.us", "bbb@g.us"}


async def test_set_user_groups_replaces_existing(groups_client):
    async with _GroupsSession() as session:
        user = User(username="member3", hashed_password=_HASHED, created_at=datetime.now(timezone.utc), role="user")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        uid = user.id
    await groups_client.post(f"/users/{uid}/groups", json={"group_ids": ["aaa@g.us", "bbb@g.us"]})
    await groups_client.post(f"/users/{uid}/groups", json={"group_ids": ["ccc@g.us"]})
    get_resp = await groups_client.get(f"/users/{uid}/groups")
    assert get_resp.json() == ["ccc@g.us"]


async def test_get_user_groups_404(groups_client):
    resp = await groups_client.get("/users/99999/groups")
    assert resp.status_code == 404


async def test_set_user_groups_404(groups_client):
    resp = await groups_client.post("/users/99999/groups", json={"group_ids": []})
    assert resp.status_code == 404


async def test_set_user_groups_deduplicates_input(groups_client):
    async with _GroupsSession() as session:
        user = User(username="dedup", hashed_password=_HASHED, created_at=datetime.now(timezone.utc), role="user")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        uid = user.id
    resp = await groups_client.post(f"/users/{uid}/groups", json={"group_ids": ["aaa@g.us", "aaa@g.us"]})
    assert resp.status_code == 200
    assert resp.json()["group_ids"].count("aaa@g.us") == 1
