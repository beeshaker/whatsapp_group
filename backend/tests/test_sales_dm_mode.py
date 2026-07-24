import importlib
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import main as backend_main
from database import get_db

_DM_PAYLOAD = {
    "event": "message.received",
    "data": {
        "id": "dm-msg-sales-1",
        "type": "chat",
        "isGroup": False,
        "chatId": "254700111222@c.us",
        "from": "254700111222@c.us",
        "notifyName": "Prospect",
        "author": "254700111222@c.us",
        "body": "Hi, how does this work for a property management company?",
        "timestamp": 1782293340,
    },
}


@pytest.fixture(autouse=True)
def _restore_main_module_state():
    """See identical fixture in test_fleet_plate_mode.py / test_billing_forward.py:
    reloading `main` after monkeypatching SALES_DM_MODE mutates the shared
    module's globals, so it must be reloaded again once monkeypatch reverts
    the env var, or later tests see a stale SALES_DM_MODE value."""
    yield
    importlib.reload(backend_main)


@pytest_asyncio.fixture
async def sales_dm_client(monkeypatch):
    monkeypatch.setenv("SALES_DM_MODE", "true")
    from tests.conftest import _TestSession
    importlib.reload(backend_main)

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        yield c
    backend_main.app.dependency_overrides.clear()


async def test_unknown_dm_gets_sales_reply_when_sales_dm_mode_enabled(sales_dm_client):
    with patch("main.answer_sales_query", new=AsyncMock(return_value="We help property managers track maintenance via WhatsApp!")) as mock_sales:
        with patch("main.send_group_message", new=AsyncMock(return_value="msg-id")) as mock_send:
            resp = await sales_dm_client.post(
                "/api/v1/ops/ingest", json=_DM_PAYLOAD, headers={"X-API-Key": "test-secret"}
            )

    assert resp.status_code == 202
    assert resp.json()["status"] == "sales_dm_handled"
    mock_sales.assert_awaited_once()
    call_args = mock_sales.await_args.args
    assert call_args[0] == "Hi, how does this work for a property management company?"
    assert call_args[1] == "sales_dm:254700111222"
    mock_send.assert_awaited_once_with(
        "254700111222@c.us", "We help property managers track maintenance via WhatsApp!"
    )


async def test_unknown_dm_still_ignored_when_sales_dm_mode_disabled(client):
    # Default (client fixture, no SALES_DM_MODE) — must preserve existing behavior.
    with patch("main.answer_sales_query", new=AsyncMock()) as mock_sales:
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            resp = await client.post(
                "/api/v1/ops/ingest", json=_DM_PAYLOAD, headers={"X-API-Key": "test-secret"}
            )
    assert resp.status_code == 202
    assert resp.json()["status"] == "dm_ignored"
    mock_sales.assert_not_awaited()
    mock_send.assert_not_awaited()


async def test_sales_dm_mode_skips_fromme_echo(sales_dm_client):
    echo_payload = {
        "event": "message.received",
        "data": {**_DM_PAYLOAD["data"], "fromMe": True},
    }
    with patch("main.answer_sales_query", new=AsyncMock()) as mock_sales:
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            resp = await sales_dm_client.post(
                "/api/v1/ops/ingest", json=echo_payload, headers={"X-API-Key": "test-secret"}
            )
    assert resp.status_code == 202
    assert resp.json()["status"] == "dm_ignored"
    mock_sales.assert_not_awaited()
    mock_send.assert_not_awaited()
