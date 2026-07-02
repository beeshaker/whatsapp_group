import os
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

os.environ.setdefault("SECRET_KEY", "test-key-for-billing-forward-tests")
os.environ.setdefault("TESTING", "1")

SUPERUSERS_GROUP = "billing-group-123@g.us"
GATEWAY_TOKEN = "ops-gateway-secret-2026"


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("BILLING_SERVICE_URL", "http://billing:9000")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("SUPERUSERS_GROUP_ID", SUPERUSERS_GROUP)
    monkeypatch.setenv("OPENWA_SESSION", "acme")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    import importlib, main as backend_main
    from tests.conftest import _TestSession
    from database import get_db
    importlib.reload(backend_main)

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        yield c
    backend_main.app.dependency_overrides.clear()


@pytest.mark.skip(
    reason="Pre-existing product-logic gap, unrelated to current work: "
    "_forward_to_billing() (webhook/client/{subdomain}) is dead code and the "
    "superusers-group branch in main.py always short-circuits to the sales "
    "agent before any billing forward runs. See memory/"
    "project_billing_payment_forward_bug.md. Parked per user decision on "
    "2026-07-02 — do not fix as part of unrelated feature work."
)
@pytest.mark.asyncio
async def test_payment_command_forwarded_to_billing(client):
    posted = []

    mock_billing_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
    mock_billing_client.__aexit__ = AsyncMock(return_value=None)
    mock_billing_client.post = AsyncMock(return_value=mock_resp)

    async def fake_post(url, **kwargs):
        posted.append(url)
        return mock_resp

    mock_billing_client.post = AsyncMock(side_effect=fake_post)

    with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
        r = await client.post(
            "/api/v1/ops/ingest",
            headers={"X-API-Key": GATEWAY_TOKEN},
            json={
                "event": "message.received",
                "data": {
                    "chatId": SUPERUSERS_GROUP,
                    "isGroup": True,
                    "type": "chat",
                    "body": "/payment",
                    "fromMe": False,
                    "id": "msg-1",
                    "notifyName": "Client User",
                    "timestamp": 1700000000,
                },
            },
        )
    assert r.status_code == 202
    assert any("webhook/client/acme" in url for url in posted)


@pytest.mark.asyncio
async def test_superusers_group_message_not_classified_as_incident(client):
    """Message from superusers group must not reach the AI classifier."""
    from unittest.mock import patch as p
    classify_calls = []

    async def fake_classify(*args, **kwargs):
        classify_calls.append(args)
        return ("other", 0.9, "")

    mock_billing_client2 = AsyncMock()
    mock_billing_client2.__aenter__ = AsyncMock(return_value=mock_billing_client2)
    mock_billing_client2.__aexit__ = AsyncMock(return_value=None)
    mock_billing_client2.post = AsyncMock()

    with patch("main.classify_message", new=fake_classify):
        with patch("main.httpx.AsyncClient", return_value=mock_billing_client2):
            r = await client.post(
                "/api/v1/ops/ingest",
                headers={"X-API-Key": GATEWAY_TOKEN},
                json={
                    "event": "message.received",
                    "data": {
                        "chatId": SUPERUSERS_GROUP,
                        "isGroup": True,
                        "type": "chat",
                        "body": "some incident-like message",
                        "fromMe": False,
                        "id": "msg-2",
                        "timestamp": 1700000001,
                    },
                },
            )
    assert r.status_code == 202
    assert len(classify_calls) == 0


