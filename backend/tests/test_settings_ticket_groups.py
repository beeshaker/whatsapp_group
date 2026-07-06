import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

os.environ.setdefault("SECRET_KEY", "test-key-for-settings-ticket-groups")
os.environ.setdefault("TESTING", "1")

GATEWAY_TOKEN = "ops-gateway-secret-2026"


@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("BILLING_SERVICE_URL", "http://billing:9000")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("CLIENT_SUBDOMAIN", "acme")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    import importlib, main as backend_main
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    from database import get_db
    from models import User
    from datetime import datetime, timezone
    importlib.reload(backend_main)
    backend_main._billing_status_cache = None
    backend_main._ticket_groups_cache = None

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    return backend_main


@pytest.mark.asyncio
async def test_settings_ticket_groups_get_proxies_billing(admin_client, monkeypatch):
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    from models import User
    from datetime import datetime, timezone

    async with _TestSession() as session:
        session.add(User(
            username="settingsadmin", hashed_password=_HASHED_TESTPASS,
            created_at=datetime.now(timezone.utc), created_by=None, role="admin",
        ))
        await session.commit()

    mock_billing_client = AsyncMock()
    mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
    mock_billing_client.__aexit__ = AsyncMock(return_value=None)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={"allowed_groups": ["g1@g.us"], "tier_limit": 5})
    mock_billing_client.get = AsyncMock(return_value=mock_resp)

    async with AsyncClient(transport=ASGITransport(app=admin_client.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "settingsadmin", "password": "testpass"})
        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.get("/api/settings/ticket-groups")
    admin_client.app.dependency_overrides.clear()
    assert r.status_code == 200
    assert r.json() == {"allowed_groups": ["g1@g.us"], "tier_limit": 5}


@pytest.mark.asyncio
async def test_settings_ticket_groups_add_rejects_malformed_id(admin_client):
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    from models import User
    from datetime import datetime, timezone

    async with _TestSession() as session:
        session.add(User(
            username="settingsadmin2", hashed_password=_HASHED_TESTPASS,
            created_at=datetime.now(timezone.utc), created_by=None, role="admin",
        ))
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=admin_client.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "settingsadmin2", "password": "testpass"})
        r = await c.post("/api/settings/ticket-groups/add", json={"group_id": "not-a-jid"})
    admin_client.app.dependency_overrides.clear()
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_settings_ticket_groups_add_proxies_to_billing(admin_client):
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    from models import User
    from datetime import datetime, timezone

    async with _TestSession() as session:
        session.add(User(
            username="settingsadmin3", hashed_password=_HASHED_TESTPASS,
            created_at=datetime.now(timezone.utc), created_by=None, role="admin",
        ))
        await session.commit()

    mock_billing_client = AsyncMock()
    mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
    mock_billing_client.__aexit__ = AsyncMock(return_value=None)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={"status": "ok", "added": True})
    mock_billing_client.post = AsyncMock(return_value=mock_resp)

    async with AsyncClient(transport=ASGITransport(app=admin_client.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "settingsadmin3", "password": "testpass"})
        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.post("/api/settings/ticket-groups/add", json={"group_id": "120363111@g.us"})
    admin_client.app.dependency_overrides.clear()
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "added": True}
    mock_billing_client.post.assert_called_once()
    call = mock_billing_client.post.call_args
    assert "ticket-groups/add" in call.args[0]
    assert call.kwargs["json"] == {"group_id": "120363111@g.us"}


@pytest.mark.asyncio
async def test_settings_ticket_groups_add_returns_502_when_billing_unreachable(admin_client):
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    from models import User
    from datetime import datetime, timezone

    async with _TestSession() as session:
        session.add(User(
            username="settingsadmin4", hashed_password=_HASHED_TESTPASS,
            created_at=datetime.now(timezone.utc), created_by=None, role="admin",
        ))
        await session.commit()

    mock_billing_client = AsyncMock()
    mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
    mock_billing_client.__aexit__ = AsyncMock(return_value=None)
    mock_billing_client.post = AsyncMock(side_effect=Exception("connection refused"))

    async with AsyncClient(transport=ASGITransport(app=admin_client.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "settingsadmin4", "password": "testpass"})
        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.post("/api/settings/ticket-groups/add", json={"group_id": "120363111@g.us"})
    admin_client.app.dependency_overrides.clear()
    assert r.status_code == 502
    assert "billing" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_settings_ticket_groups_upgrade_proxies_to_billing(admin_client):
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    from models import User
    from datetime import datetime, timezone

    async with _TestSession() as session:
        session.add(User(
            username="settingsadmin4", hashed_password=_HASHED_TESTPASS,
            created_at=datetime.now(timezone.utc), created_by=None, role="admin",
        ))
        await session.commit()

    mock_billing_client = AsyncMock()
    mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
    mock_billing_client.__aexit__ = AsyncMock(return_value=None)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={"status": "stk_sent"})
    mock_billing_client.post = AsyncMock(return_value=mock_resp)

    async with AsyncClient(transport=ASGITransport(app=admin_client.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "settingsadmin4", "password": "testpass"})
        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.post("/api/settings/ticket-groups/upgrade", json={
                "group_id": "120363111@g.us", "phone": "0712345678",
            })
    admin_client.app.dependency_overrides.clear()
    assert r.status_code == 200
    assert r.json() == {"status": "stk_sent"}
    call = mock_billing_client.post.call_args
    assert "ticket-groups/upgrade" in call.args[0]
    assert call.kwargs["json"] == {"group_id": "120363111@g.us", "phone": "0712345678"}
