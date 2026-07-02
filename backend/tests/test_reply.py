from unittest.mock import AsyncMock, patch

_INCIDENT_CLASS = {"is_incident": True, "category": "plumbing", "priority": "high", "confidence": 0.92}

_ORIGINAL = {
    "event": "message.received",
    "data": {
        "id": "msg-r1",
        "type": "chat",
        "isGroup": True,
        "chatId": "123@g.us",
        "chat": {"name": "Block A"},
        "author": "2541@c.us",
        "notifyName": "Alice",
        "body": "Pump leaking",
        "timestamp": 1782293340,
    },
}


async def _create_incident(client) -> int:
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    return (await client.get("/incidents")).json()[0]["id"]


async def test_reply_creates_update_and_returns_it(client):
    incident_id = await _create_incident(client)
    with patch("main.reply_to_message", new=AsyncMock(return_value="wa-outgoing-123")):
        r = await client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "We are on our way"},
            headers={"X-API-Key": "test-secret"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["reporter_name"] == "Dashboard"
    assert body["message_body"] == "We are on our way"
    assert body["ai_linked"] is False
    assert body["media_count"] == 0


async def test_reply_sets_incident_updated_at(client):
    incident_id = await _create_incident(client)
    with patch("main.reply_to_message", new=AsyncMock(return_value="wa-msg-upd")):
        await client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "Acknowledged"},
            headers={"X-API-Key": "test-secret"},
        )
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert detail["updated_at"] is not None


async def test_reply_update_appears_in_detail_endpoint(client):
    incident_id = await _create_incident(client)
    with patch("main.reply_to_message", new=AsyncMock(return_value="wa-msg-detail")):
        await client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "Technician dispatched"},
            headers={"X-API-Key": "test-secret"},
        )
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert len(detail["updates"]) == 1
    assert detail["updates"][0]["reporter_name"] == "Dashboard"
    assert detail["updates"][0]["message_body"] == "Technician dispatched"


async def test_reply_requires_auth():
    """No valid API key and no session cookie → 401."""
    from httpx import AsyncClient, ASGITransport
    from main import app as _app
    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as ac:
        r = await ac.post(
            "/incidents/1/reply",
            json={"text": "Hello"},
            headers={"X-API-Key": "wrong"},
        )
    assert r.status_code == 401


async def test_reply_returns_422_for_empty_text(client):
    incident_id = await _create_incident(client)
    r = await client.post(
        f"/incidents/{incident_id}/reply",
        json={"text": "   "},
        headers={"X-API-Key": "test-secret"},
    )
    assert r.status_code == 422


async def test_reply_returns_404_for_missing_incident(client):
    r = await client.post(
        "/incidents/9999/reply",
        json={"text": "Hello"},
        headers={"X-API-Key": "test-secret"},
    )
    assert r.status_code == 404


async def test_reply_returns_502_when_openwa_fails(client):
    incident_id = await _create_incident(client)
    with patch("main.send_group_message", new=AsyncMock(side_effect=Exception("connection refused"))):
        r = await client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "Test message"},
            headers={"X-API-Key": "test-secret"},
        )
    assert r.status_code == 502
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert len(detail["updates"]) == 0


async def test_reply_echo_dedup(client):
    incident_id = await _create_incident(client)
    wa_id = "wa-echo-test-999"
    with patch("main.reply_to_message", new=AsyncMock(return_value=wa_id)):
        await client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "Echo test"},
            headers={"X-API-Key": "test-secret"},
        )
    # Simulate the echo coming back through the webhook with the same message ID
    echo_payload = {
        "event": "message.received",
        "data": {
            "id": wa_id,
            "type": "chat",
            "isGroup": True,
            "chatId": "123@g.us",
            "chat": {"name": "Block A"},
            "author": "2541@c.us",
            "body": "Echo test",
            "timestamp": 1782293400,
        },
    }
    r = await client.post("/api/v1/ops/ingest", json=echo_payload, headers={"X-API-Key": "test-secret"})
    assert r.json()["status"] == "duplicate"
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert len(detail["updates"]) == 1