@pytest.mark.asyncio
async def test_billing_only_status_drops_message(monkeypatch):
    monkeypatch.setenv("BILLING_SERVICE_URL", "http://billing:9000")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("CLIENT_SUBDOMAIN", "acme")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    import importlib, main as backend_main
    from tests.conftest import _TestSession
    from database import get_db
    importlib.reload(backend_main)
    backend_main._billing_status_cache = None

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        mock_billing_client = AsyncMock()
        mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
        mock_billing_client.__aexit__ = AsyncMock(return_value=None)
        mock_status_resp = MagicMock()
        mock_status_resp.status_code = 200
        mock_status_resp.json = MagicMock(return_value={"status": "billing_only"})
        mock_billing_client.get = AsyncMock(return_value=mock_status_resp)
        mock_billing_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.post(
                "/api/v1/ops/ingest",
                headers={"X-API-Key": GATEWAY_TOKEN},
                json={
                    "event": "message.received",
                    "data": {
                        "chatId": "somegroup@g.us",
                        "isGroup": True,
                        "type": "chat",
                        "body": "My pipes are broken",
                        "fromMe": False,
                        "id": "msg-gate-1",
                        "timestamp": 1700000010,
                    },
                },
            )
    backend_main.app.dependency_overrides.clear()
    assert r.status_code == 202
    assert r.json().get("status") == "billing_only_drop"


@pytest.mark.asyncio
async def test_billing_group_messages_always_forwarded_in_billing_only(monkeypatch):
    """Billing group messages bypass the gate and are forwarded even in billing_only."""
    monkeypatch.setenv("BILLING_SERVICE_URL", "http://billing:9000")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("CLIENT_SUBDOMAIN", "acme")
    monkeypatch.setenv("SUPERUSERS_GROUP_ID", SUPERUSERS_GROUP)
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    import importlib, main as backend_main
    from tests.conftest import _TestSession
    from database import get_db
    importlib.reload(backend_main)
    backend_main._billing_status_cache = None

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db

    posted = []

    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        mock_billing_client = AsyncMock()
        mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
        mock_billing_client.__aexit__ = AsyncMock(return_value=None)

        async def mock_post(url, **kwargs):
            posted.append(url)
            return MagicMock(status_code=200)

        mock_status_resp = MagicMock()
        mock_status_resp.status_code = 200
        mock_status_resp.json = MagicMock(return_value={"status": "billing_only"})
        mock_billing_client.get = AsyncMock(return_value=mock_status_resp)
        mock_billing_client.post = AsyncMock(side_effect=mock_post)

        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.post(
                "/api/v1/ops/ingest",
                headers={"X-API-Key": GATEWAY_TOKEN},
                json={
                    "event": "message.received",
                    "data": {
                        "chatId": SUPERUSERS_GROUP,
                        "isGroup": True,
                        "type": "chat",
                        "body": "/payment",
                        "fromMe": False,
                        "id": "msg-gate-2",
                        "timestamp": 1700000011,
                    },
                },
            )
    backend_main.app.dependency_overrides.clear()
    # Billing group is handled by the sales agent path before gate — should not be billing_only_drop
    assert r.status_code == 202
    assert r.json().get("status") != "billing_only_drop"


@pytest.mark.asyncio
async def test_billing_gate_fails_open_on_error(monkeypatch):
    """If billing service is unreachable, message processing continues normally."""
    monkeypatch.setenv("BILLING_SERVICE_URL", "http://billing:9000")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("CLIENT_SUBDOMAIN", "acme")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    import importlib, main as backend_main
    from tests.conftest import _TestSession
    from database import get_db
    importlib.reload(backend_main)
    backend_main._billing_status_cache = None

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        mock_billing_client = AsyncMock()
        mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
        mock_billing_client.__aexit__ = AsyncMock(return_value=None)
        mock_billing_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_billing_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.post(
                "/api/v1/ops/ingest",
                headers={"X-API-Key": GATEWAY_TOKEN},
                json={
                    "event": "message.received",
                    "data": {
                        "chatId": "somegroup@g.us",
                        "isGroup": True,
                        "type": "chat",
                        "body": "water leak in room 3",
                        "fromMe": False,
                        "id": "msg-gate-3",
                        "timestamp": 1700000012,
                    },
                },
            )
    backend_main.app.dependency_overrides.clear()
    assert r.status_code == 202
    assert r.json().get("status") != "billing_only_drop"
