import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import main as backend_main
from database import get_db

GATEWAY_TOKEN = "fleet-dashboard-gateway-secret"


@pytest.fixture(autouse=True)
def _restore_main_module_state():
    yield
    importlib.reload(backend_main)


@pytest_asyncio.fixture
async def fleet_client(monkeypatch):
    monkeypatch.setenv("FLEET_PLATE_MODE", "true")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    from tests.conftest import _TestSession
    from auth import require_login, require_admin, hash_password
    from models import User
    importlib.reload(backend_main)

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    async def _override_require_login():
        return "fleetadmin"

    async def _override_require_admin():
        return "fleetadmin"

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    backend_main.app.dependency_overrides[require_login] = _override_require_login
    backend_main.app.dependency_overrides[require_admin] = _override_require_admin
    async with _TestSession() as session:
        session.add(User(
            username="fleetadmin",
            hashed_password=hash_password("irrelevant"),
            created_at=datetime.now(timezone.utc),
            role="admin",
        ))
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        yield c
    backend_main.app.dependency_overrides.clear()


async def test_dashboard_shows_plate_badge_when_fleet_mode_on(fleet_client):
    classification = {"issues": [{
        "category": "brakes", "priority": "high", "confidence": 0.9,
        "message_snippet": "brakes grinding on KMGQ 947Z",
    }]}
    payload = {
        "event": "message.received",
        "data": {
            "type": "chat", "isGroup": True,
            "chatId": "riders@g.us", "chat": {"name": "Pixiilive Riders"},
            "author": "254700000000@c.us",
            "body": "brakes grinding on KMGQ 947Z", "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await fleet_client.post(
                "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": GATEWAY_TOKEN}
            )
    response = await fleet_client.get("/")
    assert response.status_code == 200
    assert b"KMGQ947Z" in response.content


async def test_dashboard_shows_unassigned_when_no_plate_found(fleet_client):
    classification = {"issues": [{
        "category": "brakes", "priority": "high", "confidence": 0.9,
        "message_snippet": "bike needs service",
    }]}
    payload = {
        "event": "message.received",
        "data": {
            "type": "chat", "isGroup": True,
            "chatId": "riders2@g.us", "chat": {"name": "Pixiilive Riders"},
            "author": "254700000001@c.us",
            "body": "bike needs service", "timestamp": 1782293341,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await fleet_client.post(
                "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": GATEWAY_TOKEN}
            )
    response = await fleet_client.get("/")
    assert response.status_code == 200
    assert b"Unassigned" in response.content
