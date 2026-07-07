import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("GATEWAY_SECRET_TOKEN", "test-secret")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only1")

from unittest.mock import AsyncMock, patch
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from datetime import datetime, timezone

from database import Base, get_db
from main import app
from models import User
from auth import hash_password, require_login

_HASHED_AUDITPASS = hash_password("testpass")

_audit_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_AuditSession = async_sessionmaker(_audit_engine, expire_on_commit=False)

_INCIDENT_CLASS = {"issues": [{
    "category": "plumbing", "priority": "high", "confidence": 0.92,
    "message_snippet": "Pump leaking",
}]}
_ORIGINAL = {
    "event": "message.received",
    "data": {
        "id": "msg-audit1", "type": "chat", "isGroup": True,
        "chatId": "123@g.us", "chat": {"name": "Block A"},
        "author": "2541@c.us", "notifyName": "Alice",
        "body": "Pump leaking", "timestamp": 1782293340,
    },
}


@pytest_asyncio.fixture(scope="module", autouse=True)
async def audit_schema():
    async with _audit_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_audit_tables():
    yield
    async with _audit_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def audit_client():
    async def _override_get_db():
        async with _AuditSession() as session:
            yield session

    async def _override_require_login():
        return "auditadmin"

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_login] = _override_require_login
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        async with _AuditSession() as session:
            session.add(User(
                username="auditadmin",
                hashed_password=_HASHED_AUDITPASS,
                created_at=datetime.now(timezone.utc),
                role="admin",
            ))
            await session.commit()
        yield ac
    app.dependency_overrides.clear()


async def _make_incident(audit_client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await audit_client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    return (await audit_client.get("/incidents")).json()[0]["id"]


async def test_detail_endpoint_includes_audit_log_key(audit_client):
    incident_id = await _make_incident(audit_client)
    detail = (await audit_client.get(f"/incidents/{incident_id}")).json()
    assert "audit_log" in detail
    assert isinstance(detail["audit_log"], list)


async def test_status_history_includes_changed_by(audit_client):
    incident_id = await _make_incident(audit_client)
    await audit_client.patch(
        f"/incidents/{incident_id}/status",
        json={"status": "acknowledged"},
        headers={"X-API-Key": "test-secret"},
    )
    detail = (await audit_client.get(f"/incidents/{incident_id}")).json()
    # X-API-Key auth → actor is None → changed_by is None
    assert detail["status_history"][-1]["to_status"] == "acknowledged"
    assert detail["status_history"][-1]["changed_by"] is None


async def test_api_key_auth_does_not_create_audit_entry(audit_client):
    incident_id = await _make_incident(audit_client)
    await audit_client.patch(
        f"/incidents/{incident_id}/status",
        json={"status": "acknowledged"},
        headers={"X-API-Key": "test-secret"},
    )
    detail = (await audit_client.get(f"/incidents/{incident_id}")).json()
    # No audit log entry when using X-API-Key (actor=None)
    assert len(detail["audit_log"]) == 0


@pytest_asyncio.fixture
async def auth_audit_client():
    async def _override_get_db():
        async with _AuditSession() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Seed a user so login can succeed
        async with _AuditSession() as session:
            user = User(
                username="audituser",
                hashed_password=_HASHED_AUDITPASS,
                created_at=datetime.now(timezone.utc),
                role="admin",
            )
            session.add(user)
            await session.commit()

        # Log in to establish a session cookie
        await ac.post("/login", data={"username": "audituser", "password": "testpass"})

        yield ac

    app.dependency_overrides.clear()


async def test_session_auth_creates_audit_entry(auth_audit_client):
    # Create an incident via ingest (X-API-Key is fine for creation)
    incident_id = await _make_incident(auth_audit_client)

    # Update status using the session-authenticated client (no X-API-Key)
    await auth_audit_client.patch(
        f"/incidents/{incident_id}/status",
        json={"status": "acknowledged"},
    )

    detail = (await auth_audit_client.get(f"/incidents/{incident_id}")).json()

    assert len(detail["audit_log"]) == 1
    assert detail["audit_log"][0]["username"] == "audituser"
    assert detail["audit_log"][0]["action"] == "status_change"
    assert detail["status_history"][-1]["changed_by"] == "audituser"


async def test_reply_with_api_key_uses_dashboard_as_reporter(audit_client):
    incident_id = await _make_incident(audit_client)
    with patch("main.reply_to_message", new=AsyncMock(return_value="wa-1")):
        await audit_client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "We are investigating"},
            headers={"X-API-Key": "test-secret"},
        )
    detail = (await audit_client.get(f"/incidents/{incident_id}")).json()
    assert detail["updates"][-1]["reporter_name"] == "Dashboard"
    assert len(detail["audit_log"]) == 0
