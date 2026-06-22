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
    importlib.reload(backend_main)
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        yield c


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
